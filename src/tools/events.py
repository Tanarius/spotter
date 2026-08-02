"""query_events — provenance-ranked retrieval over the event log.

Phase 4C: the tool that stops Spotter confidently repeating stale state. Rows
are scored ``confidence x recency-decay`` (14-day half-life on ``occurred_at``,
when the thing actually happened), so a fresh commit outranks a three-week-old
remembered sentence, and each result carries its age and source so the model
can say WHY it believes what it believes. Superseded events are excluded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from ..db.models import Event, Project
from .base import ToolContext, resolve_project

_UTC_FMT = "%Y-%m-%d %H:%M:%S"
_HALF_LIFE_DAYS = 14.0
_DEFAULT_WINDOW_DAYS = 30
_MAX_WINDOW_DAYS = 365
_MAX_RESULTS = 15
_SCAN_LIMIT = 300


def query_events(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Search what actually happened, newest-and-most-trustworthy first."""
    query = (tool_input.get("query") or "").strip().lower()
    project_name = (tool_input.get("project_name") or "").strip()
    try:
        days = int(tool_input.get("days") or _DEFAULT_WINDOW_DAYS)
    except (TypeError, ValueError):
        days = _DEFAULT_WINDOW_DAYS
    days = max(1, min(days, _MAX_WINDOW_DAYS))

    project = None
    if project_name:
        project = resolve_project(ctx.session, project_name)
        if project is None:
            return f"No project named '{project_name}' is on record."

    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime(_UTC_FMT)
    stmt = (
        select(Event)
        .where(Event.occurred_at >= since, Event.superseded_by.is_(None))
        .order_by(Event.occurred_at.desc())
        .limit(_SCAN_LIMIT)
    )
    if project is not None:
        stmt = stmt.where(Event.project_id == project.id)
    rows = ctx.session.scalars(stmt).all()

    if query:
        words = query.split()
        rows = [
            e
            for e in rows
            if all(w in f"{e.summary}\n{e.detail or ''}".lower() for w in words)
        ]
    if not rows:
        scope = f" on {project.name}" if project else ""
        hint = f" matching '{query}'" if query else ""
        return (
            f"No events{hint}{scope} in the last {days} days. State that plainly "
            "— don't fall back to older memory as if it were current."
        )

    project_names = {p.id: p.name for p in ctx.session.scalars(select(Project))}
    scored = sorted(rows, key=lambda e: -_score(e, now))[:_MAX_RESULTS]
    lines = [
        f"Events (newest + most trustworthy first; {len(scored)} of {len(rows)} "
        f"in the last {days} days):"
    ]
    for event in scored:
        name = project_names.get(event.project_id) or event.subject or "general"
        lines.append(
            f"- [{name}] {event.summary} ({_age_label(event, now)}, "
            f"{event.source}, confidence {event.confidence:g}) (event #{event.id})"
        )
    lines.append(
        "Newer, higher-confidence entries (commits, session notes) outrank "
        "older claims. Cite the age and source when the user is acting on this."
    )
    return "\n".join(lines)


def _score(event: Event, now: datetime) -> float:
    """confidence x recency decay: half-life on when the thing happened."""
    return (event.confidence or 0.5) * 0.5 ** (_age_days(event, now) / _HALF_LIFE_DAYS)


def _age_days(event: Event, now: datetime) -> float:
    try:
        occurred = datetime.strptime(event.occurred_at, _UTC_FMT).replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return 365.0
    return max(0.0, (now - occurred).total_seconds() / 86400)


def _age_label(event: Event, now: datetime) -> str:
    days = int(_age_days(event, now))
    return "today" if days == 0 else f"{days}d ago"
