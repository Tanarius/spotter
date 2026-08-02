"""supersede_event — explicitly retire stale knowledge.

The reasoned half of supersession (the deterministic half lives in the
session-note writers): when the model NOTICES that a retrieved event is
contradicted by newer information, it retires the old one. The retired event
stays in the log as history but stops surfacing in retrieval, and the
correction itself becomes an event — so Spotter can say not just what it
believes, but what it stopped believing and why.
"""

from __future__ import annotations

from typing import Any

from ..db.models import Event
from .base import ToolContext, utc_now_str


def supersede_event(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Mark an event superseded by a newer event or by a stated correction."""
    raw_id = tool_input.get("event_id")
    reason = (tool_input.get("reason") or "").strip()
    if raw_id is None:
        return "Provide the event_id to retire (query_events shows 'event #N')."
    if not reason:
        return "State the reason — what newer information contradicts it."

    session = ctx.session
    old = session.get(Event, int(raw_id))
    if old is None:
        return f"No event #{raw_id} found."
    if old.superseded_by is not None:
        return f"Event #{old.id} is already superseded (by #{old.superseded_by})."

    replacement_id = tool_input.get("replacement_event_id")
    if replacement_id is not None:
        replacement = session.get(Event, int(replacement_id))
        if replacement is None:
            return f"No replacement event #{replacement_id} found."
        if replacement.id == old.id:
            return "An event can't supersede itself."
    else:
        # No newer event to point at: the correction itself becomes one.
        replacement = Event(
            source="inferred",
            kind="correction",
            project_id=old.project_id,
            subject=old.subject,
            summary=f"Correction: {reason.splitlines()[0][:160]}",
            detail=f"Retires event #{old.id} ('{old.summary[:120]}'): {reason}",
            confidence=0.9,
            occurred_at=utc_now_str(),
        )
        session.add(replacement)
        session.flush()

    old.superseded_by = replacement.id
    return (
        f"Event #{old.id} ('{old.summary[:80]}') retired, superseded by "
        f"#{replacement.id}. It won't surface in retrieval again. Tell the user "
        "in one line what was corrected."
    )
