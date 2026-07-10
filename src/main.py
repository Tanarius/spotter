"""Spotter entrypoint."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import anthropic
from telegram.ext import Application

from .bot import run_bot
from .brain import Brain
from .brief import BriefService
from .config import load_config
from .db import initialize_database, make_session_factory
from .scheduler import SpotterScheduler
from .triggers import TriggerService

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()

    engine, project_count = initialize_database(config.db_path, config.seed_context_yaml)
    session_factory = make_session_factory(engine)
    logger.info("Database ready with %d seeded projects", project_count)

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    brief_service = BriefService(config, client, session_factory)
    scheduler = SpotterScheduler(config)
    trigger_service = TriggerService(config, client, session_factory, scheduler)

    # Tool handlers run in a worker thread (asyncio.to_thread); hop back onto
    # the event loop before touching the loop-bound scheduler. The loop is
    # captured in post_init, before any message can invoke a tool.
    loop_holder: list[asyncio.AbstractEventLoop] = []

    def _register_trigger_threadsafe(trigger_id: int, due_utc: datetime) -> None:
        if not loop_holder:
            logger.warning("Trigger #%d created pre-loop; arms on restart", trigger_id)
            return
        loop_holder[0].call_soon_threadsafe(
            trigger_service.register, trigger_id, due_utc
        )

    brain = Brain(config, client, session_factory, _register_trigger_threadsafe)

    async def _post_init(application: Application) -> None:
        # The bot exists now, so bind the brief job to it and start the scheduler
        # on the running loop (alongside polling, not blocking it).
        async def _brief_job() -> None:
            try:
                brief = await brief_service.deliver(application.bot)
                logger.info("Delivered morning brief for %s", brief.brief_date)
            except Exception:
                logger.exception("Morning-brief job failed")

        loop_holder.append(asyncio.get_running_loop())
        scheduler.schedule_daily_brief(_brief_job)
        trigger_service.bind_bot(application.bot)
        registered, caught_up = trigger_service.register_pending()
        scheduler.start()
        logger.info(
            "Scheduled triggers: %d pending registered (%d past-due firing now)",
            registered,
            caught_up,
        )
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
