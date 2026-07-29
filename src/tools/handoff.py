"""prepare_handoff — hand one task or milestone to Claude Code.

Spotter holds strategy; Claude Code executes. The handler is DB-only, like
every other tool: it resolves the target, assembles everything a handoff needs
(objective, project goal and bottleneck, recent work, blockers, workspace
facts), and instructs the model to compose the final ready-to-paste prompt in
one fenced code block.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Blocker, Milestone, Project, Task, WorkspaceFact
from .base import ToolContext, resolve_project
from .goal import _resolve_milestone
from .next_action import _next_task
from .status import _resolve_task

_MAX_RELATED_TASKS = 8
_MAX_RECENT = 5
_MAX_FACTS = 8


def prepare_handoff(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Assemble handoff context; the model composes the final prompt from it."""
    session = ctx.session
    target, kind, error = _resolve_target(session, tool_input)
    if error:
        return error

    project = session.get(Project, target.project_id) if target.project_id else None
    if project is None:
        return (
            "That target isn't attached to a project; link it to one (or name "
            "the project) so the handoff has its context."
        )
    return _handoff_context(session, project, target, kind)


# -- target resolution ---------------------------------------------------------

def _resolve_target(
    session: Session, tool_input: dict[str, Any]
) -> tuple[Any, str, str | None]:
    """Return (target, 'task'|'milestone', None) or (None, '', error_message)."""
    target_type = (tool_input.get("target_type") or "").strip().lower()
    raw_id = tool_input.get("id")
    title = (tool_input.get("title") or "").strip()

    if target_type == "task":
        task, ambiguity = _resolve_task(session, raw_id, title)
        if ambiguity:
            return None, "", ambiguity
        if task is None:
            return None, "", _not_found("task", raw_id, title)
        return task, "task", None

    if target_type == "milestone":
        milestone, ambiguity = _resolve_milestone(session, raw_id, title)
        if ambiguity:
            return None, "", ambiguity
        if milestone is None:
            return None, "", _not_found("milestone", raw_id, title)
        return milestone, "milestone", None

    # No target_type given.
    if raw_id is not None:
        return None, "", (
            f"Is #{raw_id} a task or a milestone? Call again with target_type."
        )
    if title:
        task, task_ambiguity = _resolve_task(session, None, title)
        if task is not None:
            return task, "task", None
        milestone, milestone_ambiguity = _resolve_milestone(session, None, title)
        if milestone is not None:
            return milestone, "milestone", None
        return None, "", (
            task_ambiguity
            or milestone_ambiguity
            or f"Nothing named '{title}' found among tasks or milestones."
        )

    # Nothing specified: default to the project's active milestone, else its
    # next task — the same thing the focus zone points at.
    project = resolve_project(session, tool_input.get("project_name"))
    if project is None:
        return None, "", "No active project to hand off from."
    active = session.scalar(
        select(Milestone)
        .where(Milestone.project_id == project.id, Milestone.status == "active")
        .order_by(Milestone.order_index, Milestone.id)
    )
    if active is not None:
        return active, "milestone", None
    task = _next_task(session, project.id)
    if task is not None:
        return task, "task", None
    return None, "", (
        f"{project.name} has no active milestone or open task to hand off. "
        "Decompose the goal or add a task first."
    )


def _not_found(kind: str, raw_id: Any, title: str) -> str:
    ref = f"#{raw_id}" if raw_id is not None else f"'{title}'"
    return f"No {kind} {ref} found."


# -- context assembly ----------------------------------------------------------

def _handoff_context(
    session: Session, project: Project, target: Any, kind: str
) -> str:
    detail = (
        target.detail if kind == "task" else target.description
    )
    lines = [
        f"Handoff context — {kind} #{target.id}: {target.title}",
        f"OBJECTIVE: {target.title}" + (f" — {detail}" if detail else ""),
        f"PROJECT: {project.name} ({project.status}, priority {project.priority})"
        + (f" — {project.description}" if project.description else ""),
        f"GOAL: {project.goal or '(no goal recorded)'}",
        f"CURRENT BOTTLENECK: {project.current_bottleneck or '(none recorded)'}",
    ]

    if kind == "task":
        active = session.scalar(
            select(Milestone)
            .where(
                Milestone.project_id == project.id, Milestone.status == "active"
            )
            .order_by(Milestone.order_index, Milestone.id)
        )
        if active is not None:
            lines.append(f"ACTIVE MILESTONE: {active.title}")
    else:
        upcoming = session.scalars(
            select(Milestone)
            .where(
                Milestone.project_id == project.id,
                Milestone.status == "pending",
                Milestone.order_index > target.order_index,
            )
            .order_by(Milestone.order_index, Milestone.id)
            .limit(2)
        ).all()
        if upcoming:
            lines.append(
                "AFTER THIS MILESTONE: " + "; ".join(m.title for m in upcoming)
            )

    related = session.scalars(
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.status.in_(("open", "in_progress", "paused", "waiting")),
        )
        .order_by(Task.is_next.desc(), Task.id)
        .limit(_MAX_RELATED_TASKS)
    ).all()
    related = [t for t in related if not (kind == "task" and t.id == target.id)]
    if related:
        lines.append("RELATED OPEN TASKS:")
        lines += [f"- #{t.id} {t.title} ({t.status})" for t in related]

    completed = session.scalars(
        select(Task)
        .where(Task.project_id == project.id, Task.status == "done")
        .order_by(Task.completed_at.desc())
        .limit(_MAX_RECENT)
    ).all()
    if completed:
        lines.append("RECENTLY COMPLETED:")
        lines += [f"- {t.title}" for t in completed]

    blockers = session.scalars(
        select(Blocker)
        .where(Blocker.project_id == project.id, Blocker.status == "open")
        .order_by(Blocker.id.desc())
        .limit(_MAX_RECENT)
    ).all()
    if blockers:
        lines.append("OPEN BLOCKERS / WHAT'S BEEN TRIED:")
        for b in blockers:
            line = f"- {b.description}"
            if b.reason:
                line += f" (because: {b.reason})"
            if b.resolution_idea:
                line += f" (idea: {b.resolution_idea})"
            lines.append(line)

    facts = session.scalars(
        select(WorkspaceFact)
        .where(WorkspaceFact.is_core == 1)
        .order_by(WorkspaceFact.category, WorkspaceFact.id)
        .limit(_MAX_FACTS)
    ).all()
    if facts:
        lines.append("WORKSPACE CONTEXT:")
        lines += [f"- ({f.category}) {f.content}" for f in facts]

    lines.append(
        "---\n"
        "Compose a ready-to-paste prompt for Claude Code from this. Structure:\n"
        "1. Objective — the concrete thing to build or fix, one short paragraph.\n"
        "2. Context — stack, project state, what's already done, what's blocking.\n"
        "3. Constraints — do ONLY this objective, match the project's existing "
        "patterns, no scope creep beyond it.\n"
        "4. Definition of done — concrete, verifiable completion criteria.\n"
        "Address it to Claude Code (it will have the codebase; it won't have "
        "this conversation). Output ONLY the prompt itself inside one fenced "
        "code block so the user can copy it."
    )
    return "\n".join(lines)
