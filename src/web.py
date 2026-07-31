"""Password-gated web dashboard served from the Spotter process.

Web Steps 1-3: read-only state view, button actions (complete a task, change a
task's status, resolve a stall), quick-add forms (task, captured item), and the
job-applications tracker. An aiohttp application runs on the
SAME asyncio event loop as python-telegram-bot and APScheduler —
``Dashboard.start`` is awaited from the bot's ``post_init`` hook, so no second
process, thread, or event loop exists. All database access goes through
``asyncio.to_thread`` with a short-lived session from the shared
``session_factory``, the exact pattern the Telegram handler already uses for
``Brain.respond``, so DB access stays thread-safe by construction.

Task writes are NOT reimplemented here: they call the same
``update_task_status`` tool handler the brain dispatches, inside the same
transaction shape (``session.begin()`` + ``ToolContext``), so the web and chat
can never disagree about what a status change means.

Access control: a single shared secret from DASHBOARD_PASSWORD. ``main`` never
constructs a Dashboard when the password is unset, so there is no open-server
mode. Logging in sets an HttpOnly cookie holding an HMAC derived from the
password; changing the password invalidates every session.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from aiohttp import web
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .brain import _FALLBACK_REPLY as _BRAIN_FALLBACK
from .brain import Brain
from .config import Config
from .ingest import record_github_event, record_session_note
from .db.models import (
    CapturedItem,
    ConversationLogEntry,
    Event,
    JobApplication,
    Milestone,
    Project,
    ScheduledTrigger,
    StallEvent,
    Task,
)
from .tools.base import ToolContext
from .tools.capture import capture_item
# _next_task is the tool's own candidate pick (is_next else oldest open); the
# hero uses it directly so the page and the agent can never pick differently.
from .tools.next_action import _next_task
from .tools.status import update_task_status
from .triggers import parse_db_utc

logger = logging.getLogger(__name__)

_COOKIE_NAME = "spotter_session"
_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 days
# Fixed HMAC message: the session token is HMAC(password, this). Bumping the
# version string logs every session out.
_TOKEN_CONTEXT = b"spotter-dashboard-session-v1"
# Small fixed delay on a wrong password, so online brute-forcing is at least slow.
_FAILED_LOGIN_DELAY_SECONDS = 1.0

# Task statuses that still represent live work (mirrors tools/status.py).
_LIVE_TASK_STATUSES = ("open", "in_progress", "paused", "waiting")
# Statuses offered in the per-task dropdown. Validation happens inside the
# update_task_status tool, not here — this only shapes the UI.
_STATUS_CHOICES = ("open", "waiting", "paused", "done")
# Job-application pipeline statuses, in rough funnel order.
_APP_STATUSES = (
    "applied",
    "responded",
    "screen",
    "interview",
    "offer",
    "rejected",
    "ghosted",
)
# How many recent captured items the dashboard lists.
_RECENT_CAPTURES = 8
# Generated next actions are cached per project this long (and invalidated
# whenever the underlying task row changes), so page loads stay free.
_NEXT_ACTION_TTL_SECONDS = 600
# A task untouched this long gets a staleness marker in the work list.
_STALE_AFTER_DAYS = 3
# With this many items or fewer in the right column (applications + triggers),
# the page renders single-column and the work list takes the full width.
_SPARSE_RIGHT_ITEMS = 3

_NEXT_ACTION_PROMPT = (
    "Surface the next concrete action on {project} (use the surface_next_action "
    "tool). Reply with only the next action itself — one or two short sentences, "
    "no preamble, no commentary."
)
_SHRINK_PROMPT = (
    "The current next step on {project} is still too big for me to start: "
    '"{current}". Use surface_next_action with smaller_than set to that text and '
    "give me the first physical move — which file to open, the first command to "
    "run, the first sentence to write. Reply with only that smaller step."
)
_STALL_CHECK_PROMPT = (
    "Am I stalling on {project}? Check the current state — if I am, name the "
    "stall bluntly (log it with name_the_stall if it's new); if not, say so in "
    "one or two sentences."
)
_HANDOFF_PROMPT = (
    "Prepare a Claude Code handoff for task #{task_id} ('{title}') on {project}. "
    "Use the prepare_handoff tool, then reply with ONLY the final ready-to-paste "
    "prompt text — no code fences, no commentary around it."
)
_COMPLETION_EVAL_PROMPT = (
    "Task #{task_id} ('{title}') on {project} was just marked done from the "
    "dashboard. Evaluate whether the active milestone's work is now complete — "
    "if it is, mark the milestone done with update_milestone (the next pending "
    "activates automatically); if not, say in one sentence what still remains "
    "toward it."
)


class Dashboard:
    """The web dashboard: owns the aiohttp app and its lifecycle."""

    def __init__(
        self,
        config: Config,
        session_factory: sessionmaker[Session],
        brain: Brain,
    ) -> None:
        if not config.dashboard_password:
            raise ValueError("Dashboard requires DASHBOARD_PASSWORD to be set")
        self._config = config
        self._session_factory = session_factory
        self._brain = brain
        self._tz = ZoneInfo(config.timezone)
        self._runner: web.AppRunner | None = None
        # project_id -> generated next-action step, keyed to the exact task row
        # (id + updated_at) it was generated from. Mutated only from to_thread
        # workers; a lost race just costs one duplicate generation.
        self._next_cache: dict[int, dict[str, Any]] = {}

    # -- lifecycle (called from post_init / post_shutdown, on the running loop) --

    async def start(self) -> None:
        """Bind and serve on 0.0.0.0:PORT alongside long polling."""
        app = web.Application(middlewares=[self._auth_middleware])
        app.router.add_get("/", self._index)
        app.router.add_get("/login", self._login_page)
        app.router.add_post("/login", self._login_submit)
        app.router.add_post("/logout", self._logout)
        # Write path (Step 2). Same auth middleware guards these: an
        # unauthenticated POST is redirected to /login before any handler runs.
        app.router.add_post("/tasks/status", self._task_status_action)
        app.router.add_post("/stalls/resolve", self._stall_resolve_action)
        # Quick-add + job applications (Step 3), same auth middleware.
        app.router.add_post("/tasks/add", self._task_add_action)
        app.router.add_post("/captures/add", self._capture_add_action)
        app.router.add_post("/apps/add", self._app_add_action)
        app.router.add_post("/apps/status", self._app_status_action)
        # Agent-backed endpoints: each runs a Brain tool-use turn off-loop and
        # returns JSON for the page's fetch calls.
        app.router.add_post("/api/next-action", self._api_next_action)
        app.router.add_post("/api/shrink", self._api_shrink)
        app.router.add_post("/api/stall-check", self._api_stall_check)
        app.router.add_post("/api/handoff", self._api_handoff)
        # GitHub webhook ingestion (memory phase 4A). Only registered when a
        # secret exists — no secret, no endpoint. Auth is GitHub's HMAC
        # signature, not the session cookie (see _auth_middleware exemption).
        if self._config.github_webhook_secret:
            app.router.add_post("/webhooks/github", self._github_webhook)
        # Claude Code session notes (memory phase 4B): same refusal pattern,
        # authenticated by the X-Spotter-Secret header.
        if self._config.session_note_secret:
            app.router.add_post("/webhooks/session", self._session_webhook)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._config.web_port)
        await site.start()
        logger.info("Dashboard serving on port %d", self._config.web_port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- auth --------------------------------------------------------------------

    def _session_token(self) -> str:
        return hmac.new(
            self._config.dashboard_password.encode(), _TOKEN_CONTEXT, hashlib.sha256
        ).hexdigest()

    def _is_authenticated(self, request: web.Request) -> bool:
        cookie = request.cookies.get(_COOKIE_NAME, "")
        return bool(cookie) and hmac.compare_digest(cookie, self._session_token())

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        # /webhooks/github authenticates via GitHub's HMAC signature inside
        # its handler; GitHub cannot hold a session cookie.
        if (
            request.path == "/login"
            or request.path.startswith("/webhooks/")
            or self._is_authenticated(request)
        ):
            return await handler(request)
        raise web.HTTPFound("/login")

    async def _login_page(self, request: web.Request) -> web.Response:
        if self._is_authenticated(request):
            raise web.HTTPFound("/")
        return _html_response(_render_login(error=False))

    async def _login_submit(self, request: web.Request) -> web.Response:
        form = await request.post()
        password = str(form.get("password", ""))
        if not hmac.compare_digest(password, self._config.dashboard_password):
            await asyncio.sleep(_FAILED_LOGIN_DELAY_SECONDS)
            return _html_response(_render_login(error=True), status=401)
        response = web.HTTPFound("/")
        response.set_cookie(
            _COOKIE_NAME,
            self._session_token(),
            max_age=_COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
            secure=_is_https(request),
            path="/",
        )
        raise response

    async def _logout(self, request: web.Request) -> web.Response:
        response = web.HTTPFound("/login")
        response.del_cookie(_COOKIE_NAME, path="/")
        raise response

    # -- pages -------------------------------------------------------------------

    async def _index(self, request: web.Request) -> web.Response:
        # DB reads run off-loop, same as every other DB touch in the process.
        state = await asyncio.to_thread(self._load_state)
        message = request.query.get("msg", "")
        return _html_response(_render_index(state, self._config.timezone, message))

    # -- actions (write path) ----------------------------------------------------

    async def _task_status_action(self, request: web.Request) -> web.Response:
        """Set a task's status via the update_task_status tool handler."""
        form = await request.post()
        task_id = _parse_id(form.get("id"))
        status = str(form.get("status", "")).strip()
        if task_id is None:
            raise web.HTTPBadRequest(text="missing or non-numeric task id")
        result = await asyncio.to_thread(self._write_task_status, task_id, status)
        # Line 1 is the human confirmation; later lines (the milestone-eval
        # instruction added by the tool) are for the model, not the toast.
        toast = result.splitlines()[0] if result else result
        if status == "done" and "Active milestone" in result:
            # Progress awareness: evaluate milestone impact in the background so
            # the redirect stays instant. The turn is logged — it can write
            # milestone state, and chat should know about it.
            asyncio.get_running_loop().create_task(
                self._evaluate_completion(task_id)
            )
            toast += " Evaluating milestone impact…"
        raise _redirect_with_message(toast)

    async def _evaluate_completion(self, task_id: int) -> None:
        try:
            await asyncio.to_thread(self._run_completion_eval, task_id)
        except Exception:
            logger.exception("Milestone evaluation for task #%d failed", task_id)

    # -- Claude Code session notes (memory phase 4B) ----------------------------

    async def _session_webhook(self, request: web.Request) -> web.Response:
        """Record an end-of-session status; auth via X-Spotter-Secret header."""
        provided = request.headers.get("X-Spotter-Secret", "")
        if not hmac.compare_digest(provided, self._config.session_note_secret):
            logger.warning("Session note rejected: bad or missing secret")
            raise web.HTTPUnauthorized(text="bad secret")
        payload = await _json_body(request)
        if not payload:
            raise web.HTTPBadRequest(text="invalid or empty JSON body")
        ok, outcome = await asyncio.to_thread(
            record_session_note, self._session_factory, payload
        )
        if not ok:
            raise web.HTTPBadRequest(text=outcome)
        return web.Response(text=outcome)

    # -- GitHub webhook (memory phase 4A) ---------------------------------------

    async def _github_webhook(self, request: web.Request) -> web.Response:
        """Verify GitHub's HMAC signature, then record the delivery as an event."""
        body = await request.read()
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            self._config.github_webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            logger.warning("GitHub webhook rejected: bad or missing signature")
            raise web.HTTPForbidden(text="bad signature")

        event_type = request.headers.get("X-GitHub-Event", "")
        delivery_id = request.headers.get("X-GitHub-Delivery", "")
        if not delivery_id:
            raise web.HTTPBadRequest(text="missing delivery id")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise web.HTTPBadRequest(text="invalid JSON")

        outcome = await asyncio.to_thread(
            record_github_event,
            self._session_factory,
            event_type,
            delivery_id,
            payload,
        )
        return web.Response(text=outcome)

    def _run_completion_eval(self, task_id: int) -> None:
        with self._session_factory() as session:
            task = session.get(Task, task_id)
            project = (
                session.get(Project, task.project_id)
                if task is not None and task.project_id
                else None
            )
        if task is None or project is None:
            return
        self._brain.respond(
            _COMPLETION_EVAL_PROMPT.format(
                task_id=task.id, title=task.title, project=project.name
            )
        )

    def _write_task_status(self, task_id: int, status: str) -> str:
        """Run the update_task_status tool exactly as the brain dispatches it."""
        with self._session_factory() as session, session.begin():
            context = ToolContext(session=session, config=self._config)
            return update_task_status(
                context, {"target_type": "task", "id": task_id, "status": status}
            )

    async def _stall_resolve_action(self, request: web.Request) -> web.Response:
        form = await request.post()
        stall_id = _parse_id(form.get("id"))
        if stall_id is None:
            raise web.HTTPBadRequest(text="missing or non-numeric stall id")
        result = await asyncio.to_thread(self._write_stall_resolved, stall_id)
        raise _redirect_with_message(result)

    def _write_stall_resolved(self, stall_id: int) -> str:
        """Mark a stall resolved. No tool exists for this; the write is one flag."""
        with self._session_factory() as session, session.begin():
            stall = session.get(StallEvent, stall_id)
            if stall is None:
                return f"No stall #{stall_id} found."
            if stall.resolved:
                return f"Stall #{stall_id} was already resolved."
            stall.resolved = 1
            return f"Stall #{stall_id} ({stall.description}) marked resolved."

    async def _task_add_action(self, request: web.Request) -> web.Response:
        form = await request.post()
        title = str(form.get("title", "")).strip()
        project_id = _parse_id(form.get("project_id"))  # None = unlinked
        result = await asyncio.to_thread(self._write_task_add, title, project_id)
        raise _redirect_with_message(result)

    def _write_task_add(self, title: str, project_id: int | None) -> str:
        """Create a task. No create-task tool exists; this is the one write path."""
        if not title:
            return "Task needs a title."
        with self._session_factory() as session, session.begin():
            project = session.get(Project, project_id) if project_id else None
            if project_id is not None and project is None:
                return f"No project #{project_id} found."
            task = Task(title=title, project_id=project.id if project else None)
            session.add(task)
            session.flush()
            where = f" under {project.name}" if project else ""
            return f"Task #{task.id} added{where}: {title}"

    async def _capture_add_action(self, request: web.Request) -> web.Response:
        form = await request.post()
        content = str(form.get("content", "")).strip()
        result = await asyncio.to_thread(self._write_capture, content)
        raise _redirect_with_message(result)

    def _write_capture(self, content: str) -> str:
        """Run the capture_item tool exactly as the brain dispatches it."""
        with self._session_factory() as session, session.begin():
            context = ToolContext(session=session, config=self._config)
            return capture_item(context, {"content": content, "source": "dashboard"})

    async def _app_add_action(self, request: web.Request) -> web.Response:
        form = await request.post()
        fields = {
            key: str(form.get(key, "")).strip()
            for key in ("company", "role", "source", "date_applied", "notes")
        }
        result = await asyncio.to_thread(self._write_app_add, fields)
        raise _redirect_with_message(result)

    def _write_app_add(self, fields: dict[str, str]) -> str:
        if not fields["company"] or not fields["role"]:
            return "An application needs at least a company and a role."
        date_applied = fields["date_applied"] or datetime.now(self._tz).strftime("%Y-%m-%d")
        with self._session_factory() as session, session.begin():
            app_row = JobApplication(
                company=fields["company"],
                role=fields["role"],
                source=fields["source"] or None,
                date_applied=date_applied,
                notes=fields["notes"] or None,
            )
            session.add(app_row)
            session.flush()
            return (
                f"Application #{app_row.id} added: {fields['role']} at "
                f"{fields['company']} ({date_applied})."
            )

    async def _app_status_action(self, request: web.Request) -> web.Response:
        form = await request.post()
        app_id = _parse_id(form.get("id"))
        status = str(form.get("status", "")).strip().lower()
        if app_id is None:
            raise web.HTTPBadRequest(text="missing or non-numeric application id")
        result = await asyncio.to_thread(self._write_app_status, app_id, status)
        raise _redirect_with_message(result)

    def _write_app_status(self, app_id: int, status: str) -> str:
        if status not in _APP_STATUSES:
            return f"Invalid application status '{status}'. Valid: {', '.join(_APP_STATUSES)}."
        with self._session_factory() as session, session.begin():
            app_row = session.get(JobApplication, app_id)
            if app_row is None:
                return f"No application #{app_id} found."
            old = app_row.status
            app_row.status = status
            app_row.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            return f"{app_row.company} — {app_row.role}: {old} -> {status}."

    # -- agent-backed endpoints --------------------------------------------------

    async def _api_next_action(self, request: web.Request) -> web.Response:
        body = await _json_body(request)
        force = bool(body.get("force"))
        result = await asyncio.to_thread(self._generate_next_action, force)
        return web.json_response(result)

    async def _api_shrink(self, request: web.Request) -> web.Response:
        body = await _json_body(request)
        current = str(body.get("current", "")).strip()
        if not current:
            return web.json_response({"ok": False, "text": "Nothing to shrink."})
        result = await asyncio.to_thread(self._generate_shrink, current)
        return web.json_response(result)

    async def _api_stall_check(self, request: web.Request) -> web.Response:
        result = await asyncio.to_thread(self._run_stall_check)
        return web.json_response(result)

    async def _api_handoff(self, request: web.Request) -> web.Response:
        result = await asyncio.to_thread(self._generate_handoff)
        return web.json_response(result)

    def _generate_handoff(self) -> dict[str, Any]:
        """A Claude Code handoff prompt for the hero task (unlogged brain turn)."""
        hero = self._hero_snapshot()
        if hero is None:
            return {"ok": False, "text": "No active project with a live task."}
        prompt = _HANDOFF_PROMPT.format(
            task_id=hero["task_id"], title=hero["title"], project=hero["project"]
        )
        try:
            text = self._brain.respond(prompt, log=False)
        except Exception:
            logger.exception("Handoff generation from dashboard failed")
            return {"ok": False, "text": ""}
        if not text or text == _BRAIN_FALLBACK:
            return {"ok": False, "text": text}
        return {"ok": True, "text": _strip_code_fences(text)}

    def _hero_snapshot(self) -> dict[str, Any] | None:
        """The hero target: the agent's own pick for the top active project."""
        with self._session_factory() as session:
            return _pick_hero(session)

    def _generate_next_action(self, force: bool) -> dict[str, Any]:
        """Cached-or-generated concrete next step for the hero task."""
        hero = self._hero_snapshot()
        if hero is None:
            return {"ok": False, "text": "No active project with a live task."}
        cached = self._cached_step(hero)
        if cached is not None and not force:
            return {"ok": True, "text": cached, "cached": True}
        prompt = _NEXT_ACTION_PROMPT.format(project=hero["project"])
        return self._brain_step(prompt, hero)

    def _generate_shrink(self, current: str) -> dict[str, Any]:
        hero = self._hero_snapshot()
        if hero is None:
            return {"ok": False, "text": "No active project with a live task."}
        prompt = _SHRINK_PROMPT.format(project=hero["project"], current=current)
        return self._brain_step(prompt, hero)

    def _brain_step(self, prompt: str, hero: dict[str, Any]) -> dict[str, Any]:
        """One unlogged Brain turn; cache and return the step it produces."""
        try:
            text = self._brain.respond(prompt, log=False)
        except Exception:
            logger.exception("Brain call from dashboard failed")
            return {"ok": False, "text": ""}
        if not text or text == _BRAIN_FALLBACK:
            return {"ok": False, "text": text}
        self._next_cache[hero["project_id"]] = {
            "step": text,
            "task_id": hero["task_id"],
            "task_updated_at": hero["updated_at"],
            "expires_at": time.monotonic() + _NEXT_ACTION_TTL_SECONDS,
        }
        return {"ok": True, "text": text, "cached": False}

    def _run_stall_check(self) -> dict[str, Any]:
        """A logged Brain turn: real check-in that may write a stall_events row."""
        hero = self._hero_snapshot()
        project = hero["project"] if hero else "my top project"
        try:
            text = self._brain.respond(_STALL_CHECK_PROMPT.format(project=project))
        except Exception:
            logger.exception("Stall check from dashboard failed")
            return {"ok": False, "text": "Stall check failed — try again."}
        return {"ok": bool(text) and text != _BRAIN_FALLBACK, "text": text}

    def _cached_step(self, hero: dict[str, Any]) -> str | None:
        """The cached generated step, if it matches this exact task row and is fresh."""
        entry = self._next_cache.get(hero["project_id"])
        if (
            entry is not None
            and entry["task_id"] == hero["task_id"]
            and entry["task_updated_at"] == hero["updated_at"]
            and entry["expires_at"] > time.monotonic()
        ):
            return entry["step"]
        return None

    def _identity(self) -> dict[str, Any]:
        """Which world this page is: environment, bot, database. Anti-confusion."""
        return {
            "environment": self._config.environment_label,
            "bot_kind": "DEV bot" if self._config.using_dev_bot else "PROD bot",
            "bot_id": self._config.bot_id,
            "db_path": str(self._config.db_path),
        }

    def _load_state(self) -> dict[str, Any]:
        """Snapshot everything the page shows into plain dicts (no live ORM rows)."""
        with self._session_factory() as session:
            projects = session.scalars(
                select(Project).order_by(Project.priority.desc(), Project.id)
            ).all()
            active_milestones = {
                m.project_id: m.title
                for m in session.scalars(
                    select(Milestone)
                    .where(Milestone.status == "active")
                    .order_by(Milestone.order_index, Milestone.id)
                )
            }
            # Recent ingested activity: latest event + a 7-day count per project.
            activity_since = (
                datetime.now(timezone.utc) - timedelta(days=14)
            ).strftime("%Y-%m-%d %H:%M:%S")
            week_cut = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            latest_activity: dict[int, dict[str, Any]] = {}
            for event in session.scalars(
                select(Event)
                .where(Event.occurred_at >= activity_since)
                .order_by(Event.occurred_at.desc())
            ):
                if event.project_id is None:
                    continue
                entry = latest_activity.setdefault(
                    event.project_id,
                    {
                        "summary": event.summary,
                        "age_days": _age_days(event.occurred_at),
                        "week_count": 0,
                    },
                )
                if event.occurred_at >= week_cut:
                    entry["week_count"] += 1
            tasks = session.scalars(
                select(Task)
                .where(Task.status.in_(_LIVE_TASK_STATUSES))
                .order_by(Task.is_next.desc(), Task.id)
            ).all()
            stalls = session.scalars(
                select(StallEvent).where(StallEvent.resolved == 0).order_by(StallEvent.id.desc())
            ).all()
            triggers = session.scalars(
                select(ScheduledTrigger)
                .where(ScheduledTrigger.status == "pending")
                .order_by(ScheduledTrigger.fire_at)
            ).all()
            apps = session.scalars(
                select(JobApplication).order_by(
                    JobApplication.date_applied.desc(), JobApplication.id.desc()
                )
            ).all()
            captures = session.scalars(
                select(CapturedItem)
                .order_by(CapturedItem.id.desc())
                .limit(_RECENT_CAPTURES)
            ).all()
            project_names = {p.id: p.name for p in projects}
            hero = _pick_hero(session)
            if hero is not None:
                # Cache lookup only — page loads never trigger an API call
                # themselves; the page fetches /api/next-action when this is None.
                hero["step"] = self._cached_step(hero)
            week_ago = (datetime.now(self._tz) - timedelta(days=7)).strftime("%Y-%m-%d")
            counts = {
                "tasks": session.scalar(select(func.count()).select_from(Task)) or 0,
                "applications": len(apps),
                "captures": session.scalar(
                    select(func.count()).select_from(CapturedItem)
                ) or 0,
                "log_rows": session.scalar(
                    select(func.count()).select_from(ConversationLogEntry)
                ) or 0,
            }
            return {
                "hero": hero,
                "identity": self._identity(),
                "counts": counts,
                "apps_recent_count": sum(1 for a in apps if a.date_applied >= week_ago),
                "projects": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "status": p.status,
                        "priority": p.priority,
                        "description": p.description,
                        "goal": p.goal,
                        "bottleneck": p.current_bottleneck,
                        "active_milestone": active_milestones.get(p.id),
                        "activity": latest_activity.get(p.id),
                    }
                    for p in projects
                ],
                "tasks": [
                    {
                        "id": t.id,
                        "title": t.title,
                        "status": t.status,
                        "is_next": bool(t.is_next),
                        "project_id": t.project_id,
                        "project": project_names.get(t.project_id, ""),
                        "age_days": _age_days(t.updated_at),
                    }
                    for t in tasks
                ],
                "stalls": [
                    {
                        "id": s.id,
                        "description": s.description,
                        "project": project_names.get(s.project_id, "?"),
                        "created_at": s.created_at,
                    }
                    for s in stalls
                ],
                "triggers": [
                    {
                        "id": tr.id,
                        "kind": tr.kind,
                        "recurrence": tr.recurrence,
                        "message": tr.message_or_prompt,
                        "fire_at_local": self._format_local(tr.fire_at),
                    }
                    for tr in triggers
                ],
                "apps": [
                    {
                        "id": a.id,
                        "company": a.company,
                        "role": a.role,
                        "source": a.source,
                        "status": a.status,
                        "date_applied": a.date_applied,
                        "notes": a.notes,
                    }
                    for a in apps
                ],
                "captures": [
                    {
                        "id": c.id,
                        "content": c.content,
                        "category": c.category,
                        "source": c.source,
                        "created_at": c.created_at,
                    }
                    for c in captures
                ],
                "today_local": datetime.now(self._tz).strftime("%Y-%m-%d"),
            }

    def _format_local(self, fire_at_utc: str) -> str:
        try:
            local = parse_db_utc(fire_at_utc).astimezone(self._tz)
            return local.strftime("%a %b %d, %H:%M")
        except ValueError:
            return fire_at_utc


