"""Shared types for Spotter tool handlers.

A handler takes a :class:`ToolContext` (an open, soon-to-be-committed session
plus config) and the tool's already-parsed ``input`` dict, and returns a string.
That string becomes the ``tool_result`` content Claude reads on the next turn —
so write it for the model, concise and unambiguous.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..config import Config


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool handler needs to do its work."""

    session: Session
    config: Config


# A tool handler: (context, parsed tool input) -> result text for Claude.
ToolHandler = Callable[[ToolContext, dict[str, Any]], str]
