"""Configuration loading for Spotter.

Centralizes all environment- and file-based configuration behind a single typed
``Config`` object. Nothing elsewhere in the codebase should read ``os.environ``
or open ``prompts.yaml`` directly — everything flows through ``load_config()``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Repo root = parent of the ``src`` package directory. Resolving from this file
# keeps paths stable regardless of the process's working directory.
ROOT_DIR: Path = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _require_int(name: str) -> int:
    raw = _require(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _get(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _optional(name: str) -> str:
    """Return the env var if set, else empty string. Never fails boot."""
    return os.environ.get(name, "").strip()


def _optional_int(name: str) -> int | None:
    """Parse an optional integer env var; None when unset."""
    raw = _optional(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"Environment variable {name} must be an integer, got {raw!r}"
        ) from exc


@dataclass(frozen=True)
class Config:
    """Immutable, typed view of all Spotter configuration."""

    # Secrets / identity
    telegram_bot_token: str
    telegram_allowed_user_id: int
    # Anthropic is required as of Step 4 (the brain calls Claude). Groq stays
    # optional until Step 6; absence does not fail boot.
    anthropic_api_key: str
    groq_api_key: str

    # Behavior
    default_model: str
    db_path: Path
    brief_time: str
    evening_time: str
    timezone: str

    # Web dashboard. The dashboard only serves when dashboard_password is set —
    # an empty value means the web server is never started (refuse rather than
    # run open). web_port comes from PORT, which Railway injects for HTTP routing.
    dashboard_password: str
    web_port: int

    # Deployment fallback: seed content when seed/context.yaml is absent (e.g. on
    # Railway, where the file is gitignored). Empty string when the file is used.
    seed_context_yaml: str

    # Loaded artifacts
    prompts: dict[str, Any]
    # Anthropic tool definitions, loaded verbatim from tools_schema.json and
    # passed straight to the Messages API.
    tools: list[dict[str, Any]]

    # True when TELEGRAM_DEV_BOT_TOKEN supplied the token: a local run is
    # polling a separate dev bot and cannot 409 against the deployed poller.
    # Defaulted so existing Config(...) constructions stay valid.
    using_dev_bot: bool = False
    # How many timestamped DB backups to retain (weekly job + manual runs).
    backup_retain: int = 4
    # RAILWAY_ENVIRONMENT when running on Railway, else "". Drives the
    # environment badge on the dashboard and the absolute-DB_PATH boot guard.
    railway_env: str = ""
    # Shared secret for GitHub webhook signature verification. Unset means the
    # /webhooks/github endpoint is not served at all (refuse rather than open).
    github_webhook_secret: str = ""
    # Shared secret for Claude Code session notes (X-Spotter-Secret header).
    # Same refusal pattern: unset means /webhooks/session is not served.
    session_note_secret: str = ""
    # Voyage AI key for semantic retrieval embeddings. Unset = retrieval
    # degrades to keyword scoring (never an error).
    voyage_api_key: str = ""
    embed_model: str = "voyage-3.5-lite"
    # Daily conditions-engine check (at most one nudge/day), HH:MM local.
    nudge_time: str = "13:00"
    # Groq Whisper model for Telegram voice messages.
    groq_whisper_model: str = "whisper-large-v3-turbo"

    @property
    def environment_label(self) -> str:
        """Human label for where this process runs: RAILWAY or LOCAL."""
        return "RAILWAY" if self.railway_env else "LOCAL"

    @property
    def bot_id(self) -> str:
        """The numeric bot id (public half of the token) for identity display."""
        return self.telegram_bot_token.split(":", 1)[0]

    @property
    def prompts_path(self) -> Path:
        return ROOT_DIR / "prompts.yaml"

    @property
    def tools_path(self) -> Path:
        return ROOT_DIR / "tools_schema.json"


def load_config() -> Config:
    """Load environment variables and prompts.yaml into a typed ``Config``.

    Reads ``.env`` from the repo root (if present); real environment variables
    always take precedence over ``.env`` values.
    """
    load_dotenv(ROOT_DIR / ".env")

    prompts_path = ROOT_DIR / "prompts.yaml"
    if not prompts_path.exists():
        raise ConfigError(f"prompts.yaml not found at {prompts_path}")
    with prompts_path.open("r", encoding="utf-8") as fh:
        prompts = yaml.safe_load(fh) or {}

    tools_path = ROOT_DIR / "tools_schema.json"
    if not tools_path.exists():
        raise ConfigError(f"tools_schema.json not found at {tools_path}")
    with tools_path.open("r", encoding="utf-8") as fh:
        raw_tools = json.load(fh)
    # Entries flagged "deferred": true stay in the file (e.g. the not-yet-built
    # update_workspace_doc Google Docs tool) but are never offered to the model.
    # The flag itself is stripped from active entries — it isn't an API field.
    tools = [
        {key: value for key, value in tool.items() if key != "deferred"}
        for tool in raw_tools
        if not tool.get("deferred", False)
    ]

    db_path_raw = _get("DB_PATH", "data/spotter.db")
    db_path = Path(db_path_raw)
    railway_env = _optional("RAILWAY_ENVIRONMENT")
    if railway_env and not db_path.is_absolute():
        # A relative DB_PATH on Railway resolves inside the container's
        # ephemeral filesystem: the database (and its backups) are silently
        # destroyed on every deploy. Refuse loudly instead of losing data.
        raise ConfigError(
            f"DB_PATH must be ABSOLUTE on Railway (got {db_path_raw!r}). "
            "A relative path lives on ephemeral container storage and is wiped "
            "on every deploy. Mount a volume at /data and set "
            "DB_PATH=/data/spotter.db."
        )
    if not db_path.is_absolute():
        db_path = ROOT_DIR / db_path

    # Dev bot override: TELEGRAM_BOT_TOKEN is always required (parity with
    # production), but a set TELEGRAM_DEV_BOT_TOKEN wins for this process so
    # local runs poll a separate bot and never fight Railway over getUpdates.
    # TELEGRAM_DEV_ALLOWED_USER_ID optionally overrides the allowlist with it.
    prod_token = _require("TELEGRAM_BOT_TOKEN")
    dev_token = _optional("TELEGRAM_DEV_BOT_TOKEN")
    allowed_user_id = _require_int("TELEGRAM_ALLOWED_USER_ID")
    if dev_token:
        dev_user_id = _optional_int("TELEGRAM_DEV_ALLOWED_USER_ID")
        if dev_user_id is not None:
            allowed_user_id = dev_user_id

    return Config(
        telegram_bot_token=dev_token or prod_token,
        telegram_allowed_user_id=allowed_user_id,
        using_dev_bot=bool(dev_token),
        backup_retain=int(_get("BACKUP_RETAIN", "4")),
        railway_env=railway_env,
        github_webhook_secret=_optional("GITHUB_WEBHOOK_SECRET"),
        session_note_secret=_optional("SESSION_NOTE_SECRET"),
        voyage_api_key=_optional("VOYAGE_API_KEY"),
        embed_model=_get("EMBED_MODEL", "voyage-3.5-lite"),
        nudge_time=_get("NUDGE_TIME", "13:00"),
        groq_whisper_model=_get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"),
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        groq_api_key=_optional("GROQ_API_KEY"),
        default_model=_get("DEFAULT_MODEL", "claude-sonnet-4-6"),
        db_path=db_path,
        dashboard_password=_optional("DASHBOARD_PASSWORD"),
        web_port=int(_get("PORT", "8080")),
        brief_time=_get("BRIEF_TIME", "08:00"),
        evening_time=_get("EVENING_TIME", "18:00"),
        timezone=_get("TIMEZONE", "America/Chicago"),
        seed_context_yaml=_optional("SEED_CONTEXT_YAML"),
        prompts=prompts,
        tools=tools,
    )
