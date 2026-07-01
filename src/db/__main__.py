"""CLI check for the Spotter database.

Run with ``python -m src.db``. Initializes (or opens) the database, then prints
the table list and the seeded project rows so the DB layer can be verified by
eye without any other part of the system running.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, select

from sqlalchemy.orm import Session

from ..config import load_config
from .database import initialize_database
from .models import Project


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    engine, project_count = initialize_database(config.db_path, config.seed_context_yaml)

    print(f"\nDatabase initialized at {config.db_path}")
    print(f"Seeded projects: {project_count}\n")

    tables = sorted(inspect(engine).get_table_names())
    print(f"Tables ({len(tables)}):")
    for name in tables:
        print(f"  - {name}")

    print("\nProjects:")
    with Session(engine) as session:
        projects = session.scalars(select(Project).order_by(Project.priority.desc())).all()
        for project in projects:
            print(
                f"  [{project.id}] {project.name} "
                f"(status={project.status}, priority={project.priority})"
            )


if __name__ == "__main__":
    main()
