"""SQLAlchemy 2.x ORM models for Spotter's SQLite database.

This is a faithful translation of ``schema.sql``. Every one of the ten real
tables is expressed as a declarative model here. The two FTS5 virtual tables and
their sync triggers cannot be expressed cleanly through the ORM, so they live as
raw DDL in :data:`FTS_STATEMENTS` and are applied alongside ``create_all`` by the
database module. Keep this file and ``schema.sql`` in lockstep.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# All timestamp columns mirror the schema: TEXT holding an ISO/SQLite datetime,
# defaulted server-side to CURRENT_TIMESTAMP so the DB owns the clock.
_NOW = text("CURRENT_TIMESTAMP")


class Base(DeclarativeBase):
    """Declarative base for every Spotter table."""


class Project(Base):
    """Top-level container. Simmer is the priority project."""

    __tablename__ = "projects"
    # AUTOINCREMENT (not just rowid) to match schema.sql exactly.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(server_default=text("'active'"))  # active | paused | done
    priority: Mapped[int] = mapped_column(server_default=text("0"))  # higher = more important
    description: Mapped[str | None]
    created_at: Mapped[str] = mapped_column(server_default=_NOW)
    updated_at: Mapped[str] = mapped_column(server_default=_NOW)


class Task(Base):
    """Concrete actionable item, linked to a project."""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_project_status", "project_id", "status"),
        # Partial index: only the rows flagged as the next action.
        Index("idx_tasks_next", "is_next", sqlite_where=text("is_next = 1")),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    title: Mapped[str]
    detail: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=text("'open'"))  # open | in_progress | done | dropped
    is_next: Mapped[int] = mapped_column(server_default=text("0"))  # 1 = this is the next action
    created_at: Mapped[str] = mapped_column(server_default=_NOW)
    updated_at: Mapped[str] = mapped_column(server_default=_NOW)
    completed_at: Mapped[str | None]


class CapturedItem(Base):
    """Thoughts, links, follow-ups — anything the user dumps. FTS-indexed."""

    __tablename__ = "captured_items"
    __table_args__ = (
        Index("idx_captured_processed", "processed", "created_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str]
    source: Mapped[str] = mapped_column(server_default=text("'telegram'"))  # telegram | voice | brief
    category: Mapped[str | None]  # thought | followup | idea | link | task_candidate
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    processed: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[str] = mapped_column(server_default=_NOW)


class Blocker(Base):
    """"Stuck on X because Y." """

    __tablename__ = "blockers"
    __table_args__ = (
        Index("idx_blockers_status", "status", "project_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    description: Mapped[str]
    reason: Mapped[str | None]
    resolution_idea: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=text("'open'"))  # open | resolved
    created_at: Mapped[str] = mapped_column(server_default=_NOW)
    resolved_at: Mapped[str | None]


class WorkspaceFact(Base):
    """Long-term persistent context (the long-term memory layer). FTS-indexed."""

    __tablename__ = "workspace_facts"
    __table_args__ = (
        Index("idx_facts_category", "category"),
        # Stable upsert identity for seeded facts (see seed/context.yaml). The
        # matching index name is created by apply_migrations() on existing DBs.
        Index("idx_facts_key", "key", unique=True),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable key for seed upserts; NULL for facts created at runtime. Unique among
    # keyed rows (SQLite treats multiple NULLs as distinct).
    key: Mapped[str | None]
    category: Mapped[str]  # context | preference | pattern | project | priority | phase2_candidate
    content: Mapped[str]
    confidence: Mapped[float] = mapped_column(server_default=text("1.0"))
    # 1 = always injected into the system prompt; 0 = reachable only via
    # query_memory. Stored as the same INTEGER 0/1 flag pattern as is_next etc.
    is_core: Mapped[int] = mapped_column(server_default=text("0"))
    last_referenced: Mapped[str | None]
    created_at: Mapped[str] = mapped_column(server_default=_NOW)
    updated_at: Mapped[str] = mapped_column(server_default=_NOW)


class ConversationLogEntry(Base):
    """Every message in/out (the working memory layer)."""

    __tablename__ = "conversation_log"
    __table_args__ = (
        # Recent-first lookups; matches the DESC index in schema.sql.
        Index("idx_convo_recent", text("created_at DESC")),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str]  # user | assistant | tool_result
    content: Mapped[str]
    tool_calls: Mapped[str | None]  # JSON if assistant made tool calls
    tokens_in: Mapped[int | None]
    tokens_out: Mapped[int | None]
    cost_cents: Mapped[float | None]
    created_at: Mapped[str] = mapped_column(server_default=_NOW)


class ScheduledTrigger(Base):
    """Proactive time-based message: a one-shot reminder or a recurring check-in.

    ``fire_at`` is UTC in SQLite CURRENT_TIMESTAMP format (``YYYY-MM-DD HH:MM:SS``)
    so it compares apples-to-apples with every other timestamp in the DB.
    ``message_or_prompt`` is sent literally when ``is_prompt`` is 0, otherwise
    handed to Claude to generate the outgoing message.
    """

    __tablename__ = "scheduled_triggers"
    __table_args__ = (
        # The firing loop's hot path: pending rows ordered by due time.
        Index("idx_triggers_pending", "status", "fire_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]  # reminder | checkin | recurring
    fire_at: Mapped[str]  # UTC, 'YYYY-MM-DD HH:MM:SS'
    recurrence: Mapped[str | None]  # daily | weekly | NULL = one-shot
    message_or_prompt: Mapped[str]
    is_prompt: Mapped[int] = mapped_column(server_default=text("0"))  # 1 = generate via Claude
    related_project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))  # pending | fired | cancelled
    source: Mapped[str] = mapped_column(server_default=text("'chat'"))  # chat | system
    created_at: Mapped[str] = mapped_column(server_default=_NOW)


class ScheduleIntent(Base):
    """V1 captures scheduling intent only; it never touches Calendar."""

    __tablename__ = "schedule_intents"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str]
    when_text: Mapped[str | None]
    duration_text: Mapped[str | None]
    attendees: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))  # pending | scheduled | dropped
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    created_at: Mapped[str] = mapped_column(server_default=_NOW)
    scheduled_at: Mapped[str | None]


class DailyBrief(Base):
    """Record of each morning brief."""

    __tablename__ = "daily_briefs"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    brief_date: Mapped[str] = mapped_column(unique=True)
    content: Mapped[str]
    top_priority: Mapped[str | None]
    delivered_at: Mapped[str] = mapped_column(server_default=_NOW)


class StallEvent(Base):
    """Record of named stalls, so the assistant doesn't repeat itself."""

    __tablename__ = "stall_events"
    __table_args__ = (
        Index("idx_stall_project_recent", "project_id", text("created_at DESC")),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # NOT NULL in schema.sql — a stall must belong to a project.
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    description: Mapped[str]
    user_response: Mapped[str | None]
    resolved: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[str] = mapped_column(server_default=_NOW)


# ---------------------------------------------------------------------------
# FTS5 virtual tables + sync triggers.
#
# SQLAlchemy's ORM has no clean way to declare an FTS5 contentless-external
# virtual table or its companion triggers, so we keep the exact DDL here and run
# it as raw SQL during init. ``IF NOT EXISTS`` makes initialization idempotent so
# re-running against an existing DB is a no-op. These mirror schema.sql verbatim.
# ---------------------------------------------------------------------------
FTS_STATEMENTS: tuple[str, ...] = (
    # captured_items full-text search.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS captured_items_fts USING fts5(
        content,
        content='captured_items',
        content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS captured_items_ai AFTER INSERT ON captured_items BEGIN
        INSERT INTO captured_items_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS captured_items_ad AFTER DELETE ON captured_items BEGIN
        INSERT INTO captured_items_fts(captured_items_fts, rowid, content) VALUES('delete', old.id, old.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS captured_items_au AFTER UPDATE ON captured_items BEGIN
        INSERT INTO captured_items_fts(captured_items_fts, rowid, content) VALUES('delete', old.id, old.content);
        INSERT INTO captured_items_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
    # workspace_facts full-text search.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS workspace_facts_fts USING fts5(
        content,
        content='workspace_facts',
        content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS workspace_facts_ai AFTER INSERT ON workspace_facts BEGIN
        INSERT INTO workspace_facts_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS workspace_facts_ad AFTER DELETE ON workspace_facts BEGIN
        INSERT INTO workspace_facts_fts(workspace_facts_fts, rowid, content) VALUES('delete', old.id, old.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS workspace_facts_au AFTER UPDATE ON workspace_facts BEGIN
        INSERT INTO workspace_facts_fts(workspace_facts_fts, rowid, content) VALUES('delete', old.id, old.content);
        INSERT INTO workspace_facts_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
)


__all__ = [
    "Base",
    "Project",
    "Task",
    "CapturedItem",
    "Blocker",
    "WorkspaceFact",
    "ConversationLogEntry",
    "ScheduleIntent",
    "DailyBrief",
    "StallEvent",
    "FTS_STATEMENTS",
]
