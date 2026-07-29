"""Spotter entrypoint."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import anthropic
from telegram.ext import Application

from .backup import is_due as backup_is_due
from .backup import run_backup
from .bot import run_bot
from .brain import Brain
from .brief import BriefService
from .config import load_config
from .db import initialize_database, make_session_factory
from .scheduler import SpotterScheduler
from .triggers import TriggerService, ensure_evening_checkin
from .web import Dashboard

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()

    if config.using_dev_bot:
        logger.warning(
            "Using DEV bot (TELEGRAM_DEV_BOT_TOKEN set) — the production bot "
            "is untouched by this process"
        )
    else:
        logger.info("Using PRODUCTION bot")

    engine, project_count = initialize_database(config.db_path, config.seed_context_yaml)
    session_factory = make_session_factory(engine)
    logger.info("Database ready with %d seeded projects", project_count)
    ensure_evening_checkin(config, session_factory)

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

    # The dashboard only exists when DASHBOARD_PASSWORD is set: no password, no
    # web server — the port is never bound, rather than serving unauthenticated.
    dashboard: Dashboard | None = None
    if config.dashboard_password:
        dashboard = Dashboard(config, session_factory, brain)
    else:
        logger.warning("DASHBOARD_PASSWORD unset; web dashboard disabled")

    async def _post_init(application: Application) -> None:
        # The bot exists now, so bind the brief job to it and start the scheduler
        # on the running loop (alongside polling, not blocking it).
        async def _brief_job() -> None:
            try:
                brief = await brief_service.deliver(application.bot)
                logger.info("Delivered morning brief for %s", brief.brief_date)
            except Exception:
                logger.exception("Morning-brief job failed")

        async def _backup_job() -> None:
            try:
                await asyncio.to_thread(
                    run_backup, config.db_path, config.backup_retain
                )
            except Exception:
                logger.exception("Weekly backup failed")

        loop_holder.append(asyncio.get_running_loop())
        scheduler.schedule_daily_brief(_brief_job)
        scheduler.schedule_weekly_backup(_backup_job)
        trigger_service.bind_bot(application.bot)
        registered, caught_up = trigger_service.register_pending()
        scheduler.start()
        logger.info(
            "Scheduled triggers: %d pending registered (%d past-due firing now)",
            registered,
            caught_up,
        )
        # Morning-brief catch-up: if the bot was down at BRIEF_TIME and no
        # brief exists for today (daily_briefs is unique on brief_date, so
        # this can never double-send), deliver it now.
        if brief_service.is_due_catch_up():
            logger.info("Morning brief missed while down; catch-up delivering now")
            asyncio.get_running_loop().create_task(_brief_job())
        # Backup catch-up: the weekly cron only fires while the process is up,
        # and redeploys reset it — so a stale (or absent) newest backup gets
        # one now, in the background.
        if backup_is_due(config.db_path):
            logger.info("Newest DB backup is stale or missing; backing up now")
            asyncio.get_running_loop().create_task(_backup_job())
        logger.info(
            "Scheduler started; morning brief at %s %s",
            config.brief_time,
            config.timezone,
        )
        if dashboard is not None:
            await dashboard.start()

    async def _post_shutdown(application: Application) -> None:
        if dashboard is not None:
            await dashboard.stop()
        scheduler.shutdown()

    print("Spotter alive")
    run_bot(config, brain, post_init=_post_init, post_shutdown=_post_shutdown)


if __name__ == "__main__":
    main()
