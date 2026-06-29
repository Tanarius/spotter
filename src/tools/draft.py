"""draft_message — produce a draft for the user to review. V1 never sends."""

from __future__ import annotations

from typing import Any

from .base import ToolContext

_DEFAULT_TONE = "direct and brief, matching the user's voice"
# The three inputs a draft can't be written without.
_REQUIRED = ("message_type", "recipient", "purpose")


def draft_message(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """Acknowledge the gathered details and steer the model to write the draft.

    Stateless — no DB write beyond the brain's normal conversation_log /
    tool_calls entry. The handler does not generate the draft itself; it confirms
    the inputs (and flags any missing must-haves), then the model writes the draft
    in its reply and asks for approval, per the system prompt's "ALWAYS draft,
    present for approval" rule.
    """
    message_type = (tool_input.get("message_type") or "").strip()
    recipient = (tool_input.get("recipient") or "").strip()
    purpose = (tool_input.get("purpose") or "").strip()
    tone = (tool_input.get("tone") or "").strip() or _DEFAULT_TONE
    background = (tool_input.get("background") or "").strip()

    missing = [name for name in _REQUIRED if not tool_input.get(name)]
    if missing:
        return (
            "Can't draft yet — missing: "
            + ", ".join(missing)
            + ". Ask the user for the single most important missing piece, then draft."
        )

    lines = [
        f"Ready to draft a {message_type} to {recipient}.",
        f"Purpose: {purpose}",
        f"Tone: {tone}",
    ]
    if background:
        lines.append(f"Background: {background}")
    lines.append(
        "Now write the actual draft in your reply — short and copy-pasteable, in the tone "
        "above — and ask the user to approve or tweak it. Do NOT claim to have sent or "
        "scheduled anything; Spotter only drafts."
    )
    return "\n".join(lines)
