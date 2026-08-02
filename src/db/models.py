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
    # Goal layer (added post-schema.sql; apply_migrations ALTERs these onto
    # existing databases). All nullable — goals start empty, nothing backfilled.
    goal: Mapped[str | None]  # target state in plain language
    current_bottleneck: Mapped[str | None]  # the single most-blocking thing right now
    goal_updated_at: Mapped[str | None]
    # GitHub repo mapped to this project ("owner/name" or just "name"),
    # matched case-insensitively by the webhook ingester. Nullable; the
    # ingester also falls back to repo-name == project-name.
    github_repo: Mapped[str | None]
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
    source: Mapped[str] = mapped_column(server_default=text("'telegram'"))  # telegram | voice | brief | dashboard
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


class Event(Base):
    """The event log: what actually happened, with provenance and recency.

    The memory-layer backbone (goal-layer phase 4): every piece of knowledge
    carries WHERE it came from (``source``), WHEN it actually happened
    (``occurred_at``, distinct from ``recorded_at``), and HOW MUCH to trust it
    (``confidence`` — a commit is fact, a remembered sentence is a claim).
    ``superseded_by`` lets newer information explicitly retire older. Added
    post-schema.sql; ``create_all`` creates it on existing databases.
    """

    __tablename__ = "events"
    __table_args__ = (
        Index("idx_events_project_time", "project_id", text("occurred_at DESC")),
        Index("idx_events_source_time", "source", text("occurred_at DESC")),
        # Dedupe key for redelivered external events (webhook delivery ids).
        Index("idx_events_external", "source", "external_id", unique=True),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str]  # github | claude_code | user_chat | user_dashboard | inferred
    kind: Mapped[str]  # push | pull_request | session_note | ...
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    # Free-form entity when no project matched (e.g. the repo full name).
    subject: Mapped[str | None]
    summary: Mapped[str]  # one-line human/model-readable statement of the event
    detail: Mapped[str | None]  # fuller extract (commit list, notes)
    confidence: Mapped[float] = mapped_column(server_default=text("1.0"))
    occurred_at: Mapped[str]  # UTC 'YYYY-MM-DD HH:MM:SS' — when it happened
    recorded_at: Mapped[str] = mapped_column(server_default=_NOW)
    external_id: Mapped[str | None]  # e.g. GitHub delivery id, for dedupe
    superseded_by: Mapped[int | None] = mapped_column(ForeignKey("events.id"))


class EmbeddingRow(Base):
    """Stored embedding vector for a memory row (phase 4D).

    ``kind`` + ``ref_id`` point at the embedded row (events now; conversation
    turns later). ``content_hash`` detects content drift so changed rows get
    re-embedded; ``vector`` is packed little-endian float32. SQLite is the
    vector store on purpose: single-user scale is a few hundred rows, and a
    pure-Python dot product over them costs microseconds.
    """

    __tablename__ = "embeddings"
    __table_args__ = (
        Index("idx_embeddings_ref", "kind", "ref_id", unique=True),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]  # event | conversation
    ref_id: Mapped[int]
    content_hash: Mapped[str]
    model: Mapped[str]
    dim: Mapped[int]
    vector: Mapped[bytes]
    created_at: Mapped[str] = mapped_column(server_default=_NOW)


class Milestone(Base):
    """A step between a project's current state and its goal.

    Ordered by ``order_index``; at most one should be ``active`` per project at
    a time (the one being worked toward). Added after schema.sql was finalized,
    so like job_applications it exists only here and ``create_all`` creates it
    on existing databases.
    """

    __tablename__ = "milestones"
    __table_args__ = (
        Index("idx_milestones_project", "project_id", "status", "order_index"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    title: Mapped[str]
    description: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))  # pending | active | done | dropped
    order_index: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[str] = mapped_column(server_default=_NOW)
    completed_at: Mapped[str | None]


class JobApplication(Base):
    """Job-search pipeline entry, managed from the web dashboard.

    Added after schema.sql was finalized, so unlike the original ten tables it
    exists only here; ``create_all`` creates it on existing databases (the same
    additive path scheduled_triggers used).
    """

    __tablename__ = "job_applications"
    __table_args__ = (
        Index("idx_job_apps_status", "status", "date_applied"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company: Mapped[str]
    role: Mapped[str]
    source: Mapped[str | None]  # where it was found/submitted (LinkedIn, referral, ...)
    # applied | responded | screen | interview | offer | rejected | ghosted
    status: Mapped[str] = mapped_column(server_default=text("'applied'"))
    date_applied: Mapped[str]  # local date, 'YYYY-MM-DD'
    notes: Mapped[str | None]
    created_at: Mapped[str] = mapped_column(server_default=_NOW)
    updated_at: Mapped[str] = mapped_column(server_default=_NOW)


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
    "JobApplication",
    "Milestone",
    "Event",
    "EmbeddingRow",
    "FTS_STATEMENTS",
]