def _is_https(request: web.Request) -> bool:
    """True when the original request was HTTPS (Railway terminates TLS upstream)."""
    return request.headers.get("X-Forwarded-Proto", request.scheme) == "https"


def _parse_id(raw: Any) -> int | None:
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _strip_code_fences(text: str) -> str:
    """Unwrap a ```-fenced block if the model wrapped its reply in one."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    lines = lines[1:]  # opening fence (possibly with a language tag)
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _pick_hero(session: Session) -> dict[str, Any] | None:
    """The focus-zone target: the best live task down the priority ladder.

    Walks ACTIVE projects in priority order and returns the first one that has
    a candidate task — per-project candidacy is still the tool's own
    ``_next_task`` rule (is_next else oldest open), so the page can never
    spotlight a task the agent wouldn't. Empty state only when no active
    project has any live task.
    """
    projects = session.scalars(
        select(Project)
        .where(Project.status == "active")
        .order_by(Project.priority.desc(), Project.id)
    ).all()
    for project in projects:
        task = _next_task(session, project.id)
        if task is None:
            continue
        return {
            "project_id": project.id,
            "project": project.name,
            "task_id": task.id,
            "title": task.title,
            "updated_at": task.updated_at,
        }
    return None


def _age_days(updated_at: str) -> int:
    """Whole days since a DB timestamp; 0 when unparsable."""
    try:
        delta = datetime.now(timezone.utc) - parse_db_utc(updated_at)
    except (ValueError, TypeError):
        return 0
    return max(0, delta.days)


def _redirect_with_message(message: str) -> web.HTTPSeeOther:
    """Post/Redirect/Get back to the dashboard, carrying the result as a toast."""
    return web.HTTPSeeOther(f"/?msg={quote(message)}")


# -- rendering (server-side HTML; every DB string goes through html.escape) ------

_STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; margin: 0; }
body {
  background: #101216; color: #d5d9df;
  font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
  max-width: 1100px; margin: 0 auto; padding: 10px 14px 40px;
}
h1 { font-size: 16px; color: #f0f2f5; }
h2 {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
  color: #7d8590; margin: 14px 0 3px;
}
.muted { color: #7d8590; }
.small { font-size: 12px; }
b { color: #e8eaed; font-weight: 600; }
.topbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.topbar form { margin: 0; }
button, input[type=submit] {
  background: #262a31; color: #d5d9df; border: 1px solid #343a43;
  border-radius: 6px; padding: 4px 12px; font-size: 13px; cursor: pointer;
}
button.mini { padding: 2px 9px; font-size: 12px; line-height: 1.4; }
button.accent { background: #16341f; color: #55e08c; border-color: #275c38; }
select {
  background: #262a31; color: #d5d9df; border: 1px solid #343a43;
  border-radius: 6px; padding: 2px 6px; font-size: 12px;
}
input[type=password], input[type=text], input[type=date] {
  background: #191c21; color: #d5d9df; border: 1px solid #343a43;
  border-radius: 6px; padding: 7px 10px; font-size: 14px; width: 100%;
}
/* focus zone */
.hero {
  background: #14211a; border: 1px solid #275c38; border-left: 4px solid #3ddc84;
  border-radius: 10px; padding: 11px 14px; margin-bottom: 8px;
}
.hero-label {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
  color: #55e08c; margin-bottom: 4px;
}
.hero-task { font-size: 19px; font-weight: 700; color: #f2f4f6; line-height: 1.25; }
.hero.busy .hero-task { opacity: 0.55; }
.hero-parent { color: #7d8590; text-transform: none; letter-spacing: 0; }
.hero-note { color: #d3b45e; font-size: 12px; margin-top: 4px; min-height: 0; }
.hero-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 9px; align-items: center; }
.hero-actions form { margin: 0; }
.hero button.done {
  background: #1d4a2c; color: #66ffa6; border: 1px solid #2f7a48;
  font-size: 15px; font-weight: 600; padding: 8px 24px;
}
.stall-result {
  margin-top: 10px; padding: 8px 11px; background: #1c1f25;
  border: 1px solid #33290f; border-left: 3px solid #d3b45e;
  border-radius: 8px; font-size: 13px; white-space: pre-wrap;
}
.handoff { margin-top: 10px; }
.handoff-head {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 4px;
}
.handoff pre {
  background: #101216; border: 1px solid #2a2e36; border-radius: 8px;
  padding: 10px 12px; font-size: 12px; line-height: 1.5;
  white-space: pre-wrap; word-break: break-word; max-height: 320px;
  overflow-y: auto; user-select: all;
}
/* status strip */
.strip {
  display: flex; flex-wrap: wrap; gap: 4px 18px; padding: 6px 2px;
  font-size: 13px; color: #7d8590; border-bottom: 1px solid #23262d;
}
/* two-column layout; .single (sparse right column) stays one column and lets
   the work list use the full width */
.grid { display: grid; grid-template-columns: 1fr; gap: 0 24px; }
@media (min-width: 900px) { .grid { grid-template-columns: 1.15fr 0.85fr; } }
.grid.single { grid-template-columns: 1fr; }
/* let columns shrink below content width so ellipsized rows can't overflow */
.grid > div { min-width: 0; }
/* compact rows */
.projhead { display: flex; align-items: baseline; gap: 8px; margin-top: 8px; padding-bottom: 1px; }
.pname { font-weight: 600; color: #e8eaed; }
.pmeta {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  padding-bottom: 3px;
}
.pmeta b { color: #6fce93; font-weight: 600; }
.pmeta.activity { color: #8fa3b8; }
.trow {
  display: flex; align-items: center; gap: 8px; padding: 3px 0;
  border-bottom: 1px solid #1c1f25;
}
.trow form { margin: 0; display: flex; }
.ttitle {
  flex: 1 1 auto; min-width: 0; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
/* On narrow screens the controls would squeeze the title to nothing: let the
   row wrap so the title keeps its own full-width line. */
@media (max-width: 600px) {
  .trow { flex-wrap: wrap; }
  .ttitle { flex-basis: 100%; white-space: normal; }
}
.badge {
  font-size: 10.5px; padding: 1px 7px; border-radius: 9px;
  background: #262a31; color: #9aa1ab; white-space: nowrap;
}
.badge.active, .badge.open { background: #173225; color: #6fce93; }
.badge.in_progress { background: #162c3d; color: #62b0e8; }
.badge.paused, .badge.waiting { background: #33290f; color: #d3b45e; }
.badge.done { background: #262a31; color: #9aa1ab; }
.badge.next { background: #3b2320; color: #e8896f; }
.badge.applied, .badge.screen { background: #162c3d; color: #62b0e8; }
.badge.responded, .badge.offer { background: #173225; color: #6fce93; }
.badge.interview { background: #33290f; color: #d3b45e; }
.badge.rejected, .badge.ghosted { background: #262a31; color: #9aa1ab; }
.badge.stale { background: #3b2320; color: #e8896f; }
.badge.envlocal { background: #33290f; color: #d3b45e; border: 1px solid #5c4a1a; }
.badge.envprod { background: #173225; color: #6fce93; border: 1px solid #2c5b40; }
.topbar .row { display: flex; align-items: center; gap: 10px; }
.foot {
  margin-top: 20px; padding-top: 8px; border-top: 1px solid #23262d;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pipeline { font-size: 13px; color: #7d8590; padding: 2px 0 4px; }
.empty { color: #667080; font-style: italic; font-size: 13px; padding: 4px 0; }
.toast {
  background: #14273a; color: #62b0e8; border: 1px solid #1f4159;
  border-radius: 8px; padding: 7px 11px; margin-bottom: 10px; font-size: 13px;
}
/* collapsibles */
details { margin: 4px 0; }
summary {
  cursor: pointer; color: #7d8590; font-size: 13px; list-style: none;
  -webkit-user-select: none; user-select: none;
}
summary::-webkit-details-marker { display: none; }
summary::before { content: "+ "; color: #55e08c; }
details[open] > summary::before { content: "\\2212 "; }
.qa-row { display: flex; gap: 6px; margin-top: 6px; }
.qa-row input[type=text] { flex: 1 1 auto; }
.stack { display: flex; flex-direction: column; gap: 8px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 8px 0; }
@media (max-width: 480px) { .grid2 { grid-template-columns: 1fr; } }
.login-wrap { max-width: 320px; margin: 18vh auto 0; }
.login-wrap h1 { text-align: center; margin-bottom: 8px; }
.error { color: #e8896f; font-size: 14px; margin-top: 8px; }
"""


