"""schedule_intent — record a scheduling intent. V1 never touches a calendar."""

from __future__ import annotations

from typing import Any

from ..db.models import ScheduleIntent
from .base import ToolContext, resolve_project_id


def schedule_intent(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Insert a scheduling intent (intent only) and confirm.

    Stored cleanly so it can surface in the morning brief later (Step 8).
    """
    description = (tool_input.get("description") or "").strip()
    if not description:
        return "Nothing to schedule — no description provided."

    when_text = _clean(tool_input.get("when_text"))
    duration_text = _clean(tool_input.get("duration_text"))
    attendees = _clean(tool_input.get("attendees"))
    project_id = resolve_project_id(ctx.session, tool_input.get("project_name"))

    intent = ScheduleIntent(
        description=description,
        when_text=when_text,
        duration_text=duration_text,
        attendees=attendees,
        project_id=project_id,
    )
    ctx.session.add(intent)
    ctx.session.flush()  # populate intent.id within the transaction

    when = f" for {when_text}" if when_text else ""
    return (
        f"Scheduling intent #{intent.id} recorded (not on any calendar — V1 captures "
        f"intent only){when}: {description}"
    )


def _clean(value: Any) -> str | None:
    """Strip a string-ish value to None when empty/missing."""
    if not value:
        return None
    text = str(value).strip()
    return text or None
