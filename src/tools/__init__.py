"""Spotter tool handlers and the name -> handler dispatch registry.

Waves 1-2 implement capture_item, query_memory, surface_next_action, and
name_the_stall. The remaining four tools in tools_schema.json are still passed to
the API but have no handler yet; the brain returns a graceful "not implemented"
tool_result if Claude calls one.
"""

from __future__ import annotations

from .base import ToolContext, ToolHandler
from .capture import capture_item
from .memory import query_memory
from .next_action import surface_next_action
from .stall import name_the_stall

# name -> handler. Wave 1 + Wave 2 tools are registered.
TOOL_HANDLERS: dict[str, ToolHandler] = {
    "capture_item": capture_item,
    "query_memory": query_memory,
    "surface_next_action": surface_next_action,
    "name_the_stall": name_the_stall,
}

__all__ = ["TOOL_HANDLERS", "ToolContext", "ToolHandler"]
