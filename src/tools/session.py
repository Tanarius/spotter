"""start_session / end_session — warm starts and clean stops (phase 4F).

The highest-leverage interaction pattern for a stall-prone workflow:
"Starting on Simmer" pulls where you left off (last session note), what's
moved since (recent events), the active milestone, the next action, and open
blockers — everything layer 1 made knowable. "Done for now" writes where you
stopped back into the event log so the NEXT start is warm.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..db.models import Blocker, Event, Milestone
from ..tools.next_action import _next_task
from .base import ToolContext, resolve_project, utc_now_str

_RECENT_EVENTS = 6
_USER_SESSION_CONFIDENCE = 0.85  # user-stated status: above chat, below commits


def start_session(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Assemble the warm-start context for a work session on a project."""
    session = ctx.session
    project = resolve_project(session, tool_input.get("project_name"))
    if project is None:
        return "No project by that name (and no active project to default to)."

    lines = [f"Session start context for {project.name}:"]
    if project.goal:
        lines.append(f"GOAL: {project.goal}")
    if project.current_bottleneck:
        lines.append(f"BOTTLENECK: {project.current_bottleneck}")

    milestone = session.scalar(
        select(Milestone)
        .where(Milestone.project_id == project.id, Milestone.status == "active")
        .order_by(Milestone.order_index, Milestone.id)
    )
    if milestone is not None:
        lines.append(f"ACTIVE MILESTONE: {milestone.title}")

    last_note = session.scalar(
        select(Event)
        .where(
            Event.project_id == project.id,
            Event.kind == "session_note",
            Event.superseded_by.is_(None),
        )
        .order_by(Event.occurred_at.desc())
    )
    if last_note is not None:
        lines.append(f"LAST SESSION ({last_note.occurred_at} UTC):")
        lines.append(last_note.detail or last_note.summary)

    recent = session.scalars(
        select(Event)
        .where(
            Event.project_id == project.id,
            Event.kind != "session_note",
            Event.superseded_by.is_(None),
        )
        .order_by(Event.occurred_at.desc())
        .limit(_RECENT_EVENTS)
    ).all()
    if recent:
        lines.append("SINCE THEN / RECENT:")
        lines += [f"- {e.summary} ({e.occurred_at} UTC, {e.source})" for e in recent]

    task = _next_task(session, project.id)
    if task is not None:
        lines.append(f"NEXT ACTION ON RECORD: #{task.id} {task.title}")

    blockers = session.scalars(
        select(Blocker)
        .where(Blocker.project_id == project.id, Blocker.status == "open")
        .order_by(Blocker.id.desc())
        .limit(3)
    ).all()
    if blockers:
        lines.append("OPEN BLOCKERS:")
        lines += [f"- {b.description}" for b in blockers]

    lines.append(
        "---\nGive the user a warm start in a few short lines: where they left "
        "off, what's moved since, and the ONE concrete thing to do first. No "
        "pep talk — point at the work."
    )
    return "\n".join(lines)


def end_session(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Record where the user stopped, so the next session starts warm."""
    note = (tool_input.get("note") or "").strip()
    if not note:
        return (
            "Capture at least one line about where things stand before closing "
            "out — what happened, what's next."
        )
    session = ctx.session
    project = resolve_project(session, tool_input.get("project_name"))
    if project is None:
        return "No project by that name (and no active project to default to)."

    next_step = (tool_input.get("next") or "").strip()
    detail = f"WORKED ON: {note}"
    if next_step:
        detail += f"\nNEXT: {next_step}"
    event = Event(
        source="user_chat",
        kind="session_note",
        project_id=project.id,
        summary=f"Session end on {project.name}: {note.splitlines()[0][:160]}",
        detail=detail,
        confidence=_USER_SESSION_CONFIDENCE,
        occurred_at=utc_now_str(),
    )
    session.add(event)
    session.flush()
    return (
        f"Session on {project.name} closed and remembered (event #{event.id}). "
        "It'll be the warm-start context next time. Confirm briefly — no recap "
        "needed."
    )
