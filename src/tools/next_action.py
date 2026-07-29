"""surface_next_action — the single next concrete step on a project.

Goal-aware: when the project has a goal set, the tool returns a reasoning
context (goal, current bottleneck, milestone ladder, open tasks, recent
activity) and instructs the model to derive ONE concrete step that advances
the active milestone — and to say why it's next in those terms. When no goal
is set, it falls back to the original stored-task behavior and prompts to set
one. ``smaller_than`` keeps its shrink behavior on both paths.

The handler itself never calls the model; failures of the surrounding brain
call are handled by the brain (fallback reply) and the dashboard (stored-task
fallback), so this tool degrades to stored state by construction.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Blocker, Milestone, Task
from .base import ToolContext, resolve_project

# A task is only a candidate "next action" while it's still live.
_LIVE_STATUSES = ("open", "in_progress")
# Broader live set shown in goal-aware context (mirrors tools/status.py).
_CONTEXT_TASK_STATUSES = ("open", "in_progress", "paused", "waiting")
_MAX_CONTEXT_TASKS = 10
_MAX_CONTEXT_RECENT = 5

_SHRINK_INSTRUCTION = (
    '\nThe user said this is still too big: "{smaller_than}". Return a smaller '
    "sub-step — the first physical move (which file to open, where to put the "
    "cursor, the first few words to type)."
)


def surface_next_action(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Return next-action context for a project, goal-aware when a goal exists."""
    project_name = tool_input.get("project_name")
    smaller_than = tool_input.get("smaller_than")

    project = resolve_project(ctx.session, project_name)
    if project is None:
        if project_name:
            return f"No project named '{project_name}' is on record."
        return "No active project to pull a next action from."

    if (project.goal or "").strip():
        return _goal_aware_context(ctx.session, project, smaller_than)
    return _stored_task_fallback(ctx.session, project, smaller_than)


# -- goal-aware path -----------------------------------------------------------

def _goal_aware_context(session: Session, project, smaller_than: str | None) -> str:
    """Context + instruction: derive the step from the goal and active milestone."""
    lines = [
        f"Next-action context for {project.name}:",
        f"GOAL: {project.goal}",
        f"CURRENT BOTTLENECK: {project.current_bottleneck or '(none recorded)'}",
    ]

    milestones = session.scalars(
        select(Milestone)
        .where(
            Milestone.project_id == project.id,
            Milestone.status.in_(("active", "pending")),
        )
        .order_by(Milestone.order_index, Milestone.id)
    ).all()
    active = [m for m in milestones if m.status == "active"]
    pending = [m for m in milestones if m.status == "pending"]
    if active:
        m = active[0]
        detail = f" — {m.description}" if m.description else ""
        lines.append(f"ACTIVE MILESTONE: #{m.id} {m.title}{detail}")
    elif pending:
        lines.append(
            "ACTIVE MILESTONE: none — first pending is "
            f"#{pending[0].id} {pending[0].title} (activate it with "
            "update_milestone, or treat it as the working target)."
        )
    else:
        lines.append(
            "MILESTONES: none exist yet — offer to decompose the goal "
            "(decompose_goal); meanwhile derive the step from the goal directly."
        )
    if pending and active:
        lines.append(
            "UPCOMING: " + "; ".join(f"#{m.id} {m.title}" for m in pending[:3])
        )

    tasks = session.scalars(
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.status.in_(_CONTEXT_TASK_STATUSES),
        )
        .order_by(Task.is_next.desc(), Task.id)
        .limit(_MAX_CONTEXT_TASKS)
    ).all()
    if tasks:
        lines.append(f"OPEN TASKS ({len(tasks)}):")
        lines += [
            f"- #{t.id} {t.title} ({t.status})"
            + (" (flagged next)" if t.is_next else "")
            for t in tasks
        ]
    else:
        lines.append("OPEN TASKS: (none recorded)")

    completed = session.scalars(
        select(Task)
        .where(Task.project_id == project.id, Task.status == "done")
        .order_by(Task.completed_at.desc())
        .limit(_MAX_CONTEXT_RECENT)
    ).all()
    if completed:
        lines.append("RECENTLY COMPLETED:")
        lines += [f"- {t.title}" for t in completed]

    blockers = session.scalars(
        select(Blocker)
        .where(Blocker.project_id == project.id, Blocker.status == "open")
        .order_by(Blocker.id.desc())
        .limit(_MAX_CONTEXT_RECENT)
    ).all()
    if blockers:
        lines.append("OPEN BLOCKERS:")
        lines += [
            f"- {b.description}" + (f" (because: {b.reason})" if b.reason else "")
            for b in blockers
        ]

    lines.append(
        "---\n"
        "From this, give the user ONE concrete next step. It must advance the "
        "active milestone (or clear the bottleneck if that's what blocks the "
        "milestone) — not just be the oldest task. Be concrete at the level of "
        "'open this file, first move is X'. Then say WHY it's next in terms of "
        "the milestone it advances, e.g. 'this unblocks the signup flow, which "
        "is the active milestone'. If an open task already covers this step, "
        "reference it by number; don't invent a duplicate."
    )
    if smaller_than:
        lines.append(_SHRINK_INSTRUCTION.format(smaller_than=smaller_than).strip())
    return "\n".join(lines)


# -- no-goal fallback (original behavior + a nudge) ----------------------------

def _stored_task_fallback(session: Session, project, smaller_than: str | None) -> str:
    """Original stored-task behavior, plus a prompt to set a goal."""
    task = _next_task(session, project.id)
    if task is None:
        return (
            f"No tasks are recorded for {project.name} yet. Tell the user plainly that "
            "nothing is on the list — do not invent a next action. "
            f"{_goal_nudge(project.name)}"
        )

    line = f"Next action on {project.name} (task #{task.id}): {task.title}"
    if task.detail:
        line += f" — {task.detail}"
    if smaller_than:
        line += _SHRINK_INSTRUCTION.format(smaller_than=smaller_than)
    line += f"\n{_goal_nudge(project.name)}"
    return line


def _goal_nudge(project_name: str) -> str:
    return (
        f"Note: {project_name} has no goal set, so this is just the stored task. "
        "Prompt the user for the target state and record it with set_project_goal "
        "— then next actions can be derived from where the project is headed."
    )


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
