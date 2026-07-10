"""schedule_reminder — create a scheduled reminder or recurring check-in.

Division of labor: the MODEL resolves natural-language time ("tomorrow
morning", "every evening at 6") into a concrete local datetime using the
Current-time block injected into its context, and passes it as ``when_local``.
This handler owns the deterministic half: parse, attach the configured
timezone, convert to UTC, refuse one-shots in the past, advance a past
recurring start to its first future occurrence, persist the row, and queue
live registration with the running scheduler for after commit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..db.models import ScheduledTrigger
from ..triggers import format_db_utc, next_occurrence
from .base import ToolContext, resolve_project_id

_KINDS = ("reminder", "checkin")
_RECURRENCES = ("daily", "weekly")
_LOCAL_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")


def schedule_reminder(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Create a scheduled trigger; return a confirmation for Claude."""
    kind = (tool_input.get("kind") or "reminder").strip().lower()
    when_local = (tool_input.get("when_local") or "").strip()
    message = (tool_input.get("message") or "").strip()
    recurrence = (tool_input.get("recurrence") or "").strip().lower() or None
    project_name = tool_input.get("project")

    if kind not in _KINDS:
        return f"Invalid kind '{kind}'. Valid: {', '.join(_KINDS)}."
    if recurrence is not None and recurrence not in _RECURRENCES:
        return f"Invalid recurrence '{recurrence}'. Valid: {', '.join(_RECURRENCES)} or omit."
    if not message:
        return "Provide the message (for a reminder) or the check-in instruction."
    if not when_local:
        return (
            "Provide when_local as 'YYYY-MM-DD HH:MM' in the user's timezone, "
            "resolved from the Current time block in your context."
        )

    local = _parse_local(when_local)
    if local is None:
        return f"Could not parse when_local '{when_local}'. Use 'YYYY-MM-DD HH:MM' (24h)."

    tz = ZoneInfo(ctx.config.timezone)
    due_utc = local.replace(tzinfo=tz).astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)

    if due_utc <= now_utc:
        if recurrence is None:
            return (
                f"{when_local} {ctx.config.timezone} is already in the past. "
                "Re-resolve against the Current time block — did the user mean "
                "tomorrow, or a later time today?"
            )
        # A recurring request phrased around a time earlier today ("every
        # evening at 6", said at 9 PM) starts at the next occurrence.
        due_utc = next_occurrence(due_utc, recurrence, tz, now_utc)

    project_id = resolve_project_id(ctx.session, project_name)
    trigger = ScheduledTrigger(
        kind=kind,
        fire_at=format_db_utc(due_utc),
        recurrence=recurrence,
        message_or_prompt=message,
        is_prompt=1 if kind == "checkin" else 0,  # check-ins are Claude-written
        related_project_id=project_id,
    )
    ctx.session.add(trigger)
    ctx.session.flush()  # assign id inside the open transaction

    armed_note = ""
    if ctx.register_trigger is not None:
        registrar = ctx.register_trigger
        trigger_id = trigger.id
        ctx.post_commit.append(lambda: registrar(trigger_id, due_utc))
    else:
        armed_note = " (arms on next restart)"

    local_str = due_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
    cadence = f"recurring {recurrence}, first" if recurrence else "one-shot,"
    project_note = ""
    if project_name and project_id is None:
        project_note = f" No project named '{project_name}' found; saved unlinked."
    return (
        f"Scheduled {kind} #{trigger.id} ({cadence} firing {local_str} / "
        f"{format_db_utc(due_utc)} UTC){armed_note}: \"{message}\".{project_note}"
    )


def _parse_local(value: str) -> datetime | None:
    for fmt in _LOCAL_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
