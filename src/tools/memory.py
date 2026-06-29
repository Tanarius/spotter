"""query_memory — search captured items, facts, tasks, and blockers.

captured_items and workspace_facts have FTS5 indexes, so those scopes use
``MATCH``. tasks and blockers have no FTS table, so they fall back to a ``LIKE``
substring search. ``scope='all'`` searches every layer and returns the top
matches up to ``limit``.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .base import ToolContext

_DEFAULT_LIMIT = 5


def query_memory(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Search the requested memory layer(s) and return formatted top matches."""
    query = (tool_input.get("query") or "").strip()
    scope = tool_input.get("scope") or "all"
    limit = tool_input.get("limit") or _DEFAULT_LIMIT
    if not query:
        return "No search query provided."

    session = ctx.session
    results: list[str] = []
    if scope in ("captured", "all"):
        results += _search_captured(session, query, limit)
    if scope in ("facts", "all"):
        results += _search_facts(session, query, limit)
    if scope in ("tasks", "all"):
        results += _search_tasks(session, query, limit)
    if scope in ("blockers", "all"):
        results += _search_blockers(session, query, limit)

    if not results:
        return f'No matches for "{query}" in scope "{scope}".'
    return "\n".join(results[:limit])


def _to_fts_query(raw: str) -> str | None:
    """Turn free text into a safe FTS5 OR-query of quoted word tokens.

    Quoting each token sidesteps FTS5 operator/syntax errors on punctuation, and
    OR favors recall over an implicit-AND match.
    """
    tokens = re.findall(r"[A-Za-z0-9]+", raw)
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens)


def _search_captured(session: Session, query: str, limit: int) -> list[str]:
    fts = _to_fts_query(query)
    if not fts:
        return []
    rows = session.execute(
        text(
            "SELECT ci.id AS id, ci.category AS category, ci.content AS content "
            "FROM captured_items ci "
            "JOIN captured_items_fts ON captured_items_fts.rowid = ci.id "
            "WHERE captured_items_fts MATCH :q ORDER BY rank LIMIT :n"
        ),
        {"q": fts, "n": limit},
    ).all()
    return [
        f"[captured #{r.id}] ({r.category or 'uncategorized'}) {r.content}" for r in rows
    ]


def _search_facts(session: Session, query: str, limit: int) -> list[str]:
    fts = _to_fts_query(query)
    if not fts:
        return []
    rows = session.execute(
        text(
            "SELECT wf.id AS id, wf.category AS category, wf.content AS content "
            "FROM workspace_facts wf "
            "JOIN workspace_facts_fts ON workspace_facts_fts.rowid = wf.id "
            "WHERE workspace_facts_fts MATCH :q ORDER BY rank LIMIT :n"
        ),
        {"q": fts, "n": limit},
    ).all()
    return [f"[fact #{r.id}] ({r.category}) {r.content}" for r in rows]


def _search_tasks(session: Session, query: str, limit: int) -> list[str]:
    like = f"%{query}%"
    rows = session.execute(
        text(
            "SELECT id, title, detail, status FROM tasks "
            "WHERE title LIKE :p OR detail LIKE :p ORDER BY id DESC LIMIT :n"
        ),
        {"p": like, "n": limit},
    ).all()
    return [f"[task #{r.id}] ({r.status}) {r.title}" for r in rows]


def _search_blockers(session: Session, query: str, limit: int) -> list[str]:
    like = f"%{query}%"
    rows = session.execute(
        text(
            "SELECT id, description, reason, status FROM blockers "
            "WHERE description LIKE :p OR reason LIKE :p OR resolution_idea LIKE :p "
            "ORDER BY id DESC LIMIT :n"
        ),
        {"p": like, "n": limit},
    ).all()
    return [f"[blocker #{r.id}] ({r.status}) {r.description}" for r in rows]
