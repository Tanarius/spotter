"""Morning brief: assemble state, generate via Claude, send, and record it.

The brief gathers open/in-progress tasks, open blockers, items captured since the
last brief (or the last 24h), and a one-line honest "yesterday" summary; renders
the ``morning_brief`` prompt from prompts.yaml; asks Claude to write the brief;
sends it to the allow-listed user over Telegram; and upserts a ``daily_briefs``
row keyed on the (unique) brief_date so re-running the same day never collides.

Run a one-off for testing without waiting for BRIEF_TIME::

    python -m src.brief
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import anthropic
import telegram
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .clock import time_context
from .config import Config, load_config
from .db import initialize_database, make_session_factory
from .db.models import (
    Blocker,
    CapturedItem,
    DailyBrief,
    Milestone,
    Project,
    Task,
    WorkspaceFact,
)

logger = logging.getLogger(__name__)

_BRIEF_MAX_TOKENS = 1024
_CAPTURED_LOOKBACK_HOURS = 24
_LIVE_TASK_STATUSES = ("open", "in_progress")

# Brief-specific system prompt: Spotter's voice without the chat persona's
# 1-3 sentence length rule (which would fight the brief's structured format).
# Shared by the morning brief and (via triggers._generate_evening) the evening
# check-in, so the goals block below reaches both.
_BRIEF_SYSTEM_TEMPLATE = (
    "You are Spotter, a blunt, specific personal assistant. Write the user's {label} "
    "exactly as the user message instructs: honor the requested structure, order, "
    "and word limit, and use no motivational language. Be honest — never invent progress, "
    "tasks, or items that aren't in the data provided.\n\n"
    "## Context about the user\n{facts}\n\n"
    "## Project goals and progress\n{goals}\n"
    "Frame the {label} around progress toward these goals — what moved toward the "
    "active milestone, what the bottleneck is blocking — not raw task counts. "
    "If a project has no goal, don't invent one."
)


@dataclass(frozen=True)
class BriefInputs:
    """The rendered pieces that fill the morning_brief prompt, plus metadata."""

    brief_date: str
    today: str
    since: str
    active_tasks: str
    blockers: str
    captured_items: str
    yesterday_summary: str
    today_summary: str
    top_priority: str | None


@dataclass(frozen=True)
class ComposedBrief:
    """A generated brief ready to send and persist."""

    brief_date: str
    content: str
    top_priority: str | None


class BriefService:
    """Assemble, generate, deliver, and record the morning brief."""

    def __init__(
        self,
        config: Config,
        client: anthropic.Anthropic,
        session_factory: sessionmaker[Session],
    ) -> None:
        self._config = config
        self._client = client
        self._session_factory = session_factory

    # -- orchestration -------------------------------------------------------

    async def deliver(self, bot: telegram.Bot) -> ComposedBrief:
        """Compose (blocking, off-loop), send over Telegram, then persist."""
        brief = await asyncio.to_thread(self.compose)
        await bot.send_message(
            chat_id=self._config.telegram_allowed_user_id, text=brief.content
        )
        await asyncio.to_thread(self.persist, brief)
        return brief

    def compose(self) -> ComposedBrief:
        """Gather state and ask Claude to write the brief. Blocking."""
        with self._session_factory() as session:
            inputs = assemble_inputs(session, self._config)
            system = build_brief_system(self._config, session)
        user_prompt = render_morning_prompt(self._config, inputs)
        content = self._call_claude(system, user_prompt)
        return ComposedBrief(
            brief_date=inputs.brief_date, content=content, top_priority=inputs.top_priority
        )

    def persist(self, brief: ComposedBrief) -> None:
        """Upsert the daily_briefs row for this date (unique brief_date)."""
        with self._session_factory() as session, session.begin():
            row = session.scalar(
                select(DailyBrief).where(DailyBrief.brief_date == brief.brief_date)
            )
            if row is None:
                session.add(
                    DailyBrief(
                        brief_date=brief.brief_date,
                        content=brief.content,
                        top_priority=brief.top_priority,
                    )
                )
            else:
                row.content = brief.content
                row.top_priority = brief.top_priority
                row.delivered_at = _utc_now_str()

    def is_due_catch_up(self) -> bool:
        """True when today's BRIEF_TIME has passed but no brief row exists yet.

        Used at startup: the cron job only fires while the bot is running, so a
        redeploy spanning BRIEF_TIME would otherwise silently skip the brief.
        daily_briefs.brief_date is unique, making this check double-send-proof.
        """
        tz = ZoneInfo(self._config.timezone)
        now_local = datetime.now(tz)
        hour, minute = (int(part) for part in self._config.brief_time.split(":"))
        if (now_local.hour, now_local.minute) < (hour, minute):
            return False
        today = now_local.strftime("%Y-%m-%d")
        with self._session_factory() as session:
            exists = session.scalar(
                select(DailyBrief.id).where(DailyBrief.brief_date == today)
            )
        return exists is None

    def _call_claude(self, system: str, user_prompt: str) -> str:
        response = self._client.messages.create(
            model=self._config.default_model,
            max_tokens=_BRIEF_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )
        parts = [block.text for block in response.content if block.type == "text"]
        return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def assemble_inputs(session: Session, config: Config) -> BriefInputs:
    """Gather everything the morning_brief prompt needs from the database."""
    tz = ZoneInfo(config.timezone)
    now_local = datetime.now(tz)
    brief_date = now_local.strftime("%Y-%m-%d")
    today = now_local.strftime("%A, %B %d, %Y")
    yesterday = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")

    last_brief = session.scalar(select(DailyBrief).order_by(DailyBrief.id.desc()))
    since = last_brief.brief_date if last_brief is not None else "never (first brief)"
    # captured_items.created_at and daily_briefs.delivered_at are both UTC strings,
    # so the cutoff comparison is apples-to-apples in UTC.
    if last_brief is not None:
        cutoff = last_brief.delivered_at
    else:
        cutoff = _utc_now_str(datetime.now(timezone.utc) - timedelta(hours=_CAPTURED_LOOKBACK_HOURS))

    return BriefInputs(
        brief_date=brief_date,
        today=today,
        since=since,
        active_tasks=_format_active_tasks(session),
        blockers=_format_open_blockers(session),
        captured_items=_format_captured_since(session, cutoff),
        yesterday_summary=_yesterday_summary(session, yesterday),
        today_summary=_completed_summary(session, brief_date, "today"),
        top_priority=_top_priority(session),
    )


def _format_active_tasks(session: Session) -> str:
    rows = session.execute(
        select(Task.id, Task.title, Task.status, Task.is_next, Project.name)
        .outerjoin(Project, Task.project_id == Project.id)
        .where(Task.status.in_(_LIVE_TASK_STATUSES))
        .order_by(Project.priority.desc().nulls_last(), Task.is_next.desc(), Task.id)
    ).all()
    if not rows:
        return "(none)"
    lines = []
    for task_id, title, status, is_next, project_name in rows:
        tag = f"[{project_name}] " if project_name else ""
        flag = " (next)" if is_next else ""
        lines.append(f"- {tag}{title} — {status}{flag} (task #{task_id})")
    return "\n".join(lines)


def _format_open_blockers(session: Session) -> str:
    rows = session.execute(
        select(Blocker.id, Blocker.description, Blocker.reason, Project.name)
        .outerjoin(Project, Blocker.project_id == Project.id)
        .where(Blocker.status == "open")
        .order_by(Blocker.id)
    ).all()
    if not rows:
        return "(none)"
    lines = []
    for blocker_id, description, reason, project_name in rows:
        tag = f"[{project_name}] " if project_name else ""
        why = f" — {reason}" if reason else ""
        lines.append(f"- {tag}{description}{why} (blocker #{blocker_id})")
    return "\n".join(lines)


def _format_captured_since(session: Session, cutoff: str) -> str:
    rows = session.execute(
        select(CapturedItem.id, CapturedItem.category, CapturedItem.content)
        .where(CapturedItem.created_at > cutoff)
        .order_by(CapturedItem.created_at.desc())
        .limit(20)
    ).all()
    if not rows:
        return "(none)"
    return "\n".join(
        f"- ({category or 'uncategorized'}) {content} (#{item_id})"
        for item_id, category, content in rows
    )


def _yesterday_summary(session: Session, yesterday: str) -> str:
    """One honest line about yesterday. No completions -> say it was quiet."""
    return _completed_summary(session, yesterday, "yesterday")


def _completed_summary(session: Session, local_date: str, label: str) -> str:
    """One honest line about completions on a local date (UTC-close enough:
    completed_at is UTC; the day label is what the prompt needs, not a ledger)."""
    done = session.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.completed_at.is_not(None), func.date(Task.completed_at) == local_date)
    ) or 0
    if done == 0:
        return f"Quiet — nothing was marked complete {label}."
    return f"Completed {done} task(s) {label}."


def _top_priority(session: Session) -> str | None:
    """The next action on the highest-priority active project, else its name."""
    project = session.scalar(
        select(Project)
        .where(Project.status == "active")
        .order_by(Project.priority.desc(), Project.id)
    )
    if project is None:
        return None
    task = session.scalar(
        select(Task).where(Task.project_id == project.id, Task.is_next == 1)
    )
    return task.title if task is not None else project.name


def _format_goal_context(session: Session) -> str:
    """One line per active project: goal, active milestone, bottleneck."""
    projects = session.scalars(
        select(Project)
        .where(Project.status == "active")
        .order_by(Project.priority.desc(), Project.id)
    ).all()
    if not projects:
        return "(no active projects)"
    active_by_project = {
        m.project_id: m.title
        for m in session.scalars(
            select(Milestone)
            .where(Milestone.status == "active")
            .order_by(Milestone.order_index, Milestone.id)
        )
    }
    lines = []
    for p in projects:
        bits = [f"GOAL: {p.goal}" if p.goal else "GOAL: (not set)"]
        milestone = active_by_project.get(p.id)
        if milestone:
            bits.append(f"ACTIVE MILESTONE: {milestone}")
        if p.current_bottleneck:
            bits.append(f"BOTTLENECK: {p.current_bottleneck}")
        lines.append(f"- {p.name} — " + " | ".join(bits))
    return "\n".join(lines)


def build_brief_system(
    config: Config, session: Session, label: str = "morning brief"
) -> str:
    """Brief-family system prompt hydrated with core facts and goal context."""
    facts = session.scalars(
        select(WorkspaceFact)
        .where(WorkspaceFact.is_core == 1)
        .order_by(WorkspaceFact.category, WorkspaceFact.id)
    ).all()
    facts_text = "\n".join(f"- ({f.category}) {f.content}" for f in facts) or "(none)"
    hydrated = (
        _BRIEF_SYSTEM_TEMPLATE.replace("{label}", label)
        .replace("{facts}", facts_text)
        .replace("{goals}", _format_goal_context(session))
    )
    # Same per-call time block as the chat brain: the brief is the most
    # time-sensitive thing Spotter writes. {today} in the user prompt stays.
    return f"{hydrated}\n\n{time_context(config.timezone)}"


def render_morning_prompt(config: Config, inputs: BriefInputs) -> str:
    """Fill the morning_brief template. str.replace keeps literal braces safe."""
    template = config.prompts.get("morning_brief", "")
    return (
        template.replace("{today}", inputs.today)
        .replace("{since}", inputs.since)
        .replace("{active_tasks}", inputs.active_tasks)
        .replace("{blockers}", inputs.blockers)
        .replace("{captured_items}", inputs.captured_items)
        .replace("{yesterday_summary}", inputs.yesterday_summary)
    )


def render_evening_prompt(config: Config, inputs: BriefInputs) -> str:
    """Fill the evening_checkin template. str.replace keeps literal braces safe."""
    template = config.prompts.get("evening_checkin", "")
    return (
        template.replace("{today}", inputs.today)
        .replace("{top_priority}", inputs.top_priority or "(no stated priority)")
        .replace("{today_summary}", inputs.today_summary)
        .replace("{active_tasks}", inputs.active_tasks)
        .replace("{blockers}", inputs.blockers)
    )


def _utc_now_str(moment: datetime | None = None) -> str:
    """Format a UTC datetime the way SQLite's CURRENT_TIMESTAMP does."""
    moment = moment or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Manual one-shot trigger: python -m src.brief
# ---------------------------------------------------------------------------
async def _run_once() -> None:
    config = load_config()
    engine, _ = initialize_database(config.db_path, config.seed_context_yaml)
    session_factory = make_session_factory(engine)
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    service = BriefService(config, client, session_factory)

    bot = telegram.Bot(config.telegram_bot_token)
    async with bot:
        brief = await service.deliver(bot)
    print(f"Brief delivered for {brief.brief_date}. top_priority={brief.top_priority!r}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_run_once())


if __name__ == "__main__":
    main()
