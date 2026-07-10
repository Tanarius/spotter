"""Shared types for Spotter tool handlers.

A handler takes a :class:`ToolContext` (an open, soon-to-be-committed session
plus config) and the tool's already-parsed ``input`` dict, and returns a string.
That string becomes the ``tool_result`` content Claude reads on the next turn —
so write it for the model, concise and unambiguous.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Config
from ..db.models import Project


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool handler needs to do its work.

    ``register_trigger`` (when the bot is live) arms a scheduled_triggers row
    with the running scheduler: ``(trigger_id, due_utc) -> None``. Handlers must
    not call it directly — append a closure to ``post_commit`` instead, which
    the brain runs only after the transaction commits, so a job can never fire
    before its row exists. Both are optional: without them, created triggers
    still persist and arm on the next boot via register_pending.
    """

    session: Session
    config: Config
    register_trigger: Callable[[int, datetime], None] | None = None
    post_commit: list[Callable[[], None]] = field(default_factory=list)


# A tool handler: (context, parsed tool input) -> result text for Claude.
ToolHandler = Callable[[ToolContext, dict[str, Any]], str]


def resolve_project(session: Session, name: str | None) -> Project | None:
    """Resolve a project for a tool call.

    With a ``name``, match it case-insensitively (``None`` if no such project).
    Without one, fall back to the highest-priority active project — Spotter's
    default focus, which is Simmer.
    """
    if name:
        return session.scalar(
            select(Project).where(func.lower(Project.name) == name.strip().lower())
        )
    return session.scalar(
        select(Project)
        .where(Project.status == "active")
        .order_by(Project.priority.desc(), Project.id)
    )


def resolve_project_id(session: Session, name: str | None) -> int | None:
    """Resolve a project id by name, or ``None`` when the name is absent/unknown.

    Unlike :func:`resolve_project`, this does **not** fall back to a default
    project — a blocker or intent with no stated project stays unlinked rather
    than being silently attached to Simmer.
    """
    if not name:
        return None
    return session.scalar(
        select(Project.id).where(func.lower(Project.name) == name.strip().lower())
    )