def _page(title: str, body: str, script: str = "") -> str:
    script_tag = f"<script>{script}</script>" if script else ""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}{script_tag}</body></html>"
    )


# Page script for the agent-backed hero: fetches the generated next action when
# no fresh cached one was embedded, and drives the shrink / regenerate / stall
# buttons. On any failure it falls back to the stored task title.
_HERO_SCRIPT = """
const hero = document.getElementById('hero');
if (hero) {
  const step = document.getElementById('hero-step');
  const note = document.getElementById('hero-note');
  const stored = hero.dataset.stored;
  let busy = false;
  async function agent(path, body) {
    if (busy) return null;
    busy = true;
    hero.classList.add('busy');
    const prev = step.textContent;
    step.textContent = 'Thinking\\u2026';
    note.textContent = '';
    try {
      const r = await fetch(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body || {}),
      });
      if (!r.ok) throw new Error(String(r.status));
      const data = await r.json();
      if (!data.ok) throw new Error('agent');
      step.textContent = data.text;
      return data;
    } catch (err) {
      step.textContent = prev !== 'Thinking\\u2026' ? prev : stored;
      note.textContent = 'Agent unavailable \\u2014 showing the stored task.';
      return null;
    } finally {
      busy = false;
      hero.classList.remove('busy');
    }
  }
  document.getElementById('btn-shrink').addEventListener('click', function () {
    agent('/api/shrink', {current: step.textContent});
  });
  document.getElementById('btn-regen').addEventListener('click', function () {
    agent('/api/next-action', {force: true});
  });
  const handoffBtn = document.getElementById('btn-handoff');
  const handoffWrap = document.getElementById('handoff-wrap');
  const handoffText = document.getElementById('handoff-text');
  const copyBtn = document.getElementById('btn-copy');
  handoffBtn.addEventListener('click', async function () {
    handoffBtn.disabled = true;
    handoffWrap.hidden = false;
    handoffText.textContent = 'Thinking\\u2026';
    try {
      const r = await fetch('/api/handoff', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}',
      });
      const data = await r.json();
      handoffText.textContent =
        data.ok ? data.text : 'Handoff failed \\u2014 try again.';
    } catch (err) {
      handoffText.textContent = 'Handoff failed \\u2014 try again.';
    }
    handoffBtn.disabled = false;
  });
  copyBtn.addEventListener('click', async function () {
    try {
      await navigator.clipboard.writeText(handoffText.textContent);
      copyBtn.textContent = 'Copied';
      setTimeout(function () { copyBtn.textContent = 'Copy'; }, 1500);
    } catch (err) {
      copyBtn.textContent = 'Select + copy manually';
    }
  });
  const stallBtn = document.getElementById('btn-stall');
  const stallBox = document.getElementById('stall-result');
  stallBtn.addEventListener('click', async function () {
    stallBtn.disabled = true;
    stallBox.hidden = false;
    stallBox.textContent = 'Checking\\u2026';
    try {
      const r = await fetch('/api/stall-check', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}',
      });
      const data = await r.json();
      stallBox.textContent = data.text || 'Stall check failed \\u2014 try again.';
    } catch (err) {
      stallBox.textContent = 'Stall check failed \\u2014 try again.';
    }
    stallBtn.disabled = false;
  });
  if (hero.dataset.generate === '1') agent('/api/next-action', {});
}
"""


