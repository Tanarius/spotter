"""Spotter entrypoint."""

from __future__ import annotations

import logging

import anthropic
from telegram.ext import Application

from .bot import run_bot
from .brain import Brain
from .brief import BriefService
from .config import load_config
from .db import initialize_database, make_session_factory
from .scheduler import SpotterScheduler

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
    brief_service = BriefService(config, client, session_factory)
    scheduler = SpotterScheduler(config)

    async def _post_init(application: Application) -> None:
        # The bot exists now, so bind the brief job to it and start the scheduler
        # on the running loop (alongside polling, not blocking it).
        async def _brief_job() -> None:
            try:
                brief = await brief_service.deliver(application.bot)
                logger.info("Delivered morning brief for %s", brief.brief_date)
            except Exception:
                logger.exception("Morning-brief job failed")

        scheduler.schedule_daily_brief(_brief_job)
        scheduler.start()
        logger.info(
            "Scheduler started; morning brief at %s %s",
            config.brief_time,
            config.timezone,
        )

    async def _post_shutdown(application: Application) -> None:
        scheduler.shutdown()

    print("Spotter alive")
    run_bot(config, brain, post_init=_post_init, post_shutdown=_post_shutdown)


if __name__ == "__main__":
    main()
