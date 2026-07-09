"""Firing loop for scheduled_triggers: proactive reminders and check-ins.

On startup :meth:`TriggerService.register_pending` loads every pending row and
registers a one-shot APScheduler job for it. Catch-up: a pending trigger whose
``fire_at`` is already past (the bot was down or redeploying) fires once
immediately, then — if recurring — resumes its normal schedule.

Firing sends the literal ``message_or_prompt`` (or, when ``is_prompt`` is set,
a Claude-generated message) to the allow-listed Telegram user. A one-shot row
is marked ``fired`` only after the send succeeds, so a failed send stays
pending and is retried on the next boot. A recurring row's ``fire_at`` is
advanced in LOCAL wall-clock time (an 18:00 check-in stays 18:00 across DST
boundaries) and stays ``pending``. Any exception is logged and swallowed:
one bad trigger never takes down the scheduler or the bot.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import anthropic
import telegram
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .clock import time_context
from .config import Config
from .db.models import Project, ScheduledTrigger

if TYPE_CHECKING:
    from .scheduler import SpotterScheduler

logger = logging.getLogger(__name__)

_UTC_FMT = "%Y-%m-%d %H:%M:%S"
_GENERATED_MAX_TOKENS = 300
_RECURRENCE_STEPS = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}

# System prompt for Claude-generated trigger messages. Same voice rules as the
# rest of Spotter; the per-call time block is appended at generation time.
_GENERATION_SYSTEM = (
    "You are Spotter, a blunt, specific personal assistant messaging the user on "
    "Telegram at a scheduled time. Write the single short message the instruction "
    "asks for — 1-3 sentences, no greeting fluff, no motivational language, plain "
    "text (no markdown headers). Output only the message itself."
)


def parse_db_utc(value: str) -> datetime:
    """Parse a DB ``YYYY-MM-DD HH:MM:SS`` string as an aware UTC datetime."""
    return datetime.strptime(value, _UTC_FMT).replace(tzinfo=timezone.utc)


def format_db_utc(moment: datetime) -> str:
    """Format an aware datetime as the DB's UTC ``YYYY-MM-DD HH:MM:SS`` string."""
    return moment.astimezone(timezone.utc).strftime(_UTC_FMT)


def next_occurrence(
    fire_at_utc: datetime, recurrence: str, tz: ZoneInfo, now_utc: datetime
) -> datetime:
    """First occurrence strictly after ``now_utc``, advancing in local wall-clock.

    The stored UTC instant is converted to the configured timezone, stepped by
    the recurrence interval on the wall clock (so a daily 18:00 trigger is
    18:00 local on both sides of a DST change), skipping any missed periods,
    and converted back to UTC. Catch-up therefore fires once and resumes —
    never once per missed period.
    """
    step = _RECURRENCE_STEPS[recurrence]
    local_naive = fire_at_utc.astimezone(tz).replace(tzinfo=None)
    while True:
        local_naive += step
        candidate = local_naive.replace(tzinfo=tz).astimezone(timezone.utc)
        if candidate > now_utc:
            return candidate


