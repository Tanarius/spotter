"""APScheduler wiring for Spotter's scheduled work: the morning brief cron
job and one-shot firings of DB-backed scheduled triggers.

Runs an ``AsyncIOScheduler`` on the same event loop as python-telegram-bot's
long-polling, so the daily brief fires without blocking message handling. The
scheduler is created up front; the job (a coroutine that needs the running bot)
is attached and started from the application's ``post_init`` hook, once the loop
and bot exist.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .config import Config

logger = logging.getLogger(__name__)

_BRIEF_JOB_ID = "morning_brief"
_BACKUP_JOB_ID = "weekly_backup"
_NUDGE_JOB_ID = "daily_nudge"
# If Spotter is down at BRIEF_TIME and comes back within this window, still fire.
_MISFIRE_GRACE_SECONDS = 3600
# Weekly backup slot: quiet hours, local time. Missed runs are handled by the
# boot-time catch-up in src.backup.is_due, not by misfire grace.
_BACKUP_DAY_OF_WEEK = "sun"
_BACKUP_HOUR = 3


class SpotterScheduler:
    """Thin wrapper around AsyncIOScheduler for Spotter's scheduled jobs."""

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

    def schedule_daily_nudge(self, callback: Callable[[], Awaitable[None]]) -> Job:
        """Register the conditions-engine check daily at NUDGE_TIME (config tz).

        No misfire catch-up beyond the shared grace: a nudge that missed its
        slot shouldn't fire at some odd hour — tomorrow's check covers it.
        """
        hour, minute = _parse_hhmm(self._config.nudge_time)
        return self._scheduler.add_job(
            callback,
            CronTrigger(hour=hour, minute=minute, timezone=self._tz),
            id=_NUDGE_JOB_ID,
            replace_existing=True,
            misfire_grace_time=_MISFIRE_GRACE_SECONDS,
        )

    def schedule_weekly_backup(self, callback: Callable[[], Awaitable[None]]) -> Job:
        """Register the DB backup coroutine weekly (Sunday 03:00, config tz)."""
        return self._scheduler.add_job(
            callback,
            CronTrigger(
                day_of_week=_BACKUP_DAY_OF_WEEK,
                hour=_BACKUP_HOUR,
                minute=0,
                timezone=self._tz,
            ),
            id=_BACKUP_JOB_ID,
            replace_existing=True,
            misfire_grace_time=_MISFIRE_GRACE_SECONDS,
        )

    def schedule_trigger(
        self,
        trigger_id: int,
        run_at: datetime,
        callback: Callable[[int], Awaitable[None]],
    ) -> Job:
        """Register a one-shot firing of DB trigger ``trigger_id`` at ``run_at``.

        ``run_at`` must be timezone-aware (UTC from the DB). Re-registering the
        same trigger id replaces the existing job, which is what rescheduling a
        recurring trigger wants.
        """
        return self._scheduler.add_job(
            callback,
            DateTrigger(run_date=run_at),
            args=(trigger_id,),
            id=f"trigger_{trigger_id}",
            replace_existing=True,
            misfire_grace_time=_MISFIRE_GRACE_SECONDS,
        )

    def cancel_trigger(self, trigger_id: int) -> None:
        """Drop the scheduled job for a trigger, if one is registered."""
        job = self._scheduler.get_job(f"trigger_{trigger_id}")
        if job is not None:
            job.remove()

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
