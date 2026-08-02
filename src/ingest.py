"""GitHub webhook ingestion: ground truth about code landing in the event log.

Phase 4A of the memory layer. GitHub POSTs signed webhook payloads to
``/webhooks/github`` (served by the existing dashboard web server); this module
verifies nothing itself — the web layer checks the HMAC — and turns push and
pull_request payloads into ``events`` rows with source ``github``, the time the
thing actually HAPPENED (commit/merge timestamps, not receipt time), and an
``external_id`` (delivery id) so redelivered webhooks can never duplicate.

Repo → project mapping: a project's ``github_repo`` column ("owner/name" or
"name", case-insensitive) wins; otherwise repo short name == project name.
Unmapped repos still get events, with ``subject`` carrying the repo name.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .db.models import Event, Project

logger = logging.getLogger(__name__)

_UTC_FMT = "%Y-%m-%d %H:%M:%S"
_MAX_COMMITS_IN_DETAIL = 10
# Push/PR events from GitHub are ground truth about the code.
_GITHUB_CONFIDENCE = 1.0
# Session notes are agent-written status: high trust, but a notch below commits.
_SESSION_CONFIDENCE = 0.9
_MAX_NOTE_FIELD_CHARS = 1000


def record_github_event(
    session_factory: sessionmaker[Session],
    event_type: str,
    delivery_id: str,
    payload: dict[str, Any],
) -> str:
    """Store one webhook delivery as an event. Returns a short outcome string."""
    if event_type == "ping":
        return "pong"
    if event_type == "push":
        extracted = _extract_push(payload)
    elif event_type == "pull_request":
        extracted = _extract_pull_request(payload)
    else:
        return f"ignored ({event_type})"
    if extracted is None:
        return "ignored (nothing to record)"

    kind, repo_full, repo_name, summary, detail, occurred_at = extracted
    with session_factory() as session, session.begin():
        existing = session.scalar(
            select(Event.id).where(
                Event.source == "github", Event.external_id == delivery_id
            )
        )
        if existing is not None:
            return f"duplicate delivery (event #{existing})"
        project = _resolve_repo_project(session, repo_full, repo_name)
        event = Event(
            source="github",
            kind=kind,
            project_id=project.id if project else None,
            subject=repo_full if project is None else None,
            summary=summary,
            detail=detail,
            confidence=_GITHUB_CONFIDENCE,
            occurred_at=occurred_at,
            external_id=delivery_id,
        )
        session.add(event)
        session.flush()
        target = project.name if project else f"unmapped repo {repo_full}"
        logger.info("GitHub %s event #%d recorded for %s", kind, event.id, target)
        return f"recorded event #{event.id} ({target})"


def record_session_note(
    session_factory: sessionmaker[Session], payload: dict[str, Any]
) -> tuple[bool, str]:
    """Store a Claude Code end-of-session status as an event.

    Expected payload: ``project`` (name or repo), ``worked_on`` (required),
    optional ``shipped``, ``blocked``, ``next``, ``session_id`` (dedupe key),
    ``ended_at`` (ISO; defaults to now). Returns (ok, outcome).
    """
    project_ref = str(payload.get("project") or payload.get("repo") or "").strip()
    fields = {
        key: str(payload.get(key) or "").strip()[:_MAX_NOTE_FIELD_CHARS]
        for key in ("worked_on", "shipped", "blocked", "next")
    }
    if not fields["worked_on"]:
        return False, "worked_on is required"
    session_id = str(payload.get("session_id") or "").strip() or None
    occurred_at = _to_utc_str(payload.get("ended_at"))

    with session_factory() as session, session.begin():
        if session_id:
            existing = session.scalar(
                select(Event.id).where(
                    Event.source == "claude_code", Event.external_id == session_id
                )
            )
            if existing is not None:
                return True, f"duplicate session note (event #{existing})"
        project = _resolve_repo_project(session, project_ref, project_ref)
        target = project.name if project else (project_ref or "unknown project")
        headline = fields["worked_on"].splitlines()[0]
        detail_parts = [f"WORKED ON: {fields['worked_on']}"]
        if fields["shipped"]:
            detail_parts.append(f"SHIPPED: {fields['shipped']}")
        if fields["blocked"]:
            detail_parts.append(f"BLOCKED: {fields['blocked']}")
        if fields["next"]:
            detail_parts.append(f"NEXT: {fields['next']}")
        event = Event(
            source="claude_code",
            kind="session_note",
            project_id=project.id if project else None,
            subject=None if project else (project_ref or None),
            summary=f"Claude Code session on {target}: {headline}",
            detail="\n".join(detail_parts),
            confidence=_SESSION_CONFIDENCE,
            occurred_at=occurred_at,
            external_id=session_id,
        )
        session.add(event)
        session.flush()
        if project is not None:
            supersede_previous_session_notes(session, project.id, event.id)
        logger.info("Session note event #%d recorded for %s", event.id, target)
        return True, f"recorded session note #{event.id} ({target})"


def supersede_previous_session_notes(
    session: Session, project_id: int, new_event_id: int
) -> int:
    """The newest session note for a project retires all prior live ones.

    'Where things stand' has exactly one current answer per project; older
    notes stay in the log (history) but stop surfacing in retrieval.
    """
    superseded = 0
    for old in session.scalars(
        select(Event).where(
            Event.project_id == project_id,
            Event.kind == "session_note",
            Event.superseded_by.is_(None),
            Event.id != new_event_id,
        )
    ):
        old.superseded_by = new_event_id
        superseded += 1
    return superseded


# -- payload extraction --------------------------------------------------------

def _extract_push(
    payload: dict[str, Any],
) -> tuple[str, str, str, str, str | None, str] | None:
    """(kind, repo_full, repo_name, summary, detail, occurred_at) for a push."""
    if payload.get("deleted"):
        return None  # branch deletion, not work
    repo = payload.get("repository") or {}
    repo_full = str(repo.get("full_name") or "")
    repo_name = str(repo.get("name") or repo_full)
    if not repo_full and not repo_name:
        return None
    branch = str(payload.get("ref") or "").removeprefix("refs/heads/")
    commits = payload.get("commits") or []
    head = payload.get("head_commit") or (commits[-1] if commits else None)
    if head is None:
        return None  # e.g. tag push with no commits

    head_line = str(head.get("message") or "").splitlines()[0]
    count = len(commits) or 1
    plural = "s" if count != 1 else ""
    summary = f"{count} commit{plural} pushed to {repo_name}@{branch}: {head_line}"
    detail_lines = [
        f"- {str(c.get('id') or '')[:7]} {str(c.get('message') or '').splitlines()[0]}"
        for c in commits[-_MAX_COMMITS_IN_DETAIL:]
    ]
    if len(commits) > _MAX_COMMITS_IN_DETAIL:
        detail_lines.insert(0, f"(showing last {_MAX_COMMITS_IN_DETAIL} of {len(commits)})")
    detail = "\n".join(detail_lines) or None
    occurred_at = _to_utc_str(head.get("timestamp"))
    return "push", repo_full, repo_name, summary, detail, occurred_at


def _extract_pull_request(
    payload: dict[str, Any],
) -> tuple[str, str, str, str, str | None, str] | None:
    """Record PR opened and PR merged; ignore everything else."""
    action = payload.get("action")
    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    repo_full = str(repo.get("full_name") or "")
    repo_name = str(repo.get("name") or repo_full)
    number = pr.get("number")
    title = str(pr.get("title") or "").strip()

    if action == "opened":
        verb, stamp = "opened", pr.get("created_at")
    elif action == "closed" and pr.get("merged"):
        verb, stamp = "merged", pr.get("merged_at")
    else:
        return None
    summary = f"PR #{number} {verb} in {repo_name}: {title}"
    detail = str(pr.get("body") or "").strip()[:500] or None
    return (
        "pull_request",
        repo_full,
        repo_name,
        summary,
        detail,
        _to_utc_str(stamp),
    )


# -- helpers -------------------------------------------------------------------

def _resolve_repo_project(
    session: Session, repo_full: str, repo_name: str
) -> Project | None:
    """github_repo match (full, bare, or name-tail) else repo name == project name."""
    for candidate in (repo_full, repo_name):
        if not candidate:
            continue
        project = session.scalar(
            select(Project).where(
                func.lower(Project.github_repo) == candidate.lower()
            )
        )
        if project is not None:
            return project
    if repo_name:
        # A bare repo name should also match a github_repo stored as
        # 'owner/name' — compare against the segment after the slash.
        lowered = repo_name.lower()
        for project in session.scalars(
            select(Project).where(Project.github_repo.is_not(None))
        ):
            if (project.github_repo or "").lower().split("/")[-1] == lowered:
                return project
        return session.scalar(
            select(Project).where(func.lower(Project.name) == lowered)
        )
    return None


def _to_utc_str(stamp: Any) -> str:
    """ISO timestamp (with offset or Z) -> DB UTC string; now() when absent."""
    if stamp:
        try:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).strftime(_UTC_FMT)
        except ValueError:
            logger.warning("Unparsable event timestamp %r; using now()", stamp)
    return datetime.now(timezone.utc).strftime(_UTC_FMT)
