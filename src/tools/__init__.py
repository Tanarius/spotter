"""Spotter tool handlers and the name -> handler dispatch registry.

Wave 1 implements ``capture_item`` and ``query_memory``. The remaining six tools
in tools_schema.json are still passed to the API but have no handler yet; the
brain returns a graceful "not implemented" tool_result if Claude calls one.
"""

from __future__ import annotations

from .base import ToolContext, ToolHandler
from .capture import capture_item
from .memory import query_memory

# name -> handler. Only the Wave 1 tools are registered.
TOOL_HANDLERS: dict[str, ToolHandler] = {
    "capture_item": capture_item,
    "query_memory": query_memory,
}

__all__ = ["TOOL_HANDLERS", "ToolContext", "ToolHandler"]