def _html_response(text: str, status: int = 200) -> web.Response:
    return web.Response(text=text, status=status, content_type="text/html")


def _render_login(error: bool) -> str:
    error_html = "<p class='error'>Wrong password.</p>" if error else ""
    return _page(
        "Spotter — log in",
        "<div class='login-wrap'><h1>Spotter</h1>"
        "<form method='post' action='/login'>"
        "<input type='password' name='password' placeholder='Password' autofocus>"
        f"{error_html}"
        "<p style='margin-top:12px'><input type='submit' value='Log in'></p>"
        "</form></div>",
    )


def _render_index(state: dict[str, Any], timezone_name: str, message: str = "") -> str:
    toast = f"<p class='toast'>{html.escape(message)}</p>" if message else ""
    left = [
        "<h2>Work</h2>",
        _render_work(state["projects"], state["tasks"]),
        _render_stalls(state["stalls"]),
    ]
    right = [
        "<h2>Job applications</h2>",
        _render_pipeline(state["apps"]),
        _render_app_add(state["today_local"]),
        _render_apps(state["apps"]),
        f"<h2>Upcoming <span class='muted'>({html.escape(timezone_name)})</span></h2>",
        _render_triggers(state["triggers"]),
        _render_captures(state["captures"]),
    ]
    # Sparse right column -> single column; the work list gets the full width.
    grid_class = (
        "grid single"
        if len(state["apps"]) + len(state["triggers"]) <= _SPARSE_RIGHT_ITEMS
        else "grid"
    )
    identity = state.get("identity")
    badge = ""
    if identity:
        flavor = "envlocal" if identity["environment"] == "LOCAL" else "envprod"
        badge = (
            f"<span class='badge {flavor}' title='DB: {html.escape(identity['db_path'], quote=True)}'>"
            f"{html.escape(identity['environment'])} · {html.escape(identity['bot_kind'])} "
            f"{html.escape(identity['bot_id'])}</span>"
        )
    sections = [
        f"<div class='topbar'><span class='row'><h1>Spotter</h1>{badge}</span>"
        "<form method='post' action='/logout'><button class='mini'>Log out</button></form></div>",
        toast,
        _render_hero(state["hero"]),
        _render_strip(state),
        _render_quick_add(state["projects"]),
        f"<div class='{grid_class}'><div>{''.join(left)}</div><div>{''.join(right)}</div></div>",
        _render_footer(state),
    ]
    return _page("Spotter", "".join(sections), script=_HERO_SCRIPT)


