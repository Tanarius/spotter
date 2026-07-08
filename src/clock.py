"""Current-time context injected into every model call.

Both the chat brain and the morning brief append this block to their system
prompts so the model always knows the current date, time, timezone, and that
database timestamps are UTC.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


def time_context(timezone: str) -> str:
    """Render the current date/time block for a system prompt.

    Uses the configured IANA timezone; falls back to the host's local timezone
    if the configured name is invalid, so a bad TIMEZONE env var degrades the
    answer instead of killing the turn.
    """
    try:
        now = datetime.now(ZoneInfo(timezone))
        tz_label = timezone
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Invalid TIMEZONE %r; falling back to host local time", timezone)
        now = datetime.now().astimezone()
        tz_label = str(now.tzinfo)
    return (
        "## Current time\n\n"
        f"It is {now.strftime('%A, %Y-%m-%d %H:%M')} ({now.strftime('%I:%M %p').lstrip('0')}) "
        f"in {tz_label} (UTC{now.strftime('%z')}).\n"
        "Timestamps in tool results and the database are stored in UTC — convert "
        "when reasoning about elapsed time or time of day."
    )
