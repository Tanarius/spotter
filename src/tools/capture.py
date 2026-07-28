"""capture_item — save anything the user dumps into captured_items."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from ..db.models import CapturedItem, Project
from .base import ToolContext

# Mirrors the enum in tools_schema.json; used to default unknown categories.
_DEFAULT_CATEGORY = "thought"


def capture_item(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Insert a captured item and return a short confirmation for Claude."""
    content = (tool_input.get("content") or "").strip()
    if not content:
        return "Nothing to capture — no content provided."

    category = tool_input.get("category") or _DEFAULT_CATEGORY
    project_name = tool_input.get("project_name")
    project_id = _resolve_project_id(ctx, project_name)

    # 'source' is an internal caller field (the dashboard passes 'dashboard'),
    # not part of the model-facing schema — Claude's calls never set it.
    item = CapturedItem(
        content=content,
        category=category,
        source=tool_input.get("source") or "telegram",
        project_id=project_id,
    )
    ctx.session.add(item)
    ctx.session.flush()  # populate item.id within the transaction

    where = ""
    if project_name:
        where = f" under {project_name}" if project_id else f" (project '{project_name}' not found, saved unlinked)"
    return f"Captured #{item.id} as '{category}'{where}: {content}"


def _resolve_project_id(ctx: ToolContext, project_name: str | None) -> int | None:
    """Case-insensitive lookup of a project id by name; None if absent/unknown."""
    if not project_name:
        return None
    return ctx.session.scalar(
        select(Project.id).where(func.lower(Project.name) == project_name.strip().lower())
    )