class TriggerService:
    """Load, fire, and reschedule scheduled_triggers rows."""

    def __init__(
        self,
        config: Config,
        client: anthropic.Anthropic,
        session_factory: sessionmaker[Session],
        scheduler: SpotterScheduler,
    ) -> None:
        self._config = config
        self._client = client
        self._session_factory = session_factory
        self._scheduler = scheduler
        self._tz = ZoneInfo(config.timezone)
        self._bot: telegram.Bot | None = None

    def bind_bot(self, bot: telegram.Bot) -> None:
        """Attach the running bot (from post_init) so firings can send."""
        self._bot = bot

    # -- startup ---------------------------------------------------------------

    def register_pending(self) -> tuple[int, int]:
        """Register every pending trigger with the scheduler.

        Returns ``(total_registered, caught_up)`` where caught-up triggers are
        the past-due ones scheduled to fire immediately.
        """
        now = datetime.now(timezone.utc)
        with self._session_factory() as session:
            rows = session.execute(
                select(ScheduledTrigger.id, ScheduledTrigger.fire_at)
                .where(ScheduledTrigger.status == "pending")
                .order_by(ScheduledTrigger.fire_at)
            ).all()

        caught_up = 0
        for trigger_id, fire_at in rows:
            due = parse_db_utc(fire_at)
            if due <= now:
                caught_up += 1
                logger.info(
                    "Trigger #%d was due %s (past); catch-up firing now",
                    trigger_id,
                    fire_at,
                )
            self.register(trigger_id, due)
        return len(rows), caught_up

    def register(self, trigger_id: int, due_utc: datetime) -> None:
        """Register one trigger; past-due times fire immediately."""
        run_at = max(due_utc, datetime.now(timezone.utc))
        self._scheduler.schedule_trigger(trigger_id, run_at, self.fire)

    # -- firing ------------------------------------------------------------------

    async def fire(self, trigger_id: int) -> None:
        """Fire one trigger. Never raises: failures are logged and contained."""
        try:
            await self._fire(trigger_id)
        except Exception:
            logger.exception("Trigger #%d failed; scheduler continues", trigger_id)

    async def _fire(self, trigger_id: int) -> None:
        snapshot = self._load_snapshot(trigger_id)
        if snapshot is None:
            return
        message, recurrence = snapshot
        if self._bot is None:
            raise RuntimeError("TriggerService has no bot bound; cannot send")

        await self._bot.send_message(
            chat_id=self._config.telegram_allowed_user_id, text=message
        )
        # Only after a successful send: mark fired, or advance a recurring row.
        next_due = await asyncio.to_thread(self._finalize, trigger_id, recurrence)
        if next_due is not None:
            self.register(trigger_id, next_due)
            logger.info(
                "Trigger #%d rescheduled (%s) for %s UTC",
                trigger_id,
                recurrence,
                format_db_utc(next_due),
            )
        else:
            logger.info("Trigger #%d fired", trigger_id)

    def _load_snapshot(self, trigger_id: int) -> tuple[str, str | None] | None:
        """Resolve the outgoing message text (generating if needed) + recurrence."""
        with self._session_factory() as session:
            row = session.get(ScheduledTrigger, trigger_id)
            if row is None or row.status != "pending":
                logger.info(
                    "Trigger #%d skipped (missing or not pending)", trigger_id
                )
                return None
            text = row.message_or_prompt
            is_prompt = bool(row.is_prompt)
            recurrence = row.recurrence
            project = (
                session.get(Project, row.related_project_id)
                if row.related_project_id
                else None
            )
            project_name = project.name if project else None
        if is_prompt:
            text = self._generate(text, project_name)
        return text, recurrence

    def _finalize(self, trigger_id: int, recurrence: str | None) -> datetime | None:
        """Mark a one-shot fired, or advance a recurring fire_at. Returns next due."""
        with self._session_factory() as session, session.begin():
            row = session.get(ScheduledTrigger, trigger_id)
            if row is None or row.status != "pending":
                return None
            if recurrence in _RECURRENCE_STEPS:
                next_due = next_occurrence(
                    parse_db_utc(row.fire_at),
                    recurrence,
                    self._tz,
                    datetime.now(timezone.utc),
                )
                row.fire_at = format_db_utc(next_due)
                return next_due
            row.status = "fired"
            return None

    def _generate(self, prompt: str, project_name: str | None) -> str:
        """Ask Claude to write the outgoing message for a prompt-type trigger."""
        system = f"{_GENERATION_SYSTEM}\n\n{time_context(self._config.timezone)}"
        user = prompt if not project_name else f"{prompt}\n\n(Related project: {project_name})"
        response = self._client.messages.create(
            model=self._config.default_model,
            max_tokens=_GENERATED_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        parts = [block.text for block in response.content if block.type == "text"]
        return "\n".join(parts).strip() or prompt
