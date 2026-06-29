"""Shared types for Spotter tool handlers.

A handler takes a :class:`ToolContext` (an open, soon-to-be-committed session
plus config) and the tool's already-parsed ``input`` dict, and returns a string.
That string becomes the ``tool_result`` content Claude reads on the next turn —
so write it for the model, concise and unambiguous.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Config
from ..db.models import Project


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool handler needs to do its work."""

    session: Session
    config: Config


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
