"""Database engine, schema, migrations, and context seeding for Spotter.

The public entrypoint is :func:`initialize_database`, which creates the SQLite
file (and its parent directory) if missing, applies the schema (ORM tables plus
the raw FTS5 virtual tables and triggers), runs idempotent additive migrations,
then upserts the seeded context from ``seed/context.yaml``. ``seed/context.yaml``
is the single source of truth for seeded projects and facts; seeding runs on
every boot and is keyed on stable identities (project name, fact key) so it never
duplicates rows and always reflects the file. Everything here is safe to re-run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import ROOT_DIR
from .models import FTS_STATEMENTS, Base, Project, Task, WorkspaceFact

logger = logging.getLogger(__name__)

# Single source of truth for seeded projects + facts.
SEED_PATH: Path = ROOT_DIR / "seed" / "context.yaml"


@dataclass(frozen=True)
class SeedResult:
    """Per-entity insert/update counts from a :func:`seed_context` run."""

    projects_inserted: int = 0
    projects_updated: int = 0
    tasks_inserted: int = 0
    tasks_updated: int = 0
    facts_inserted: int = 0
    facts_updated: int = 0


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


def apply_migrations(engine: Engine) -> None:
    """Apply idempotent, purely additive schema migrations to existing DBs.

    ``create_all`` only creates missing *tables*; it never adds columns to a
    table that already exists. So a database created before ``workspace_facts``
    gained ``is_core``/``key`` needs these ALTERs. Each is guarded by a
    ``PRAGMA table_info`` check, and the index uses ``IF NOT EXISTS`` — re-running
    is a clean no-op, and nothing is ever dropped or rewritten.
    """
    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(workspace_facts)")}
        if "is_core" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE workspace_facts ADD COLUMN is_core INTEGER NOT NULL DEFAULT 0"
            )
            logger.info("Migration: added workspace_facts.is_core")
        if "key" not in columns:
            conn.exec_driver_sql("ALTER TABLE workspace_facts ADD COLUMN key TEXT")
            logger.info("Migration: added workspace_facts.key")
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_key ON workspace_facts(key)"
        )


def load_seed(path: Path | None = None) -> dict:
    """Parse the seed YAML file into a dict (``{}`` if empty)."""
    seed_path = path or SEED_PATH
    if not seed_path.exists():
        raise FileNotFoundError(f"Seed file not found at {seed_path}")
    with seed_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def seed_context(session: Session, seed: dict) -> SeedResult:
    """Upsert projects, their next-action tasks, and facts from ``seed``.

    Read-then-write per row, keyed on stable identities:
      * projects   -> matched by ``name``
      * next action-> the project's is_next task, matched by (project_id, is_next)
      * facts      -> matched by ``key``

    Idempotent: editing a row's wording in the seed file updates the existing row
    rather than creating a duplicate. Rows removed from the file are NOT deleted
    (no prune). Returns insert/update counts.
    """
    counts = {
        "projects_inserted": 0,
        "projects_updated": 0,
        "tasks_inserted": 0,
        "tasks_updated": 0,
        "facts_inserted": 0,
        "facts_updated": 0,
    }

    for row in seed.get("projects", []):
        name = row["name"]
        project = session.scalar(select(Project).where(Project.name == name))
        if project is None:
            project = Project(name=name)
            session.add(project)
            counts["projects_inserted"] += 1
        else:
            counts["projects_updated"] += 1
        project.status = row.get("status", "active")
        project.priority = row.get("priority", 0)
        project.description = _clean(row.get("description"))
        session.flush()  # ensure project.id for the next-action task

        next_action = _clean(row.get("next_action"))
        if next_action:
            _upsert_next_action(session, project.id, next_action, counts)

    for row in seed.get("facts", []):
        key = row["key"]
        fact = session.scalar(select(WorkspaceFact).where(WorkspaceFact.key == key))
        if fact is None:
            fact = WorkspaceFact(key=key)
            session.add(fact)
            counts["facts_inserted"] += 1
        else:
            counts["facts_updated"] += 1
        fact.category = row["category"]
        fact.content = _clean(row["content"]) or ""
        fact.is_core = 1 if row.get("is_core") else 0
        fact.confidence = row.get("confidence", 1.0)

    return SeedResult(**counts)


def _upsert_next_action(
    session: Session, project_id: int, title: str, counts: dict[str, int]
) -> None:
    """Upsert the project's single is_next task (one next action per project)."""
    task = session.scalar(
        select(Task).where(Task.project_id == project_id, Task.is_next == 1)
    )
    if task is None:
        task = Task(project_id=project_id, is_next=1, status="open", title=title)
        session.add(task)
        counts["tasks_inserted"] += 1
    else:
        task.title = title
        counts["tasks_updated"] += 1


def _clean(value: object) -> str | None:
    """Strip a string-ish value; None when missing/empty."""
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def initialize_database(db_path: Path) -> tuple[Engine, int]:
    """Ensure the database exists, is migrated, and is seeded from the file.

    Returns the live engine plus the current project count.
    """
    fresh = not db_path.exists()
    engine = create_db_engine(db_path)
    apply_schema(engine)
    apply_migrations(engine)

    seed = load_seed()
    with Session(engine) as session, session.begin():
        result = seed_context(session, seed)
        project_count = session.scalar(select(func.count()).select_from(Project)) or 0

    logger.info(
        "Database initialized at %s (%s); %d projects. Seed: %s",
        db_path,
        "created" if fresh else "existing",
        project_count,
        result,
    )
    return engine, project_count


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a configured ``Session`` factory bound to ``engine``.

    Provided for later steps that need to open sessions against the database.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
