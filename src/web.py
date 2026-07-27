"""Password-gated web dashboard served from the Spotter process.

Web Step 1 scope: read-only state view. An aiohttp application runs on the SAME
asyncio event loop as python-telegram-bot and APScheduler — ``Dashboard.start``
is awaited from the bot's ``post_init`` hook, so no second process, thread, or
event loop exists. All database reads go through ``asyncio.to_thread`` with a
short-lived session from the shared ``session_factory``, the exact pattern the
Telegram handler already uses for ``Brain.respond``, so DB access stays
thread-safe by construction.

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
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import Config
from .db.models import Project, ScheduledTrigger, StallEvent, Task
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
        return _html_response(_render_index(state, self._config.timezone))

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
            project_names = {p.id: p.name for p in projects}
            return {
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


# -- rendering (server-side HTML; every DB string goes through html.escape) ------

_STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; margin: 0; }
body {
  background: #14161a; color: #d7dae0;
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  max-width: 720px; margin: 0 auto; padding: 16px 12px 48px;
}
h1 { font-size: 19px; margin: 4px 0 16px; color: #f0f2f5; }
h2 {
  font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em;
  color: #8a919c; margin: 24px 0 8px;
}
.card {
  background: #1c1f25; border: 1px solid #2a2e36; border-radius: 8px;
  padding: 10px 12px; margin-bottom: 8px;
}
.row { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.title { font-weight: 600; color: #eceef1; }
.muted { color: #8a919c; font-size: 13px; }
.badge {
  font-size: 11px; padding: 1px 8px; border-radius: 10px;
  background: #2a2e36; color: #aeb4bd; white-space: nowrap;
}
.badge.active, .badge.open { background: #173225; color: #6fce93; }
.badge.in_progress { background: #162c3d; color: #62b0e8; }
.badge.paused, .badge.waiting { background: #33290f; color: #d3b45e; }
.badge.done { background: #2a2e36; color: #8a919c; }
.badge.next { background: #3b2320; color: #e8896f; }
.empty { color: #6b7280; font-style: italic; padding: 4px 2px; }
.topbar { display: flex; justify-content: space-between; align-items: center; }
.topbar form { margin: 0; }
button, input[type=submit] {
  background: #2a2e36; color: #d7dae0; border: 1px solid #3a3f49;
  border-radius: 6px; padding: 6px 14px; font-size: 14px; cursor: pointer;
}
input[type=password] {
  background: #1c1f25; color: #d7dae0; border: 1px solid #3a3f49;
  border-radius: 6px; padding: 8px 10px; font-size: 15px; width: 100%;
}
.login-wrap { max-width: 320px; margin: 18vh auto 0; }
.login-wrap h1 { text-align: center; }
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


def _render_index(state: dict[str, Any], timezone_name: str) -> str:
    sections = [
        "<div class='topbar'><h1>Spotter</h1>"
        "<form method='post' action='/logout'><button>Log out</button></form></div>",
        "<h2>Projects</h2>",
        _render_projects(state["projects"]),
        "<h2>Open tasks</h2>",
        _render_tasks(state["tasks"]),
        "<h2>Active stalls</h2>",
        _render_stalls(state["stalls"]),
        f"<h2>Upcoming triggers <span class='muted'>({html.escape(timezone_name)})</span></h2>",
        _render_triggers(state["triggers"]),
    ]
    return _page("Spotter", "".join(sections))


def _render_projects(projects: list[dict[str, Any]]) -> str:
    if not projects:
        return "<p class='empty'>No projects.</p>"
    cards = []
    for p in projects:
        description = (
            f"<div class='muted'>{html.escape(p['description'])}</div>"
            if p["description"]
            else ""
        )
        cards.append(
            "<div class='card'><div class='row'>"
            f"<span class='title'>{html.escape(p['name'])}</span>"
            f"<span class='badge {html.escape(p['status'])}'>{html.escape(p['status'])}</span>"
            f"<span class='muted'>priority {p['priority']}</span>"
            f"</div>{description}</div>"
        )
    return "".join(cards)


def _render_tasks(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "<p class='empty'>No open tasks.</p>"
    cards = []
    for t in tasks:
        next_badge = "<span class='badge next'>next</span>" if t["is_next"] else ""
        project = f"<span class='muted'>{html.escape(t['project'])}</span>" if t["project"] else ""
        cards.append(
            "<div class='card'><div class='row'>"
            f"<span class='title'>#{t['id']} {html.escape(t['title'])}</span>"
            f"<span class='badge {html.escape(t['status'])}'>{html.escape(t['status'])}</span>"
            f"{next_badge}{project}</div></div>"
        )
    return "".join(cards)


def _render_stalls(stalls: list[dict[str, Any]]) -> str:
    if not stalls:
        return "<p class='empty'>No active stalls.</p>"
    cards = []
    for s in stalls:
        cards.append(
            "<div class='card'><div class='row'>"
            f"<span class='title'>{html.escape(s['project'])}</span>"
            f"<span class='muted'>#{s['id']} · {html.escape(s['created_at'])}</span></div>"
            f"<div>Avoiding: {html.escape(s['description'])}</div></div>"
        )
    return "".join(cards)


def _render_triggers(triggers: list[dict[str, Any]]) -> str:
    if not triggers:
        return "<p class='empty'>Nothing scheduled.</p>"
    cards = []
    for tr in triggers:
        recurrence = f" · {html.escape(tr['recurrence'])}" if tr["recurrence"] else ""
        cards.append(
            "<div class='card'><div class='row'>"
            f"<span class='title'>{html.escape(tr['fire_at_local'])}</span>"
            f"<span class='badge'>{html.escape(tr['kind'])}{recurrence}</span></div>"
            f"<div class='muted'>{html.escape(tr['message'])}</div></div>"
        )
    return "".join(cards)