def _render_footer(state: dict[str, Any]) -> str:
    """Identity + row counts: makes two environments instantly comparable."""
    identity = state.get("identity")
    counts = state.get("counts")
    if not identity or not counts:
        return ""
    return (
        "<div class='foot muted small'>"
        f"{html.escape(identity['environment'])} · {html.escape(identity['bot_kind'])} "
        f"{html.escape(identity['bot_id'])} · DB: {html.escape(identity['db_path'])} · "
        f"{counts['tasks']} tasks · {counts['applications']} applications · "
        f"{counts['captures']} captures · {counts['log_rows']} conversation rows"
        "</div>"
    )


def _render_hero(hero: dict[str, Any] | None) -> str:
    """The focus zone: an agent-generated concrete step, not the raw task title.

    Server-side it embeds the cached step when one is fresh; otherwise it shows
    the stored task title and marks itself ``data-generate='1'`` so the page
    script fetches a generated step (with a loading state) after load.
    """
    if hero is None:
        return (
            "<div class='hero'><div class='hero-label'>Next action</div>"
            "<div class='hero-task muted'>No active project with a live task. Add "
            "one below, or ask Spotter in chat what to start on.</div></div>"
        )
    step = hero.get("step")
    return (
        f"<div class='hero' id='hero' data-generate='{'0' if step else '1'}' "
        f"data-stored='{html.escape(hero['title'], quote=True)}'>"
        "<div class='hero-label'>"
        f"Next action · {html.escape(hero['project'])} "
        f"<span class='hero-parent'>· task: {html.escape(hero['title'])}</span></div>"
        f"<div class='hero-task' id='hero-step'>{html.escape(step or hero['title'])}</div>"
        "<div class='hero-note' id='hero-note'></div>"
        "<div class='hero-actions'>"
        "<form method='post' action='/tasks/status'>"
        f"<input type='hidden' name='id' value='{hero['task_id']}'>"
        "<input type='hidden' name='status' value='done'>"
        "<button class='done'>&#10003; Done</button></form>"
        "<button type='button' id='btn-shrink'>Too big &mdash; shrink it</button>"
        "<button type='button' id='btn-regen' title='Regenerate the next action'>&#8635;</button>"
        "<button type='button' id='btn-handoff' title='Ready-to-paste prompt for Claude Code'>Get the prompt</button>"
        "<button type='button' id='btn-stall'>Am I stalling?</button>"
        "</div>"
        "<div id='stall-result' class='stall-result' hidden></div>"
        "<div id='handoff-wrap' class='handoff' hidden>"
        "<div class='handoff-head'><span class='muted small'>Paste into Claude Code:</span>"
        "<button type='button' class='mini' id='btn-copy'>Copy</button></div>"
        "<pre id='handoff-text'></pre></div>"
        "</div>"
    )


