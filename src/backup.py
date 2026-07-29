"""SQLite database backups: weekly scheduled, boot catch-up, and manual runs.

Backups use sqlite3's online backup API (``Connection.backup``), which produces
a consistent snapshot even while the live process holds the database — never a
raw file copy mid-write. They land in ``<db_dir>/backups/`` on the same volume
as timestamped files (``spotter-YYYYMMDD-HHMMSS.db``, UTC), pruned to the
newest ``BACKUP_RETAIN``.

The weekly APScheduler job only fires while the process is up, and Railway
redeploys reset it — so :func:`is_due` gives boot a catch-up check, the same
pattern the morning brief uses.

Manual one-shot::

    python -m src.backup
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_STAMP_FORMAT = "%Y%m%d-%H%M%S"
_PREFIX = "spotter-"
# A backup is due when the newest one is older than this (or none exist).
_MAX_AGE_DAYS = 7


def backup_dir_for(db_path: Path) -> Path:
    return db_path.parent / "backups"


def run_backup(db_path: Path, retain: int, now: datetime | None = None) -> Path:
    """Snapshot ``db_path`` into the backups directory and prune old copies."""
    if not db_path.exists():
        raise FileNotFoundError(f"No database at {db_path}")
    target_dir = backup_dir_for(db_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime(_STAMP_FORMAT)
    target = target_dir / f"{_PREFIX}{stamp}.db"

    source = sqlite3.connect(str(db_path))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    pruned = _prune(target_dir, retain)
    logger.info(
        "Backup written: %s (%d bytes); %d retained, %d pruned",
        target,
        target.stat().st_size,
        len(_backups(target_dir)),
        pruned,
    )
    return target


def is_due(db_path: Path, max_age_days: int = _MAX_AGE_DAYS) -> bool:
    """True when no backup exists or the newest is older than ``max_age_days``.

    Used at startup: the weekly cron only fires while the process is running,
    and frequent redeploys could otherwise starve the schedule forever.
    """
    newest = _newest_stamp(backup_dir_for(db_path))
    if newest is None:
        return True
    return datetime.now(timezone.utc) - newest > timedelta(days=max_age_days)


def _backups(target_dir: Path) -> list[Path]:
    """All backup files, name-sorted (the stamp format sorts chronologically)."""
    return sorted(target_dir.glob(f"{_PREFIX}*.db"))


def _prune(target_dir: Path, retain: int) -> int:
    """Delete all but the newest ``retain`` backups; return how many went."""
    if retain < 1:
        return 0
    excess = _backups(target_dir)[:-retain]
    for path in excess:
        path.unlink()
        logger.info("Pruned old backup: %s", path.name)
    return len(excess)


def _newest_stamp(target_dir: Path) -> datetime | None:
    """UTC timestamp of the newest backup, parsed from its filename."""
    for path in reversed(_backups(target_dir)):
        raw = path.stem.removeprefix(_PREFIX)
        try:
            return datetime.strptime(raw, _STAMP_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError:
            continue  # foreign file in the directory; ignore
    return None


# ---------------------------------------------------------------------------
# Manual one-shot trigger: python -m src.backup
# ---------------------------------------------------------------------------
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from .config import load_config

    config = load_config()
    target = run_backup(config.db_path, config.backup_retain)
    print(f"Backup written: {target}")


if __name__ == "__main__":
    main()
