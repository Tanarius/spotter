"""Spotter tool handlers and the name -> handler dispatch registry.

Waves 1-4 implement seven of the original eight tools: capture_item,
query_memory, surface_next_action, name_the_stall, log_blocker,
schedule_intent, and draft_message — plus update_task_status, added post-Wave 4. update_workspace_doc is intentionally deferred: it stays defined in
tools_schema.json with "deferred": true, which keeps it OUT of the tool list the
model receives (see load_config) until the optional Google Docs step. If a call
somehow arrives anyway, the brain still returns a graceful "not available yet"
tool_result.
"""

from __future__ import annotations

from .base import ToolContext, ToolHandler
from .blocker import log_blocker
from .capture import capture_item
from .draft import draft_message
from .intent import schedule_intent
from .memory import query_memory
from .next_action import surface_next_action
from .stall import name_the_stall
from .status import update_task_status

# name -> handler. Eight tools registered; update_workspace_doc deferred.
TOOL_HANDLERS: dict[str, ToolHandler] = {
    "capture_item": capture_item,
    "query_memory": query_memory,
    "surface_next_action": surface_next_action,
    "name_the_stall": name_the_stall,
    "log_blocker": log_blocker,
    "schedule_intent": schedule_intent,
    "draft_message": draft_message,
    "update_task_status": update_task_status,
}

__all__ = ["TOOL_HANDLERS", "ToolContext", "ToolHandler"]
