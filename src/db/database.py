"""Database engine, schema, migrations, and context seeding for Spotter.

The public entrypoint is :func:`initialize_database`, which creates the SQLite
file (and its parent directory) if missing, applies the schema (ORM tables plus
the raw FTS5 virtual tables and triggers), runs idempotent additive migrations,
then seeds context from ``seed/context.yaml``. Seeding runs on every boot,
keyed on stable identities (project name, task title, fact key), and is
INSERT-ONLY for anything carrying live state: it bootstraps a fresh database
but never enforces a snapshot on a live one — task status, project status, the
goal layer, and runtime-modified facts are never rewritten. Everything here is
safe to re-run.
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

# Single source of truth for seeded projects + facts. This file holds personal
# context and is gitignored; copy seed/context.example.yaml to seed/context.yaml
# and fill it in. seed_context() always loads the real file below at runtime.
SEED_PATH: Path = ROOT_DIR / "seed" / "context.yaml"


@dataclass(frozen=True)
class SeedResult:
    """Per-entity insert/update/skip counts from a :func:`seed_context` run.

    ``*_skipped`` counts rows the seed deliberately left alone because they
    already exist (tasks) or were modified at runtime (facts).
    """

    projects_inserted: int = 0
    projects_updated: int = 0
    tasks_inserted: int = 0
    tasks_skipped: int = 0
    facts_inserted: int = 0
    facts_updated: int = 0
    facts_skipped: int = 0


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
        # scheduled_triggers itself is created by create_all when missing; this
        # guard covers a DB created by an intermediate build without is_prompt.
        trigger_columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(scheduled_triggers)")
        }
        if trigger_columns and "is_prompt" not in trigger_columns:
            conn.exec_driver_sql(
                "ALTER TABLE scheduled_triggers ADD COLUMN is_prompt INTEGER NOT NULL DEFAULT 0"
            )
            logger.info("Migration: added scheduled_triggers.is_prompt")
        if trigger_columns and "source" not in trigger_columns:
            conn.exec_driver_sql(
                "ALTER TABLE scheduled_triggers ADD COLUMN source TEXT NOT NULL DEFAULT 'chat'"
            )
            logger.info("Migration: added scheduled_triggers.source")
        # Goal layer: databases created before projects gained goal columns.
        # All nullable, so plain ADD COLUMN with no default is safe and nothing
        # is backfilled — goals start empty.
        project_columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(projects)")
        }
        for column in ("goal", "current_bottleneck", "goal_updated_at"):
            if column not in project_columns:
                conn.exec_driver_sql(f"ALTER TABLE projects ADD COLUMN {column} TEXT")
                logger.info("Migration: added projects.%s", column)


def load_seed(path: Path | None = None, env_yaml: str | None = None) -> dict:
    """Load seed content, preferring the file, then the env-var fallback.

    Decision order:
      1. ``seed/context.yaml`` if it exists — the file always wins (local dev).
      2. ``env_yaml`` (the SEED_CONTEXT_YAML string) — used on Railway, where the
         gitignored file is absent.
      3. Otherwise a clear error: neither source is available.
    """
    seed_path = path or SEED_PATH
    if seed_path.exists():
        with seed_path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    if env_yaml:
        return yaml.safe_load(env_yaml) or {}
    raise FileNotFoundError(
        f"No seed found: {seed_path} does not exist and SEED_CONTEXT_YAML is unset."
    )


def seed_context(session: Session, seed: dict) -> SeedResult:
    """Bootstrap projects, next-action tasks, and facts from ``seed``.

    INSERT-ONLY for live state. Keyed on stable identities:
      * projects -> matched by ``name``. Insert with seeded values; on existing
        rows only priority and description (seed-managed metadata) may change.
        ``status`` and the goal layer (goal, current_bottleneck,
        goal_updated_at) are live state and are never touched.
      * tasks    -> matched by (project, title). Insert if missing; an existing
        task — whatever its status — is skipped entirely, so a completed task
        can never be resurrected by a redeploy.
      * facts    -> matched by ``key``. Insert if missing; update from the seed
        only while the row is pristine (updated_at == created_at, i.e. never
        modified at runtime). Seed updates leave updated_at alone so the row
        stays seed-managed until something else touches it.

    Rows removed from the file are NOT deleted (no prune). Returns
    insert/update/skip counts.
    """
    counts = {
        "projects_inserted": 0,
        "projects_updated": 0,
        "tasks_inserted": 0,
        "tasks_skipped": 0,
        "facts_inserted": 0,
        "facts_updated": 0,
        "facts_skipped": 0,
    }

    for row in seed.get("projects", []):
        name = row["name"]
        project = session.scalar(select(Project).where(Project.name == name))
        if project is None:
            project = Project(
                name=name,
                status=row.get("status", "active"),
                priority=row.get("priority", 0),
                description=_clean(row.get("description")),
            )
            session.add(project)
            counts["projects_inserted"] += 1
        else:
            priority = row.get("priority", 0)
            description = _clean(row.get("description"))
            if project.priority != priority or project.description != description:
                project.priority = priority
                project.description = description
                counts["projects_updated"] += 1
        session.flush()  # ensure project.id for the next-action task

        next_action = _clean(row.get("next_action"))
        if next_action:
            _seed_next_action(session, project.id, next_action, counts)

    for row in seed.get("facts", []):
        key = row["key"]
        category = row["category"]
        content = _clean(row["content"]) or ""
        is_core = 1 if row.get("is_core") else 0
        confidence = row.get("confidence", 1.0)

        fact = session.scalar(select(WorkspaceFact).where(WorkspaceFact.key == key))
        if fact is None:
            session.add(
                WorkspaceFact(
                    key=key,
                    category=category,
                    content=content,
                    is_core=is_core,
                    confidence=confidence,
                )
            )
            counts["facts_inserted"] += 1
            continue
        if fact.updated_at != fact.created_at:
            # Modified at runtime since seeding: the live version wins.
            counts["facts_skipped"] += 1
            continue
        if (
            fact.category != category
            or fact.content != content
            or fact.is_core != is_core
            or fact.confidence != confidence
        ):
            fact.category = category
            fact.content = content
            fact.is_core = is_core
            fact.confidence = confidence
            # updated_at deliberately untouched: the row remains seed-managed.
            counts["facts_updated"] += 1

    return SeedResult(**counts)


def _seed_next_action(
    session: Session, project_id: int, title: str, counts: dict[str, int]
) -> None:
    """Insert the seeded next-action task if the project doesn't already have it.

    Matched case-insensitively by (project, title) across ALL statuses — a done
    or dropped task with this title means the seed has nothing to add. A new
    task gets is_next=1 only when no live next action exists, preserving the
    one-next-per-project convention.
    """
    existing = session.scalar(
        select(Task).where(
            Task.project_id == project_id,
            func.lower(Task.title) == title.lower(),
        )
    )
    if existing is not None:
        counts["tasks_skipped"] += 1
        return
    has_live_next = (
        session.scalar(
            select(Task.id).where(
                Task.project_id == project_id,
                Task.is_next == 1,
                Task.status.in_(("open", "in_progress")),
            )
        )
        is not None
    )
    session.add(
        Task(
            project_id=project_id,
            title=title,
            status="open",
            is_next=0 if has_live_next else 1,
        )
    )
    counts["tasks_inserted"] += 1


def _clean(value: object) -> str | None:
    """Strip a string-ish value; None when missing/empty."""
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def initialize_database(
    db_path: Path, seed_yaml: str | None = None
) -> tuple[Engine, int]:
    """Ensure the database exists, is migrated, and is seeded.

    ``seed_yaml`` is the SEED_CONTEXT_YAML fallback used when the seed file is
    absent (Railway); the file takes precedence when present. Returns the live
    engine plus the current project count.
    """
    fresh = not db_path.exists()
    engine = create_db_engine(db_path)
    apply_schema(engine)
    apply_migrations(engine)

    seed = load_seed(env_yaml=seed_yaml)
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