def _render_strip(state: dict[str, Any]) -> str:
    next_fire = state["triggers"][0]["fire_at_local"] if state["triggers"] else "nothing scheduled"
    stall_count = len(state["stalls"])
    return (
        "<div class='strip'>"
        f"<span><b>{len(state['tasks'])}</b> open tasks</span>"
        f"<span><b>{stall_count}</b> stall{'s' if stall_count != 1 else ''}</span>"
        f"<span><b>{state['apps_recent_count']}</b> apps this week</span>"
        f"<span>next trigger: <b>{html.escape(next_fire)}</b></span>"
        "</div>"
    )


def _render_quick_add(projects: list[dict[str, Any]]) -> str:
    """Collapsed quick-add: one summary line expanding to two slim form rows."""
    options = "<option value=''>(no project)</option>" + "".join(
        f"<option value='{p['id']}'>{html.escape(p['name'])}</option>"
        for p in projects
        if p["status"] == "active"
    )
    return (
        "<details><summary>Quick add</summary>"
        "<form method='post' action='/tasks/add' class='qa-row'>"
        "<input type='text' name='title' placeholder='New task…'>"
        f"<select name='project_id'>{options}</select>"
        "<button class='accent'>Add</button></form>"
        "<form method='post' action='/captures/add' class='qa-row'>"
        "<input type='text' name='content' placeholder='Capture a thought, link, follow-up…'>"
        "<button>Capture</button></form>"
        "</details>"
    )


