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
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import Config
from .db.models import (
    CapturedItem,
    JobApplication,
    Project,
    ScheduledTrigger,
    StallEvent,
    Task,
)
from .tools.base import ToolContext
from .tools.capture import capture_item
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


class Dashboard:
    """The web dashboard: owns the aiohttp app and its lifecycle."""

    def __init__(self, config: Config, session_factory: sessionmaker[Session]) -> None:
        if not config.dashboard_password:
            raise ValueError("Dashboard requires DASHBOARD_PASSWORD to be set")
        self._config = config
        self._session_factory = session_factory
        self._tz = ZoneInfo(config.timezone)
        self._runner: web.AppRunner | None = None

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
        if request.path == "/login" or self._is_authenticated(request):
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
        raise _redirect_with_message(result)

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

    def _load_state(self) -> dict[str, Any]:
        """Snapshot everything the page shows into plain dicts (no live ORM rows)."""
        with self._session_factory() as session:
            projects = session.scalars(
                select(Project).order_by(Project.priority.desc(), Project.id)
            ).all()
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
            # The hero next action: among live is_next tasks on active projects,
            # the one whose project has the highest priority.
            priorities = {p.id: p.priority for p in projects}
            active_ids = {p.id for p in projects if p.status == "active"}
            next_flagged = sorted(
                (t for t in tasks if t.is_next and t.project_id in active_ids),
                key=lambda t: (-priorities.get(t.project_id, 0), t.id),
            )
            hero = (
                {
                    "id": next_flagged[0].id,
                    "title": next_flagged[0].title,
                    "project": project_names.get(next_flagged[0].project_id, ""),
                }
                if next_flagged
                else None
            )
            week_ago = (datetime.now(self._tz) - timedelta(days=7)).strftime("%Y-%m-%d")
            return {
                "hero": hero,
                "apps_recent_count": sum(1 for a in apps if a.date_applied >= week_ago),
                "projects": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "status": p.status,
                        "priority": p.priority,
                        "description": p.description,
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


def _redirect_with_message(message: str) -> web.HTTPSeeOther:
    """Post/Redirect/Get back to the dashboard, carrying the result as a toast."""
    return web.HTTPSeeOther(f"/?msg={quote(message)}")


# -- rendering (server-side HTML; every DB string goes through html.escape) ------

_STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; margin: 0; }
body {
  background: #101216; color: #d5d9df;
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
  max-width: 1100px; margin: 0 auto; padding: 12px 14px 48px;
}
h1 { font-size: 16px; color: #f0f2f5; }
h2 {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
  color: #7d8590; margin: 20px 0 4px;
}
.muted { color: #7d8590; }
.small { font-size: 12px; }
b { color: #e8eaed; font-weight: 600; }
.topbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
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
  border-radius: 10px; padding: 14px 16px; margin-bottom: 10px;
}
.hero-label {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
  color: #55e08c; margin-bottom: 4px;
}
.hero-task { font-size: 21px; font-weight: 700; color: #f2f4f6; line-height: 1.25; }
.hero form { margin-top: 12px; }
.hero button {
  background: #1d4a2c; color: #66ffa6; border: 1px solid #2f7a48;
  font-size: 15px; font-weight: 600; padding: 8px 24px;
}
/* status strip */
.strip {
  display: flex; flex-wrap: wrap; gap: 4px 18px; padding: 8px 2px;
  font-size: 13px; color: #7d8590; border-bottom: 1px solid #23262d;
}
/* two-column layout */
.grid { display: grid; grid-template-columns: 1fr; gap: 0 32px; }
@media (min-width: 900px) { .grid { grid-template-columns: 1.15fr 0.85fr; } }
/* let columns shrink below content width so ellipsized rows can't overflow */
.grid > div { min-width: 0; }
/* compact rows */
.projhead { display: flex; align-items: baseline; gap: 8px; margin-top: 12px; padding-bottom: 2px; }
.pname { font-weight: 600; color: #e8eaed; }
.trow {
  display: flex; align-items: center; gap: 8px; padding: 4px 0;
  border-bottom: 1px solid #1c1f25;
}
.trow form { margin: 0; display: flex; }
.ttitle {
  flex: 1 1 auto; min-width: 0; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
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
.pipeline { font-size: 13px; color: #7d8590; padding: 2px 0 4px; }
.empty { color: #667080; font-style: italic; font-size: 13px; padding: 4px 0; }
.toast {
  background: #14273a; color: #62b0e8; border: 1px solid #1f4159;
  border-radius: 8px; padding: 7px 11px; margin-bottom: 10px; font-size: 13px;
}
/* collapsibles */
details { margin: 6px 0; }
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


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


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
    sections = [
        "<div class='topbar'><h1>Spotter</h1>"
        "<form method='post' action='/logout'><button class='mini'>Log out</button></form></div>",
        toast,
        _render_hero(state["hero"]),
        _render_strip(state),
        _render_quick_add(state["projects"]),
        f"<div class='grid'><div>{''.join(left)}</div><div>{''.join(right)}</div></div>",
    ]
    return _page("Spotter", "".join(sections))


def _render_hero(hero: dict[str, Any] | None) -> str:
    """The focus zone: the single next action, or a nudge to pick one."""
    if hero is None:
        return (
            "<div class='hero'><div class='hero-label'>Next action</div>"
            "<div class='hero-task muted'>Nothing is flagged as next. Pick one task "
            "below, or ask Spotter in chat what to start on.</div></div>"
        )
    return (
        "<div class='hero'>"
        f"<div class='hero-label'>Next action · {html.escape(hero['project'])}</div>"
        f"<div class='hero-task'>{html.escape(hero['title'])}</div>"
        "<form method='post' action='/tasks/status'>"
        f"<input type='hidden' name='id' value='{hero['id']}'>"
        "<input type='hidden' name='status' value='done'>"
        "<button>&#10003; Done</button></form></div>"
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
        parts.extend(_render_task_row(t) for t in group)
    unlinked = by_project.pop(None, [])
    if unlinked:
        parts.append("<div class='projhead'><span class='pname muted'>No project</span></div>")
        parts.extend(_render_task_row(t) for t in unlinked)
    return "".join(parts)


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
