"""Goal-layer tools: set_project_goal, decompose_goal, update_milestone,
set_bottleneck.

``decompose_goal`` is a two-phase tool, following the same convention as
``surface_next_action``'s ``smaller_than``: the handler stays DB-only and the
reasoning happens in the brain's tool loop. Phase 1 (no ``milestones`` input)
returns the project's goal, bottleneck, open tasks, recent activity, and
existing milestones plus an instruction to think and call back. Phase 2 (with
``milestones``) writes the model's ordered set, replacing unfinished ones.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Blocker, CapturedItem, Milestone, Project, Task
from .base import ToolContext, resolve_project, utc_now_str

_MILESTONE_STATUSES = ("pending", "active", "done", "dropped")
# Live task statuses shown in decomposition context (mirrors tools/status.py).
_LIVE_TASK_STATUSES = ("open", "in_progress", "paused", "waiting")
_MAX_MILESTONES = 8
_MAX_CONTEXT_TASKS = 15
_MAX_CONTEXT_RECENT = 5
_MAX_CANDIDATES = 5


# -- set_project_goal ----------------------------------------------------------

def set_project_goal(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Set a project's goal (and optionally its bottleneck)."""
    goal = (tool_input.get("goal") or "").strip()
    if not goal:
        return "Can't set an empty goal — provide the target state in plain language."

    project = resolve_project(ctx.session, tool_input.get("project_name"))
    if project is None:
        return _no_project(tool_input.get("project_name"))

    project.goal = goal
    project.goal_updated_at = utc_now_str()
    project.updated_at = utc_now_str()
    parts = [f"Goal set on {project.name}: {goal}."]
    bottleneck = (tool_input.get("current_bottleneck") or "").strip()
    if bottleneck:
        project.current_bottleneck = bottleneck
        parts.append(f"Bottleneck: {bottleneck}.")

    live = _live_milestones(ctx.session, project.id)
    if not live:
        parts.append(
            "No milestones exist for this project yet — offer to decompose the "
            "goal into milestones (decompose_goal)."
        )
    return " ".join(parts)


# -- decompose_goal ------------------------------------------------------------