def _render_work(projects: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> str:
    """Open tasks grouped under compact project headers, in priority order."""
    if not projects and not tasks:
        return "<p class='empty'>No projects or tasks.</p>"
    by_project: dict[Any, list[dict[str, Any]]] = {}
    for t in tasks:
        by_project.setdefault(t["project_id"], []).append(t)
    parts: list[str] = []
    for p in projects:  # already ordered by priority desc
        group = by_project.pop(p["id"], [])
        parts.append(
            "<div class='projhead'>"
            f"<span class='pname'>{html.escape(p['name'])}</span>"
            f"<span class='badge {html.escape(p['status'])}'>{html.escape(p['status'])}</span>"
            f"<span class='muted small'>p{p['priority']} · {len(group)} open</span></div>"
        )
        meta = _render_project_meta(p)
        if meta:
            parts.append(meta)
        activity = _render_project_activity(p)
        if activity:
            parts.append(activity)
        parts.extend(_render_task_row(t) for t in group)
    unlinked = by_project.pop(None, [])
    if unlinked:
        parts.append("<div class='projhead'><span class='pname muted'>No project</span></div>")
        parts.extend(_render_task_row(t) for t in unlinked)
    return "".join(parts)


def _render_project_meta(project: dict[str, Any]) -> str:
    """Compact goal / active milestone / bottleneck line under a project header."""
    bits = []
    if project.get("active_milestone"):
        bits.append(f"<b>&#9656; {html.escape(project['active_milestone'])}</b>")
    if project.get("goal"):
        bits.append(f"goal: {html.escape(project['goal'])}")
    if project.get("bottleneck"):
        bits.append(f"bottleneck: {html.escape(project['bottleneck'])}")
    if not bits:
        return ""
    full = " · ".join(bits)
    plain_bits = [
        b
        for b in (
            project.get("active_milestone"),
            project.get("goal"),
            project.get("bottleneck"),
        )
        if b
    ]
    tooltip = html.escape(" · ".join(plain_bits), quote=True)
    return f"<div class='pmeta muted small' title='{tooltip}'>{full}</div>"


def _render_project_activity(project: dict[str, Any]) -> str:
    """Latest ingested event for the project: ground truth about movement."""
    activity = project.get("activity")
    if not activity:
        return ""
    age = activity["age_days"]
    when = "today" if age == 0 else f"{age}d ago"
    extra = (
        f" · {activity['week_count']} events this week"
        if activity.get("week_count", 0) > 1
        else ""
    )
    text = f"{when}: {activity['summary']}{extra}"
    return (
        f"<div class='pmeta muted small activity' title='{html.escape(text, quote=True)}'>"
        f"&#9889; {html.escape(text)}</div>"
    )


def _render_task_row(task: dict[str, Any]) -> str:
    """One tight row: title, badge, ✓ button, auto-submitting status dropdown."""
    statuses = list(_STATUS_CHOICES)
    if task["status"] not in statuses:
        statuses.insert(0, task["status"])
    options = "".join(
        f"<option value='{html.escape(s)}'{' selected' if s == task['status'] else ''}>"
        f"{html.escape(s)}</option>"
        for s in statuses
    )
    next_badge = "<span class='badge next'>next</span>" if task["is_next"] else ""
    # Staleness: any task untouched 3+ days gets a quiet age marker; a stale
    # is_next task gets a louder one — it's the thing supposedly in progress.
    age = task.get("age_days", 0)
    if age >= _STALE_AFTER_DAYS:
        tip = f"title='no status change in {age} days'"
        if task["is_next"]:
            next_badge += f" <span class='badge stale' {tip}>{age}d &mdash; stalling?</span>"
        else:
            next_badge += f" <span class='muted small' {tip}>{age}d</span>"
    return (
        "<div class='trow'>"
        f"<span class='ttitle'>{html.escape(task['title'])}</span>{next_badge}"
        f"<span class='badge {html.escape(task['status'])}'>{html.escape(task['status'])}</span>"
        "<form method='post' action='/tasks/status'>"
        f"<input type='hidden' name='id' value='{task['id']}'>"
        "<input type='hidden' name='status' value='done'>"
        "<button class='mini accent' title='Mark done'>&#10003;</button></form>"
        "<form method='post' action='/tasks/status'>"
        f"<input type='hidden' name='id' value='{task['id']}'>"
        f"<select name='status' onchange='this.form.submit()'>{options}</select></form>"
        "</div>"
    )


def _render_pipeline(apps: list[dict[str, Any]]) -> str:
    """One glanceable funnel line: counts per status."""
    if not apps:
        return ""
    counts: dict[str, int] = {}
    for a in apps:
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    funnel = ["applied", "responded", "screen", "interview", "offer"]
    parts = [f"<b>{counts.get(s, 0)}</b> {s}" for s in funnel]
    parts += [f"<b>{counts[s]}</b> {s}" for s in ("rejected", "ghosted") if counts.get(s)]
    return f"<div class='pipeline'>{' · '.join(parts)}</div>"


def _render_app_add(today_local: str) -> str:
    return (
        "<details><summary>Add application</summary>"
        "<form method='post' action='/apps/add' class='stack'>"
        "<div class='grid2'>"
        "<input type='text' name='company' placeholder='Company'>"
        "<input type='text' name='role' placeholder='Role'>"
        "<input type='text' name='source' placeholder='Source (LinkedIn, referral…)'>"
        f"<input type='date' name='date_applied' value='{html.escape(today_local)}'>"
        "</div>"
        "<input type='text' name='notes' placeholder='Notes (optional)'>"
        "<div><button class='accent'>Add application</button></div></form></details>"
    )


def _render_apps(apps: list[dict[str, Any]]) -> str:
    if not apps:
        return "<p class='empty'>No applications tracked yet.</p>"
    rows = []
    for a in apps:
        options = "".join(
            f"<option value='{s}'{' selected' if s == a['status'] else ''}>{s}</option>"
            for s in _APP_STATUSES
        )
        tooltip_bits = [b for b in (a["source"], a["notes"]) if b]
        tooltip = f" title='{html.escape(' · '.join(tooltip_bits), quote=True)}'" if tooltip_bits else ""
        rows.append(
            f"<div class='trow'{tooltip}>"
            f"<span class='ttitle'>{html.escape(a['company'])} "
            f"<span class='muted'>— {html.escape(a['role'])}</span></span>"
            f"<span class='badge {html.escape(a['status'])}'>{html.escape(a['status'])}</span>"
            "<form method='post' action='/apps/status'>"
            f"<input type='hidden' name='id' value='{a['id']}'>"
            f"<select name='status' onchange='this.form.submit()'>{options}</select></form>"
            f"<span class='muted small' style='white-space:nowrap'>{html.escape(a['date_applied'])}</span>"
            "</div>"
        )
    return "".join(rows)


def _render_captures(captures: list[dict[str, Any]]) -> str:
    if not captures:
        return ""
    rows = "".join(
        "<div class='trow'>"
        f"<span class='ttitle' title='{html.escape(c['content'], quote=True)}'>"
        f"{html.escape(c['content'])}</span>"
        + (f"<span class='badge'>{html.escape(c['category'])}</span>" if c["category"] else "")
        + f"<span class='muted small'>{html.escape(c['source'])}</span></div>"
        for c in captures
    )
    return (
        f"<details><summary>Recent captures ({len(captures)})</summary>{rows}</details>"
    )


def _render_stalls(stalls: list[dict[str, Any]]) -> str:
    """Compact stall rows; the whole section disappears when there are none."""
    if not stalls:
        return ""
    rows = "".join(
        "<div class='trow'>"
        f"<span class='ttitle' title='{html.escape(s['description'], quote=True)}'>"
        f"<b>{html.escape(s['project'])}</b> — avoiding: {html.escape(s['description'])}</span>"
        "<form method='post' action='/stalls/resolve'>"
        f"<input type='hidden' name='id' value='{s['id']}'>"
        "<button class='mini'>Resolve</button></form></div>"
        for s in stalls
    )
    return f"<h2>Stalls</h2>{rows}"


def _render_triggers(triggers: list[dict[str, Any]]) -> str:
    if not triggers:
        return "<p class='empty'>Nothing scheduled.</p>"
    rows = []
    for tr in triggers:
        recurrence = f" · {html.escape(tr['recurrence'])}" if tr["recurrence"] else ""
        rows.append(
            "<div class='trow'>"
            f"<span style='white-space:nowrap'><b>{html.escape(tr['fire_at_local'])}</b></span>"
            f"<span class='badge'>{html.escape(tr['kind'])}{recurrence}</span>"
            f"<span class='ttitle muted small' title='{html.escape(tr['message'], quote=True)}'>"
            f"{html.escape(tr['message'])}</span></div>"
        )
    return "".join(rows)
