"""Database engine, schema initialization, and first-run seeding for Spotter.

The public entrypoint is :func:`initialize_database`, which creates the SQLite
file (and its parent directory) if missing, applies the full schema — ORM tables
plus the raw FTS5 virtual tables and triggers — and seeds the known starting
context exactly once. Everything is idempotent: calling it against an existing,
already-seeded database is a no-op that just hands back a working engine.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import FTS_STATEMENTS, Base, Project, WorkspaceFact

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Seed data — the known starting context for a fresh database.
# ---------------------------------------------------------------------------
# priority is an integer where higher = more important. Simmer outranks Runedex.
_SEED_PROJECTS: tuple[dict[str, object], ...] = (
    {
        "name": "Simmer",
        "status": "active",
        "priority": 10,
        "description": "The priority project to ship. Highest priority.",
    },
    {
        "name": "Runedex",
        "status": "active",
        "priority": 5,
        "description": "AWS deck app (Runedex). Active but lower priority than Simmer.",
    },
)

# Long-term context facts about the user (the workspace_facts memory layer).
_SEED_FACTS: tuple[dict[str, object], ...] = (
    {
        "category": "context",
        "content": (
            "User is an infrastructure engineer moving into AI / agent work."
        ),
        "confidence": 1.0,
    },
    {
        "category": "pattern",
        "content": (
            "User has a documented 70-80% stall pattern: projects tend to stall "
            "before shipping. Spotter exists to catch and name those stalls."
        ),
        "confidence": 1.0,
    },
    {
        "category": "project",
        "content": "Simmer is the priority project to ship.",
        "confidence": 1.0,
    },
)


def _enable_sqlite_fks(dbapi_connection: object, connection_record: object) -> None:
    """Turn on SQLite foreign-key enforcement for every connection.

    SQLite ignores FK constraints unless this PRAGMA is set per-connection.
    """
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_db_engine(db_path: Path) -> Engine:
    """Create an engine for ``db_path``, ensuring its parent directory exists."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # as_posix() keeps the sqlite URL well-formed on Windows paths.
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    event.listen(engine, "connect", _enable_sqlite_fks)
    return engine


def apply_schema(engine: Engine) -> None:
    """Create all ORM tables, then the FTS5 virtual tables and triggers.

    Ordering matters: the FTS tables and triggers reference the content tables,
    so the ORM tables must exist first. Both halves are idempotent.
    """
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for statement in FTS_STATEMENTS:
            conn.exec_driver_sql(statement)


def seed_initial_data(session: Session) -> int:
    """Seed projects and workspace facts if the database is empty.

    Returns the total number of projects present afterwards. Seeding is gated on
    an empty ``projects`` table so re-runs never duplicate rows.
    """
    existing = session.scalar(select(func.count()).select_from(Project)) or 0
    if existing:
        logger.info("Projects already present (%d); skipping seed.", existing)
        return existing

    session.add_all(Project(**row) for row in _SEED_PROJECTS)
    session.add_all(WorkspaceFact(**row) for row in _SEED_FACTS)
    session.flush()

    count = session.scalar(select(func.count()).select_from(Project)) or 0
    logger.info("Seeded %d projects and %d workspace facts.", count, len(_SEED_FACTS))
    return count


def initialize_database(db_path: Path) -> tuple[Engine, int]:
    """Ensure the database exists, is fully migrated, and is seeded.

    Returns the live engine plus the number of seeded projects.
    """
    fresh = not db_path.exists()
    engine = create_db_engine(db_path)
    apply_schema(engine)

    with Session(engine) as session, session.begin():
        project_count = seed_initial_data(session)

    logger.info(
        "Database initialized at %s (%s); %d projects.",
        db_path,
        "created" if fresh else "existing",
        project_count,
    )
    return engine, project_count


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a configured ``Session`` factory bound to ``engine``.

    Provided for later steps that need to open sessions against the database.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