def decompose_goal(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Phase 1: return decomposition context. Phase 2: write the milestones."""
    project = resolve_project(ctx.session, tool_input.get("project_name"))
    if project is None:
        return _no_project(tool_input.get("project_name"))
    if not (project.goal or "").strip():
        return (
            f"{project.name} has no goal set. Ask the user for the target state, "
            "set it with set_project_goal, then decompose."
        )

    milestones_input = tool_input.get("milestones")
    if not milestones_input:
        return _decomposition_context(ctx.session, project)
    return _write_milestones(ctx.session, project, milestones_input)


def _decomposition_context(session: Session, project: Project) -> str:
    """Everything the model needs to reason from current state to the goal."""
    lines = [
        f"Decomposition context for {project.name} "
        f"(status {project.status}, priority {project.priority}):",
        f"GOAL: {project.goal}",
        f"CURRENT BOTTLENECK: {project.current_bottleneck or '(none recorded)'}",
    ]

    open_tasks = session.scalars(
        select(Task)
        .where(Task.project_id == project.id, Task.status.in_(_LIVE_TASK_STATUSES))
        .order_by(Task.is_next.desc(), Task.id)
        .limit(_MAX_CONTEXT_TASKS)
    ).all()
    lines.append(f"OPEN TASKS ({len(open_tasks)}):")
    lines += [f"- #{t.id} {t.title} ({t.status})" for t in open_tasks] or ["- (none)"]

    completed = session.scalars(
        select(Task)
        .where(Task.project_id == project.id, Task.status == "done")
        .order_by(Task.completed_at.desc())
        .limit(_MAX_CONTEXT_RECENT)
    ).all()
    if completed:
        lines.append("RECENTLY COMPLETED:")
        lines += [f"- {t.title} ({t.completed_at or 'done'})" for t in completed]

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

    captures = session.scalars(
        select(CapturedItem)
        .where(CapturedItem.project_id == project.id)
        .order_by(CapturedItem.id.desc())
        .limit(_MAX_CONTEXT_RECENT)
    ).all()
    if captures:
        lines.append("RECENT CAPTURES:")
        lines += [f"- {c.content}" for c in captures]

    existing = _all_milestones(session, project.id)
    if existing:
        lines.append("EXISTING MILESTONES:")
        lines += [f"- #{m.id} [{m.status}] {m.title}" for m in existing]

    lines.append(
        "---\n"
        "Now identify what actually stands between the current state above and "
        "the goal — do not just restate the goal. Produce 3-6 ordered "
        "milestones, each a concrete verifiable state, ordered by what must "
        "happen first; the first is the one to work toward right now. Then call "
        "decompose_goal again with the same project_name and the milestones "
        "array to write them. Writing replaces existing unfinished milestones "
        "(done ones are kept)."
    )
    return "\n".join(lines)


def _write_milestones(
    session: Session, project: Project, milestones_input: Any
) -> str:
    """Replace the project's unfinished milestones with the model's ordered set."""
    if not isinstance(milestones_input, list):
        return "milestones must be an array of {title, description} objects."
    cleaned: list[tuple[str, str | None]] = []
    for item in milestones_input:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        description = str(item.get("description") or "").strip() or None
        cleaned.append((title, description))
    if not cleaned:
        return "No valid milestones provided — each needs at least a title."
    truncated = len(cleaned) > _MAX_MILESTONES
    cleaned = cleaned[:_MAX_MILESTONES]

    replaced = _live_milestones(session, project.id)
    for old in replaced:
        old.status = "dropped"
    for index, (title, description) in enumerate(cleaned):
        session.add(
            Milestone(
                project_id=project.id,
                title=title,
                description=description,
                status="active" if index == 0 else "pending",
                order_index=index,
            )
        )
    session.flush()

    lines = [
        f"Milestones set for {project.name} ({len(cleaned)} written"
        + (f", {len(replaced)} unfinished replaced" if replaced else "")
        + ("; extra beyond the first 8 were dropped" if truncated else "")
        + "):"
    ]
    lines += [
        f"{i + 1}. {'[active] ' if i == 0 else ''}{title}"
        for i, (title, _) in enumerate(cleaned)
    ]
    lines.append("Give the user the plan in one short message.")
    return "\n".join(lines)


# -- update_milestone ----------------------------------------------------------

def update_milestone(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Change a milestone's status, keeping at most one active per project."""
    status = (tool_input.get("status") or "").strip().lower()
    if status not in _MILESTONE_STATUSES:
        return (
            f"Invalid milestone status '{status}'. "
            f"Valid: {', '.join(_MILESTONE_STATUSES)}."
        )

    milestone, ambiguity = _resolve_milestone(
        ctx.session, tool_input.get("id"), (tool_input.get("title") or "").strip()
    )
    if ambiguity:
        return ambiguity
    if milestone is None:
        ref = (
            f"#{tool_input.get('id')}"
            if tool_input.get("id") is not None
            else f"'{tool_input.get('title')}'"
        )
        return f"No milestone {ref} found."

    project = ctx.session.get(Project, milestone.project_id)
    project_name = project.name if project else f"project #{milestone.project_id}"
    old = milestone.status
    was_active = old == "active"
    milestone.status = status
    if status == "done":
        milestone.completed_at = utc_now_str()
    elif old == "done":
        milestone.completed_at = None  # reopened

    notes = []
    if status == "active":
        # Enforce at-most-one-active: demote any other active milestone.
        others = ctx.session.scalars(
            select(Milestone).where(
                Milestone.project_id == milestone.project_id,
                Milestone.status == "active",
                Milestone.id != milestone.id,
            )
        ).all()
        for other in others:
            other.status = "pending"
        if others:
            notes.append(
                "Demoted to pending: "
                + ", ".join(f"'{o.title}'" for o in others)
                + " (one active milestone per project)."
            )
    elif status == "done" or (status == "dropped" and was_active):
        # The active slot opened up: promote the next pending milestone.
        next_pending = ctx.session.scalar(
            select(Milestone)
            .where(
                Milestone.project_id == milestone.project_id,
                Milestone.status == "pending",
            )
            .order_by(Milestone.order_index, Milestone.id)
        )
        if next_pending is not None and not _has_active(
            ctx.session, milestone.project_id
        ):
            next_pending.status = "active"
            notes.append(f"Now active: '{next_pending.title}' (#{next_pending.id}).")

    reply = (
        f"Milestone #{milestone.id} '{milestone.title}' [{project_name}]: "
        f"{old} -> {status}."
    )
    return " ".join([reply, *notes])


def _resolve_milestone(
    session: Session, raw_id: Any, title: str
) -> tuple[Milestone | None, str | None]:
    """Return (milestone, None), (None, None) for no match, or (None, ambiguity)."""
    if raw_id is not None:
        return session.get(Milestone, int(raw_id)), None
    if not title:
        return None, "Provide an id or a title to identify the milestone."

    lowered = title.lower()
    exact = session.scalars(
        select(Milestone)
        .where(func.lower(Milestone.title) == lowered)
        .order_by(Milestone.id)
    ).all()
    matches = exact or session.scalars(
        select(Milestone)
        .where(func.lower(Milestone.title).like(f"%{lowered}%"))
        .order_by(Milestone.id)
        .limit(_MAX_CANDIDATES + 1)
    ).all()

    if not matches:
        return None, None
    if len(matches) == 1:
        return matches[0], None
    live = [m for m in matches if m.status in ("pending", "active")]
    if len(live) == 1:
        return live[0], None
    lines = "\n".join(
        f"- #{m.id} '{m.title}' ({m.status})" for m in matches[:_MAX_CANDIDATES]
    )
    return None, f"Multiple milestones match '{title}' — retry with the exact id:\n{lines}"


# -- set_bottleneck ------------------------------------------------------------

def set_bottleneck(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Record the single most-blocking thing on a project."""
    bottleneck = (tool_input.get("bottleneck") or "").strip()
    if not bottleneck:
        return "Can't record an empty bottleneck — say what's actually in the way."

    project = resolve_project(ctx.session, tool_input.get("project_name"))
    if project is None:
        return _no_project(tool_input.get("project_name"))

    previous = project.current_bottleneck
    project.current_bottleneck = bottleneck
    project.updated_at = utc_now_str()
    if previous and previous != bottleneck:
        return f"Bottleneck on {project.name}: {bottleneck} (was: {previous})."
    return f"Bottleneck on {project.name}: {bottleneck}."


# -- shared helpers ------------------------------------------------------------

def _no_project(project_name: Any) -> str:
    if project_name:
        return f"No project named '{project_name}' is on record."
    return "No active project to work with."


def _live_milestones(session: Session, project_id: int) -> list[Milestone]:
    return list(
        session.scalars(
            select(Milestone)
            .where(
                Milestone.project_id == project_id,
                Milestone.status.in_(("pending", "active")),
            )
            .order_by(Milestone.order_index, Milestone.id)
        )
    )


def _all_milestones(session: Session, project_id: int) -> list[Milestone]:
    return list(
        session.scalars(
            select(Milestone)
            .where(Milestone.project_id == project_id)
            .order_by(Milestone.order_index, Milestone.id)
        )
    )


def _has_active(session: Session, project_id: int) -> bool:
    return (
        session.scalar(
            select(Milestone.id).where(
                Milestone.project_id == project_id, Milestone.status == "active"
            )
        )
        is not None
    )
