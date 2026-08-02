"""Weekly digest: the week's events condensed into one durable event.

Long-horizon questions ("what did July look like?") shouldn't have to
retrieve sixty raw events — each Sunday the week gets summarized into a
single ``digest`` event (source ``inferred``, deterministic template, no
model call) that ages gracefully in retrieval. ``external_id`` is keyed on
the ISO week, so the scheduled run and the boot catch-up can never
double-write, and redeploy-heavy weeks still get their digest.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db.models import Event, Project

logger = logging.getLogger(__name__)

_UTC_FMT = "%Y-%m-%d %H:%M:%S"
_WINDOW_DAYS = 7
_HIGHLIGHTS_PER_PROJECT = 2
# Kinds that are bookkeeping, not work — excluded from digests.
_EXCLUDED_KINDS = ("digest", "nudge", "correction")


def write_weekly_digest(session_factory: sessionmaker[Session]) -> int | None:
    """Summarize the last 7 days into a digest event. Returns its id, or None.

    None when the week had no events or this ISO week's digest already exists.
    """
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=_WINDOW_DAYS)).strftime(_UTC_FMT)
    iso_year, iso_week, _ = now.isocalendar()
    external_id = f"digest-{iso_year}-W{iso_week:02d}"

    with session_factory() as session, session.begin():
        existing = session.scalar(
            select(Event.id).where(
                Event.source == "inferred", Event.external_id == external_id
            )
        )
        if existing is not None:
            return None
        events = session.scalars(
            select(Event)
            .where(
                Event.occurred_at >= since,
                Event.kind.not_in(_EXCLUDED_KINDS),
                Event.superseded_by.is_(None),
            )
            .order_by(Event.occurred_at.desc())
        ).all()
        if not events:
            return None

        project_names = {p.id: p.name for p in session.scalars(select(Project))}
        grouped: dict[str, list[Event]] = {}
        for event in events:
            name = project_names.get(event.project_id) or event.subject or "general"
            grouped.setdefault(name, []).append(event)

        parts = []
        detail_lines = []
        for name, rows in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
            kinds: dict[str, int] = {}
            for row in rows:
                kinds[row.kind] = kinds.get(row.kind, 0) + 1
            kind_text = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
            parts.append(f"{name} {len(rows)} ({kind_text})")
            detail_lines.append(f"{name}: {len(rows)} events — {kind_text}")
            detail_lines += [
                f"  - {row.summary}" for row in rows[:_HIGHLIGHTS_PER_PROJECT]
            ]

        digest = Event(
            source="inferred",
            kind="digest",
            summary=f"Weekly digest {external_id.removeprefix('digest-')}: "
            + " · ".join(parts),
            detail="\n".join(detail_lines),
            confidence=1.0,  # derived arithmetically from ground truth
            occurred_at=now.strftime(_UTC_FMT),
            external_id=external_id,
        )
        session.add(digest)
        session.flush()
        logger.info("Weekly digest written: event #%d (%s)", digest.id, external_id)
        return digest.id


# Catch-up threshold: a healthy weekly cadence never exceeds 7 days between
# digests; past this, the cron slot was missed (redeploy reset — the backup
# lesson) and boot should write one.
_STALE_AFTER_DAYS = 8


def digest_is_due(session_factory: sessionmaker[Session]) -> bool:
    """True when the newest digest is missing/stale AND the week had events.

    Deliberately NOT keyed on "this ISO week has no digest yet" — that would
    make mid-week boots pre-write Sunday's digest. The per-week external_id
    in :func:`write_weekly_digest` still dedupes overlapping runs.
    """
    now = datetime.now(timezone.utc)
    stale_cutoff = (now - timedelta(days=_STALE_AFTER_DAYS)).strftime(_UTC_FMT)
    since = (now - timedelta(days=_WINDOW_DAYS)).strftime(_UTC_FMT)
    with session_factory() as session:
        newest = session.scalar(
            select(Event.occurred_at)
            .where(Event.kind == "digest")
            .order_by(Event.occurred_at.desc())
        )
        if newest is not None and newest >= stale_cutoff:
            return False
        any_events = session.scalar(
            select(Event.id)
            .where(
                Event.occurred_at >= since,
                Event.kind.not_in(_EXCLUDED_KINDS),
            )
            .limit(1)
        )
        return any_events is not None
