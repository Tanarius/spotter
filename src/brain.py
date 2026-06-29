"""Spotter's reasoning core: turn an incoming message into a reply via Claude.

Step 4 scope: a single conversational turn, no tools yet (those arrive at Step
5). For each turn the brain builds the system prompt (hydrating the
``{workspace_facts}`` placeholder from the database), replays recent
conversation history, calls the Anthropic Messages API, logs both sides of the
turn to ``conversation_log``, and returns the assistant's reply text. Any API
failure is caught and turned into a short error reply so a bad turn never
crashes the bot.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import anthropic
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import Config
from .db.models import ConversationLogEntry, WorkspaceFact

logger = logging.getLogger(__name__)

# How many prior conversation_log rows to replay as context, oldest first.
_HISTORY_LIMIT = 20
# Cap on a single reply. Spotter answers in 1-3 sentences, so this is generous.
_MAX_TOKENS = 1024
# conversation_log roles we know how to map into the Messages API. 'tool_result'
# exists in the schema but isn't produced until Step 5, so it's filtered out.
_API_ROLES = frozenset({"user", "assistant"})

_FALLBACK_REPLY = (
    "Something went wrong reaching my brain just now. Try again in a moment."
)


class Brain:
    """Reasoning core wired to Claude and the Spotter database."""

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
        """Produce Spotter's reply to ``user_text``.

        Reads workspace facts + recent history, calls the API, and on success
        logs both the user message and the reply. On any API failure, returns a
        short error string instead of raising.
        """
        with self._session_factory() as session:
            system_prompt = self._build_system_prompt(session)
            history = self._load_history(session)

        messages = history + [{"role": "user", "content": user_text}]

        try:
            response = self._client.messages.create(
                model=self._config.default_model,
                max_tokens=_MAX_TOKENS,
                system=system_prompt,
                messages=messages,
            )
        except anthropic.AnthropicError:
            # Covers API status errors, connection errors, and timeouts.
            logger.exception("Anthropic API call failed")
            return _FALLBACK_REPLY

        reply = _extract_text(response)
        self._log_turn(
            user_text=user_text,
            reply=reply,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
        )
        return reply

    # -- internals -----------------------------------------------------------

    def _build_system_prompt(self, session: Session) -> str:
        """Load the system prompt template and hydrate workspace facts into it."""
        template = self._config.prompts.get("system_prompt", "")
        facts = session.scalars(
            select(WorkspaceFact).order_by(WorkspaceFact.category, WorkspaceFact.id)
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
        self, *, user_text: str, reply: str, tokens_in: int, tokens_out: int
    ) -> None:
        """Persist the user message and assistant reply as two log rows.

        Input tokens land on the user row, output tokens on the assistant row;
        ``cost_cents`` stays null for now.
        """
        with self._session_factory() as session, session.begin():
            session.add(
                ConversationLogEntry(role="user", content=user_text, tokens_in=tokens_in)
            )
            session.add(
                ConversationLogEntry(role="assistant", content=reply, tokens_out=tokens_out)
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
