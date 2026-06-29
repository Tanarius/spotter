"""Spotter entrypoint."""

from __future__ import annotations

import logging

import anthropic

from .bot import run_bot
from .brain import Brain
from .config import load_config
from .db import initialize_database, make_session_factory

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()

    engine, project_count = initialize_database(config.db_path)
    session_factory = make_session_factory(engine)
    logger.info("Database ready with %d seeded projects", project_count)

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    brain = Brain(config, client, session_factory)

    print("Spotter alive")
    run_bot(config, brain)


if __name__ == "__main__":
    main()
