"""log_blocker — record that the user is stuck on something specific."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..db.models import Blocker, Task
from .base import ToolContext, resolve_project_id


def log_blocker(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Insert a blocker and return a short confirmation for Claude.

    Feeds stall detection, and is retrievable via ``query_memory`` scope=blockers.
    """
    description = (tool_input.get("description") or "").strip()
    if not description:
        return "Nothing to log — no blocker description provided."

    reason = (tool_input.get("reason") or "").strip() or None
    project_name = tool_input.get("project_name")
    project_id = resolve_project_id(ctx.session, project_name)
    task_id = _valid_task_id(ctx.session, tool_input.get("task_id"))

    blocker = Blocker(
        description=description,
        reason=reason,
        project_id=project_id,
        task_id=task_id,
    )
    ctx.session.add(blocker)
    ctx.session.flush()  # populate blocker.id within the transaction

    where = ""
    if project_name:
        where = f" on {project_name}" if project_id else f" (project '{project_name}' not found, logged unlinked)"
    return f"Blocker #{blocker.id} logged{where}: {description}"


def _valid_task_id(session, task_id: Any) -> int | None:
    """Keep ``task_id`` only if it references an existing task; else drop it.

    Avoids a foreign-key violation if the model passes a task id that doesn't
    exist (the tasks table may well be empty).
    """
    if task_id is None:
        return None
    exists = session.scalar(select(Task.id).where(Task.id == task_id))
    return task_id if exists is not None else None
