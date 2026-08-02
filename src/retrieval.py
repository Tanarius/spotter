"""Semantic retrieval with hybrid re-ranking over the event log (phase 4D).

The Glyph technique applied to Spotter's memory: vector similarity fused with
metadata signals into one explainable score —

    score = 0.50 * semantic + 0.25 * recency + 0.15 * confidence + 0.10 * subject

Embeddings come from Voyage AI (``VOYAGE_API_KEY``); everything degrades
gracefully — no key or a failed call means callers fall back to keyword
scoring, never an error surfaced to the user. Vectors are stored in SQLite as
packed float32 (see ``EmbeddingRow``); indexing is lazy — ``ensure_indexed``
embeds whatever isn't embedded yet in one batch at query time.
"""

from __future__ import annotations

import hashlib
import logging
import math
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import EmbeddingRow, Event, Project

logger = logging.getLogger(__name__)

_UTC_FMT = "%Y-%m-%d %H:%M:%S"
_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
_DEFAULT_MODEL = "voyage-3.5-lite"
_EMBED_TEXT_MAX_CHARS = 2000
_BATCH_LIMIT = 128
_REQUEST_TIMEOUT = 20
# Hybrid weights — semantic leads, metadata disciplines it.
_W_SEMANTIC = 0.50
_W_RECENCY = 0.25
_W_CONFIDENCE = 0.15
_W_SUBJECT = 0.10
_RECENCY_HALF_LIFE_DAYS = 14.0


class RetrievalUnavailable(RuntimeError):
    """Semantic retrieval can't run (no key, API failure); use the fallback."""


class Embedder(Protocol):
    def embed(self, texts: list[str], input_type: str) -> list[list[float]]: ...


class VoyageEmbedder:
    """Thin Voyage AI client. Raises RetrievalUnavailable on any failure."""

    def __init__(self, api_key: str, model: str = _DEFAULT_MODEL) -> None:
        self._api_key = api_key
        self.model = model

    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        try:
            response = requests.post(
                _VOYAGE_URL,
                json={"input": texts, "model": self.model, "input_type": input_type},
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()["data"]
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise RetrievalUnavailable(f"Voyage embedding call failed: {exc}") from exc
        return [item["embedding"] for item in data]


def get_embedder(config: Any) -> VoyageEmbedder | None:
    """Embedder from config, or None when semantic retrieval isn't configured."""
    api_key = getattr(config, "voyage_api_key", "") or ""
    if not api_key:
        return None
    model = getattr(config, "embed_model", "") or _DEFAULT_MODEL
    return VoyageEmbedder(api_key, model)


@dataclass(frozen=True)
class RankedEvent:
    """One scored result with its per-signal breakdown (explainable ranking)."""

    event: Event
    score: float
    semantic: float
    recency: float
    confidence: float
    subject: float


class Retriever:
    """Lazy-indexing hybrid search over events."""

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

    # -- indexing --------------------------------------------------------------

    def ensure_indexed(self, session: Session) -> int:
        """Embed events that have no (or stale) embedding. Returns how many."""
        model = getattr(self._embedder, "model", _DEFAULT_MODEL)
        stored = {
            row.ref_id: row
            for row in session.scalars(
                select(EmbeddingRow).where(EmbeddingRow.kind == "event")
            )
        }
        pending: list[tuple[Event, str, str]] = []
        for event in session.scalars(select(Event)):
            text = _event_text(event)
            content_hash = _hash(text, model)
            existing = stored.get(event.id)
            if existing is not None and existing.content_hash == content_hash:
                continue
            pending.append((event, text, content_hash))
            if len(pending) >= _BATCH_LIMIT:
                break
        if not pending:
            return 0

        vectors = self._embedder.embed([t for _, t, _ in pending], "document")
        for (event, _, content_hash), vector in zip(pending, vectors):
            existing = stored.get(event.id)
            packed = _pack(vector)
            if existing is not None:
                existing.content_hash = content_hash
                existing.model = model
                existing.dim = len(vector)
                existing.vector = packed
            else:
                session.add(
                    EmbeddingRow(
                        kind="event",
                        ref_id=event.id,
                        content_hash=content_hash,
                        model=model,
                        dim=len(vector),
                        vector=packed,
                    )
                )
        logger.info("Embedded %d event(s) for semantic retrieval", len(pending))
        return len(pending)

    # -- search ----------------------------------------------------------------

    def rank(
        self, session: Session, query: str, events: list[Event]
    ) -> list[RankedEvent]:
        """Hybrid-score ``events`` against ``query``, best first."""
        query_vector = self._embedder.embed([query], "query")[0]
        vectors = {
            row.ref_id: _unpack(row.vector)
            for row in session.scalars(
                select(EmbeddingRow).where(
                    EmbeddingRow.kind == "event",
                    EmbeddingRow.ref_id.in_([e.id for e in events]),
                )
            )
        }
        # Subject signal: does the query name the event's project?
        query_lower = query.lower()
        project_names = {
            p.id: p.name.lower() for p in session.scalars(select(Project))
        }
        now = datetime.now(timezone.utc)
        ranked = []
        for event in events:
            vector = vectors.get(event.id)
            semantic = _cosine(query_vector, vector) if vector else 0.0
            recency = 0.5 ** (_age_days(event, now) / _RECENCY_HALF_LIFE_DAYS)
            confidence = event.confidence if event.confidence is not None else 0.5
            name = project_names.get(event.project_id or -1, "")
            subject = 1.0 if name and name in query_lower else 0.0
            score = (
                _W_SEMANTIC * semantic
                + _W_RECENCY * recency
                + _W_CONFIDENCE * confidence
                + _W_SUBJECT * subject
            )
            ranked.append(
                RankedEvent(event, score, semantic, recency, confidence, subject)
            )
        ranked.sort(key=lambda r: -r.score)
        return ranked


# -- helpers -------------------------------------------------------------------

def _event_text(event: Event) -> str:
    return f"{event.summary}\n{event.detail or ''}"[:_EMBED_TEXT_MAX_CHARS]


def _hash(text: str, model: str) -> str:
    return hashlib.sha256(f"{model}\x00{text}".encode()).hexdigest()


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    if norm == 0:
        return 0.0
    # Cosine can be negative; clamp to [0, 1] so the fused score stays sane.
    return max(0.0, dot / norm)


def _age_days(event: Event, now: datetime) -> float:
    try:
        occurred = datetime.strptime(event.occurred_at, _UTC_FMT).replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return 365.0
    return max(0.0, (now - occurred).total_seconds() / 86400)
