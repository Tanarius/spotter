"""APScheduler wiring for Spotter's one scheduled job: the morning brief.

Runs an ``AsyncIOScheduler`` on the same event loop as python-telegram-bot's
long-polling, so the daily brief fires without blocking message handling. The
scheduler is created up front; the job (a coroutine that needs the running bot)
is attached and started from the application's ``post_init`` hook, once the loop
and bot exist.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Config

logger = logging.getLogger(__name__)

_BRIEF_JOB_ID = "morning_brief"
# If Spotter is down at BRIEF_TIME and comes back within this window, still fire.
_MISFIRE_GRACE_SECONDS = 3600


class SpotterScheduler:
    """Thin wrapper around AsyncIOScheduler for the daily-brief job."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._tz = ZoneInfo(config.timezone)
        self._scheduler = AsyncIOScheduler(timezone=self._tz)

    def schedule_daily_brief(self, callback: Callable[[], Awaitable[None]]) -> Job:
        """Register the brief coroutine to run daily at BRIEF_TIME (config tz)."""
        hour, minute = _parse_hhmm(self._config.brief_time)
        return self._scheduler.add_job(
            callback,
            CronTrigger(hour=hour, minute=minute, timezone=self._tz),
            id=_BRIEF_JOB_ID,
            replace_existing=True,
            misfire_grace_time=_MISFIRE_GRACE_SECONDS,
        )

    def start(self) -> None:
        """Start the scheduler on the currently running asyncio loop."""
        if not self._scheduler.running:
            self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    @property
    def jobs(self) -> list[Job]:
        return self._scheduler.get_jobs()


def _parse_hhmm(value: str) -> tuple[int, int]:
    """Parse a ``HH:MM`` string into (hour, minute); default 07:00 if malformed."""
    try:
        hour_str, minute_str = value.strip().split(":", 1)
        return int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        logger.warning("Invalid BRIEF_TIME %r; defaulting to 07:00", value)
        return 7, 0
