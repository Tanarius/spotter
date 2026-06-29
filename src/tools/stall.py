"""name_the_stall — record and bluntly call out a project stall."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from ..db.models import StallEvent
from .base import ToolContext, resolve_project


def name_the_stall(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Log a stall event (deduping recent identical ones) and return a blunt callout."""
    project_name = tool_input.get("project_name")
    avoided_step = (tool_input.get("avoided_step") or "").strip()
    pattern_observed = (tool_input.get("pattern_observed") or "").strip()

    if not avoided_step:
        return "Can't name a stall without the avoided step."

    # stall_events.project_id is NOT NULL, so a stall must attach to a real project.
    project = resolve_project(ctx.session, project_name)
    if project is None:
        return (
            f"No project named '{project_name}' is on record, so there's nothing to log a "
            "stall against."
        )

    existing = _recent_unresolved(ctx.session, project.id, avoided_step)
    if existing is not None:
        # Don't re-log the identical callout twice in a row — point back to it.
        return (
            f"Already named this exact stall (#{existing.id}) on {project.name} and it's "
            f'still open: avoiding "{avoided_step}". Don\'t re-log it — point back to it and '
            "hold the line. The step hasn't changed."
        )

    event = StallEvent(project_id=project.id, description=avoided_step)
    ctx.session.add(event)
    ctx.session.flush()  # populate event.id within the transaction
    return (
        f"Stall logged (#{event.id}) on {project.name}. Avoided step: {avoided_step}. "
        f"Pattern: {pattern_observed or 'n/a'}. Call this out directly and bluntly — name "
        "the avoidance, point at the concrete step, and do not soften."
    )


def _recent_unresolved(session, project_id: int, avoided_step: str) -> StallEvent | None:
    """The most recent unresolved stall on this project with the same avoided step."""
    return session.scalar(
        select(StallEvent)
        .where(
            StallEvent.project_id == project_id,
            StallEvent.resolved == 0,
            func.lower(StallEvent.description) == avoided_step.lower(),
        )
        .order_by(StallEvent.id.desc())
    )
