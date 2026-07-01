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
    timezone: str

    # Deployment fallback: seed content when seed/context.yaml is absent (e.g. on
    # Railway, where the file is gitignored). Empty string when the file is used.
    seed_context_yaml: str

    # Loaded artifacts
    prompts: dict[str, Any]
    # Anthropic tool definitions, loaded verbatim from tools_schema.json and
    # passed straight to the Messages API.
    tools: list[dict[str, Any]]

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
        tools = json.load(fh)

    db_path_raw = _get("DB_PATH", "data/spotter.db")
    db_path = Path(db_path_raw)
    if not db_path.is_absolute():
        db_path = ROOT_DIR / db_path

    return Config(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_allowed_user_id=_require_int("TELEGRAM_ALLOWED_USER_ID"),
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        groq_api_key=_optional("GROQ_API_KEY"),
        default_model=_get("DEFAULT_MODEL", "claude-sonnet-4-6"),
        db_path=db_path,
        brief_time=_get("BRIEF_TIME", "07:00"),
        timezone=_get("TIMEZONE", "America/Chicago"),
        seed_context_yaml=_optional("SEED_CONTEXT_YAML"),
        prompts=prompts,
        tools=tools,
    )
