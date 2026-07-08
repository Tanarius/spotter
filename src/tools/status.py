"""update_task_status — change the status of an existing task or project.

Resolves the target by numeric id when the model has one from recent context
(other tool results expose ``task #N``), otherwise by case-insensitive exact
then substring name match. Ambiguity is returned to the model as a candidate
list rather than guessed at.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Project, Task
from .base import ToolContext

_TASK_STATUSES = ("open", "in_progress", "done", "paused", "waiting", "dropped")
_PROJECT_STATUSES = ("active", "paused", "done")
# Statuses under which a task can still be worked (mirrors next_action/brief).
_LIVE_TASK_STATUSES = frozenset({"open", "in_progress", "paused", "waiting"})
# Reaching these ends the task's claim on the is_next flag.
_TERMINAL_TASK_STATUSES = frozenset({"done", "dropped"})
_MAX_CANDIDATES = 5


def update_task_status(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Update a task's or project's status; return a confirmation for Claude."""
    target_type = (tool_input.get("target_type") or "").strip().lower()
    status = (tool_input.get("status") or "").strip().lower()
    raw_id = tool_input.get("id")
    name = (tool_input.get("name") or "").strip()

    if target_type not in ("task", "project"):
        return "target_type must be 'task' or 'project'."
    if raw_id is None and not name:
        return "Provide an id or a name to identify the target."

    if target_type == "task":
        return _update_task(ctx.session, raw_id, name, status)
    return _update_project(ctx.session, raw_id, name, status)


# -- tasks --------------------------------------------------------------------

def _update_task(session: Session, raw_id: Any, name: str, status: str) -> str:
    if status not in _TASK_STATUSES:
        return f"Invalid task status '{status}'. Valid: {', '.join(_TASK_STATUSES)}."

    task, ambiguity = _resolve_task(session, raw_id, name)
    if ambiguity:
        return ambiguity
    if task is None:
        ref = f"#{raw_id}" if raw_id is not None else f"'{name}'"
        return f"No task {ref} found."

    old = task.status
    task.status = status
    task.updated_at = _utc_now_str()
    if status == "done":
        task.completed_at = _utc_now_str()
    elif old == "done":
        task.completed_at = None  # reopened
    if status in _TERMINAL_TASK_STATUSES:
        task.is_next = 0

    project = session.get(Project, task.project_id) if task.project_id else None
    where = f" [{project.name}]" if project else ""
    return f"Task #{task.id} '{task.title}'{where}: {old} -> {status}."


def _resolve_task(
    session: Session, raw_id: Any, name: str
) -> tuple[Task | None, str | None]:
    """Return (task, None), (None, None) for no match, or (None, ambiguity_msg)."""
    if raw_id is not None:
        return session.get(Task, int(raw_id)), None

    lowered = name.lower()
    exact = session.scalars(
        select(Task).where(func.lower(Task.title) == lowered).order_by(Task.id)
    ).all()
    matches = exact or session.scalars(
        select(Task)
        .where(func.lower(Task.title).like(f"%{lowered}%"))
        .order_by(Task.id)
        .limit(_MAX_CANDIDATES + 1)
    ).all()

    if not matches:
        return None, None
    if len(matches) == 1:
        return matches[0], None
    # Several matches: if exactly one is still live, that's almost certainly
    # the one being updated (e.g. an old done task shares the name).
    live = [t for t in matches if t.status in _LIVE_TASK_STATUSES]
    if len(live) == 1:
        return live[0], None
    lines = "\n".join(
        f"- #{t.id} '{t.title}' ({t.status})" for t in matches[:_MAX_CANDIDATES]
    )
    return None, (
        f"Multiple tasks match '{name}' — retry with the exact id:\n{lines}"
    )


# -- projects -------------------------------------------------------------------

def _update_project(session: Session, raw_id: Any, name: str, status: str) -> str:
    if status not in _PROJECT_STATUSES:
        return (
            f"Invalid project status '{status}'. Valid: {', '.join(_PROJECT_STATUSES)}. "
            "For 'waiting', pause the project or set the specific task to waiting."
        )

    if raw_id is not None:
        project = session.get(Project, int(raw_id))
    else:
        lowered = name.lower()
        project = session.scalar(
            select(Project).where(func.lower(Project.name) == lowered)
        ) or session.scalar(
            select(Project)
            .where(func.lower(Project.name).like(f"%{lowered}%"))
            .order_by(Project.id)
        )
    if project is None:
        ref = f"#{raw_id}" if raw_id is not None else f"'{name}'"
        return f"No project {ref} found."

    old = project.status
    project.status = status
    project.updated_at = _utc_now_str()
    return f"Project '{project.name}' (#{project.id}): {old} -> {status}."


def _utc_now_str() -> str:
    """UTC now, formatted the way SQLite's CURRENT_TIMESTAMP stores it."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
