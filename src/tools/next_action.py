"""surface_next_action — the single next concrete step on a project."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..db.models import Task
from .base import ToolContext, resolve_project

# A task is only a candidate "next action" while it's still live.
_LIVE_STATUSES = ("open", "in_progress")


def surface_next_action(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Return the next action for a project, or say plainly there are no tasks."""
    project_name = tool_input.get("project_name")
    smaller_than = tool_input.get("smaller_than")

    project = resolve_project(ctx.session, project_name)
    if project is None:
        if project_name:
            return f"No project named '{project_name}' is on record."
        return "No active project to pull a next action from."

    task = _next_task(ctx.session, project.id)
    if task is None:
        return (
            f"No tasks are recorded for {project.name} yet. Tell the user plainly that "
            "nothing is on the list — do not invent a next action."
        )

    line = f"Next action on {project.name} (task #{task.id}): {task.title}"
    if task.detail:
        line += f" — {task.detail}"
    if smaller_than:
        line += (
            f'\nThe user said this is still too big: "{smaller_than}". Return a smaller '
            "sub-step — the first physical move (which file to open, where to put the "
            "cursor, the first few words to type)."
        )
    return line


def _next_task(session, project_id: int) -> Task | None:
    """The task flagged is_next for the project, else its oldest open task."""
    flagged = session.scalar(
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.is_next == 1,
            Task.status.in_(_LIVE_STATUSES),
        )
        .order_by(Task.id)
    )
    if flagged is not None:
        return flagged
    return session.scalar(
        select(Task)
        .where(Task.project_id == project_id, Task.status == "open")
        .order_by(Task.id)
    )
