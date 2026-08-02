"""Conditions engine: proactive nudges from data, not the clock (phase 4E).

A daily scheduled check evaluates real conditions against the event log and
workspace state, and sends AT MOST one nudge per local day — a bot that pings
five times gets muted, and a muted bot is worse than a silent one. Every nudge
is deterministic (numbers come from queries, never a model), recorded back
into the event log (source ``inferred``, kind ``nudge``) — which is also how
the daily cap and the per-condition cooldown are enforced.

Conditions, in priority order (first match wins):
  1. milestone_stuck  — commits landing all week while the active milestone
     hasn't moved: "is it done?"
  2. project_inactive — an active project with zero activity across all
     channels (events AND task updates) for 12+ days.
  3. stale_next       — the flagged next action untouched for 3+ days: the
     stall pattern, detected from data instead of vibes.

Stated-commitment dates are deliberately not implemented yet: schedule_intents
stores free-text times, and a wrong "you said today" is worse than none.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import telegram
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import Config
from .db.models import Event, Milestone, Project, Task

logger = logging.getLogger(__name__)

_UTC_FMT = "%Y-%m-%d %H:%M:%S"
# Thresholds: tuned for "specific and true", not sensitivity.
_MILESTONE_COMMIT_WINDOW_DAYS = 7
_MILESTONE_MIN_COMMITS = 3
_INACTIVE_DAYS = 12
_STALE_NEXT_DAYS = 3
# The same condition+target doesn't repeat within this window.
_CONDITION_COOLDOWN_DAYS = 3


class ConditionsService:
    """Evaluate conditions daily and deliver at most one earned nudge."""

    def __init__(
        self, config: Config, session_factory: sessionmaker[Session]
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._tz = ZoneInfo(config.timezone)
        self._bot: telegram.Bot | None = None

    def bind_bot(self, bot: telegram.Bot) -> None:
        self._bot = bot

    async def run_check(self) -> None:
        """The scheduled entrypoint. Never raises."""
        try:
            found = await asyncio.to_thread(self._evaluate)
            if found is None:
                return
            key, message = found
            if self._bot is None:
                logger.warning("Nudge suppressed (no bot bound): %s", key)
                return
            await self._bot.send_message(
                chat_id=self._config.telegram_allowed_user_id, text=message
            )
            await asyncio.to_thread(self._record, key, message)
            logger.info("Nudge sent: %s", key)
        except Exception:
            logger.exception("Conditions check failed; next run tomorrow")

    # -- evaluation --------------------------------------------------------------

    def _evaluate(self) -> tuple[str, str] | None:
        """Return (condition_key, message) for the top firing condition."""
        with self._session_factory() as session:
            if self._nudged_today(session):
                return None
            for check in (self._milestone_stuck, self._project_inactive, self._stale_next):
                found = check(session)
                if found is not None:
                    key, message = found
                    if self._in_cooldown(session, key):
                        continue
                    return key, message
        return None

    def _milestone_stuck(self, session: Session) -> tuple[str, str] | None:
        since = _utc_str(datetime.now(timezone.utc) - timedelta(days=_MILESTONE_COMMIT_WINDOW_DAYS))
        for project in _active_projects(session):
            milestone = session.scalar(
                select(Milestone)
                .where(Milestone.project_id == project.id, Milestone.status == "active")
                .order_by(Milestone.order_index, Milestone.id)
            )
            if milestone is None or (milestone.created_at or "") >= since:
                continue  # no target, or the milestone itself is brand new
            commits = session.scalars(
                select(Event).where(
                    Event.project_id == project.id,
                    Event.source == "github",
                    Event.occurred_at >= since,
                )
            ).all()
            if len(commits) < _MILESTONE_MIN_COMMITS:
                continue
            completed_recently = session.scalar(
                select(Milestone.id).where(
                    Milestone.project_id == project.id,
                    Milestone.completed_at.is_not(None),
                    Milestone.completed_at >= since,
                )
            )
            if completed_recently is not None:
                continue
            key = f"milestone_stuck-{project.id}"
            message = (
                f"{len(commits)} GitHub events on {project.name} this week while "
                f"milestone '{milestone.title}' hasn't moved. Is that milestone "
                "actually done? If yes, tell me and I'll advance it. If not, "
                "what's the gap?"
            )
            return key, message
        return None

    def _project_inactive(self, session: Session) -> tuple[str, str] | None:
        cutoff = _utc_str(datetime.now(timezone.utc) - timedelta(days=_INACTIVE_DAYS))
        for project in _active_projects(session):
            last_event = session.scalar(
                select(Event.occurred_at)
                .where(Event.project_id == project.id, Event.kind != "nudge")
                .order_by(Event.occurred_at.desc())
            )
            last_task_touch = session.scalar(
                select(Task.updated_at)
                .where(Task.project_id == project.id)
                .order_by(Task.updated_at.desc())
            )
            latest = max(filter(None, (last_event, last_task_touch)), default=None)
            if latest is None or latest >= cutoff:
                continue
            days = _days_between(latest, datetime.now(timezone.utc))
            key = f"project_inactive-{project.id}"
            message = (
                f"{project.name} hasn't moved in {days} days — no commits, no "
                "sessions, no task changes — and it's still marked active. "
                "Either park it or name one small step."
            )
            return key, message
        return None

    def _stale_next(self, session: Session) -> tuple[str, str] | None:
        cutoff = _utc_str(datetime.now(timezone.utc) - timedelta(days=_STALE_NEXT_DAYS))
        task = session.execute(
            select(Task, Project.name)
            .join(Project, Task.project_id == Project.id)
            .where(
                Task.is_next == 1,
                Task.status.in_(("open", "in_progress")),
                Task.updated_at < cutoff,
                Project.status == "active",
            )
            .order_by(Project.priority.desc(), Task.updated_at)
        ).first()
        if task is None:
            return None
        row, project_name = task
        days = _days_between(row.updated_at, datetime.now(timezone.utc))
        key = f"stale_next-{row.id}"
        message = (
            f"'{row.title}' has been your next action on {project_name} for "
            f"{days} days with no status change. That's the stall pattern. "
            "Do the first ten minutes now, or ask me to shrink it."
        )
        return key, message

    # -- caps and bookkeeping ----------------------------------------------------

    def _nudged_today(self, session: Session) -> bool:
        """At most one nudge per LOCAL calendar day."""
        local_midnight = datetime.now(self._tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        since = _utc_str(local_midnight.astimezone(timezone.utc))
        return (
            session.scalar(
                select(Event.id).where(
                    Event.kind == "nudge", Event.occurred_at >= since
                )
            )
            is not None
        )

    def _in_cooldown(self, session: Session, key: str) -> bool:
        since = _utc_str(
            datetime.now(timezone.utc) - timedelta(days=_CONDITION_COOLDOWN_DAYS)
        )
        return (
            session.scalar(
                select(Event.id).where(
                    Event.kind == "nudge",
                    Event.subject == key,
                    Event.occurred_at >= since,
                )
            )
            is not None
        )

    def _record(self, key: str, message: str) -> None:
        now = datetime.now(timezone.utc)
        with self._session_factory() as session, session.begin():
            session.add(
                Event(
                    source="inferred",
                    kind="nudge",
                    subject=key,
                    summary=f"Nudge sent: {message.splitlines()[0][:160]}",
                    detail=message,
                    confidence=1.0,
                    occurred_at=_utc_str(now),
                    external_id=f"nudge-{key}-{now.strftime('%Y%m%d%H%M%S')}",
                )
            )


def _active_projects(session: Session) -> list[Project]:
    return list(
        session.scalars(
            select(Project)
            .where(Project.status == "active")
            .order_by(Project.priority.desc(), Project.id)
        )
    )


def _utc_str(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_UTC_FMT)


def _days_between(stamp: str, now: datetime) -> int:
    try:
        then = datetime.strptime(stamp, _UTC_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0
    return max(0, (now - then).days)
