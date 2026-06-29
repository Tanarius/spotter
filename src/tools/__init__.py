"""Spotter tool handlers and the name -> handler dispatch registry.

Waves 1-3 implement capture_item, query_memory, surface_next_action,
name_the_stall, log_blocker, and schedule_intent. The remaining two tools in
tools_schema.json (draft_message, update_workspace_doc) are still passed to the
API but have no handler yet; the brain returns a graceful "not implemented"
tool_result if Claude calls one.
"""

from __future__ import annotations

from .base import ToolContext, ToolHandler
from .blocker import log_blocker
from .capture import capture_item
from .intent import schedule_intent
from .memory import query_memory
from .next_action import surface_next_action
from .stall import name_the_stall

# name -> handler. Wave 1 + Wave 2 + Wave 3 tools are registered.
TOOL_HANDLERS: dict[str, ToolHandler] = {
    "capture_item": capture_item,
    "query_memory": query_memory,
    "surface_next_action": surface_next_action,
    "name_the_stall": name_the_stall,
    "log_blocker": log_blocker,
    "schedule_intent": schedule_intent,
}

__all__ = ["TOOL_HANDLERS", "ToolContext", "ToolHandler"]
