"""Spotter's reasoning core: turn an incoming message into a reply via Claude.

Step 5 (Wave 1) scope: the brain now runs a tool-use loop. It passes the tool
definitions to the Messages API, and whenever Claude responds with
``stop_reason == "tool_use"`` it dispatches each tool to its handler, appends the
results, and re-calls Claude — until Claude finishes (``end_turn``) or a hard
iteration cap is hit. The full turn (user message + final reply, with token
totals and a JSON record of any tool calls) is logged to ``conversation_log``.
Any API failure is caught and turned into a short error reply.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import anthropic
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import Config
from .db.models import ConversationLogEntry, WorkspaceFact
from .tools import TOOL_HANDLERS, ToolContext

logger = logging.getLogger(__name__)

# How many prior conversation_log rows to replay as context, oldest first.
_HISTORY_LIMIT = 20
# Cap on a single reply. Spotter answers in 1-3 sentences, so this is generous.
_MAX_TOKENS = 1024
# Hard ceiling on tool-use round trips per turn, so a misbehaving loop can't run
# forever.
_MAX_TOOL_ITERATIONS = 10
# conversation_log roles we know how to map into the Messages API. 'tool_result'
# exists in the schema but isn't logged as a standalone history turn, so it's
# filtered out of replay.
_API_ROLES = frozenset({"user", "assistant"})

_FALLBACK_REPLY = (
    "Something went wrong reaching my brain just now. Try again in a moment."
)
_CAP_REPLY = (
    "I got stuck working through that — too many steps. Try rephrasing or "
    "breaking it into smaller pieces."
)


class Brain:
    """Reasoning core wired to Claude, the tool registry, and the database."""

    def __init__(
        self,
        config: Config,
        client: anthropic.Anthropic,
        session_factory: sessionmaker[Session],
    ) -> None:
        self._config = config
        self._client = client
        self._session_factory = session_factory

    def respond(self, user_text: str) -> str:
        """Produce Spotter's reply to ``user_text``, running tools as needed."""
        with self._session_factory() as session:
            system_prompt = self._build_system_prompt(session)
            history = self._load_history(session)

        messages: list[dict[str, Any]] = history + [
            {"role": "user", "content": user_text}
        ]
        tokens_in = 0
        tokens_out = 0
        tool_calls: list[dict[str, Any]] = []

        try:
            for _ in range(_MAX_TOOL_ITERATIONS):
                response = self._client.messages.create(
                    model=self._config.default_model,
                    max_tokens=_MAX_TOKENS,
                    system=system_prompt,
                    messages=messages,
                    tools=self._config.tools,
                )
                tokens_in += response.usage.input_tokens
                tokens_out += response.usage.output_tokens

                if response.stop_reason != "tool_use":
                    reply = _extract_text(response)
                    self._log_turn(user_text, reply, tokens_in, tokens_out, tool_calls)
                    return reply

                # Claude wants tools: dispatch each, collect results, loop.
                messages.append({"role": "assistant", "content": response.content})
                messages.append(
                    {"role": "user", "content": self._run_tools(response, tool_calls)}
                )
        except anthropic.AnthropicError:
            # Covers API status errors, connection errors, and timeouts.
            logger.exception("Anthropic API call failed")
            return _FALLBACK_REPLY

        # Fell out of the loop without an end_turn: the iteration cap tripped.
        logger.warning(
            "Tool loop hit the %d-iteration cap; aborting turn", _MAX_TOOL_ITERATIONS
        )
        self._log_turn(user_text, _CAP_REPLY, tokens_in, tokens_out, tool_calls)
        return _CAP_REPLY

    # -- tool dispatch -------------------------------------------------------

    def _run_tools(
        self, response: anthropic.types.Message, tool_calls: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Execute every tool_use block in ``response``; return tool_result blocks."""
        results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result_text, is_error = self._dispatch(block.name, block.input)
            tool_calls.append(
                {"name": block.name, "input": block.input, "result": result_text}
            )
            result: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text,
            }
            if is_error:
                result["is_error"] = True
            results.append(result)
        return results

    def _dispatch(self, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Run one tool handler in its own transaction. Returns (text, is_error)."""
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            # Either an unknown tool or one of the not-yet-built Wave 2+ tools.
            return f"Tool '{name}' is not available yet.", True
        try:
            with self._session_factory() as session, session.begin():
                return handler(ToolContext(session=session, config=self._config), tool_input), False
        except Exception:
            logger.exception("Tool handler %s failed", name)
            return f"Error running {name}.", True

    # -- prompt + history ----------------------------------------------------

    def _build_system_prompt(self, session: Session) -> str:
        """Load the system prompt template and hydrate workspace facts into it."""
        template = self._config.prompts.get("system_prompt", "")
        # Only core facts go in the system prompt — non-core facts stay reachable
        # on demand via query_memory scope=facts (FTS5, unfiltered).
        facts = session.scalars(
            select(WorkspaceFact)
            .where(WorkspaceFact.is_core == 1)
            .order_by(WorkspaceFact.category, WorkspaceFact.id)
        ).all()
        # str.replace (not str.format) so literal braces in the prompt are safe.
        return template.replace("{workspace_facts}", _format_facts(facts))

    def _load_history(self, session: Session) -> list[dict[str, str]]:
        """Return up to the last ``_HISTORY_LIMIT`` turns, oldest first."""
        rows = session.scalars(
            select(ConversationLogEntry)
            .order_by(ConversationLogEntry.id.desc())
            .limit(_HISTORY_LIMIT)
        ).all()
        rows = list(reversed(rows))  # newest-first query -> oldest-first for the API
        return [
            {"role": row.role, "content": row.content}
            for row in rows
            if row.role in _API_ROLES
        ]

    def _log_turn(
        self,
        user_text: str,
        reply: str,
        tokens_in: int,
        tokens_out: int,
        tool_calls: list[dict[str, Any]],
    ) -> None:
        """Persist the user message and assistant reply as two log rows.

        Input tokens land on the user row, output tokens on the assistant row,
        and any tool calls made this turn are serialized to the assistant row's
        ``tool_calls`` column. ``cost_cents`` stays null for now.
        """
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None
        with self._session_factory() as session, session.begin():
            session.add(
                ConversationLogEntry(role="user", content=user_text, tokens_in=tokens_in)
            )
            session.add(
                ConversationLogEntry(
                    role="assistant",
                    content=reply,
                    tokens_out=tokens_out,
                    tool_calls=tool_calls_json,
                )
            )


def _format_facts(facts: Sequence[WorkspaceFact]) -> str:
    if not facts:
        return "(No workspace facts recorded yet.)"
    return "\n".join(f"- ({fact.category}) {fact.content}" for fact in facts)


def _extract_text(response: anthropic.types.Message) -> str:
    """Concatenate the text blocks of a Messages API response."""
    parts = [block.text for block in response.content if block.type == "text"]
    text = "\n".join(parts).strip()
    return text or _FALLBACK_REPLY
