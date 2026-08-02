"""Retrieval eval harness: measure the ranking instead of vibing it.

``python -m src.evals`` builds a throwaway fixture database (realistic events
with controlled ages, sources, and confidences), runs a fixed query set with
known right answers through the SAME Retriever production uses, and reports
hit@1, hit@3, and MRR per query. Change the hybrid weights or the half-life
in :mod:`src.retrieval`, re-run, and see whether retrieval actually got
better.

With ``VOYAGE_API_KEY`` set the run uses real embeddings (a quality
measurement, ~20 embedding calls). Without it, a deterministic hash embedder
runs the same pipeline — labeled clearly as a plumbing check, not a quality
score.
"""

from __future__ import annotations

import sys
import tempfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from .db.database import apply_schema, create_db_engine, make_session_factory
from .db.models import Event, Project
from .retrieval import Retriever, get_embedder

_UTC_FMT = "%Y-%m-%d %H:%M:%S"

# (external_id, project, kind, source, confidence, days_ago, summary)
_FIXTURES = [
    ("fx-1", "Simmer", "push", "github", 1.0, 1,
     "3 commits pushed to mealprep@main: swap Stripe live keys and verify charge flow"),
    ("fx-2", "Simmer", "capture", "user_chat", 0.8, 20,
     "Captured (thought): Stripe keys still in test mode, must swap before launch"),
    ("fx-3", "Simmer", "session_note", "claude_code", 0.9, 2,
     "Claude Code session on Simmer: fixed cuisine filter regression, merged to main"),
    ("fx-4", "Simmer", "push", "github", 1.0, 15,
     "5 commits pushed to mealprep@main: recipe search indexing rewrite"),
    ("fx-5", "Spotter", "push", "github", 1.0, 1,
     "4 commits pushed to spotter@main: semantic retrieval with hybrid re-ranking"),
    ("fx-6", "Spotter", "session_note", "claude_code", 0.9, 3,
     "Claude Code session on Spotter: GitHub webhook ingestion into the event log"),
    ("fx-7", "Simmer", "capture", "user_chat", 0.8, 4,
     "Captured (followup): AIsa recruiter wants the GitHub link by next week"),
    ("fx-8", "Simmer", "pull_request", "github", 1.0, 6,
     "PR #12 merged in mealprep: household onboarding flow"),
    ("fx-9", "Spotter", "capture", "user_dashboard", 0.8, 8,
     "Captured (idea): weekly digest of everything that shipped"),
    ("fx-10", "Simmer", "session_note", "claude_code", 0.9, 30,
     "Claude Code session on Simmer: early payment integration spike, abandoned"),
]

# (query, expected external_id) — the answer a good ranking puts on top.
_QUERIES = [
    ("what's the situation with payment credentials", "fx-1"),
    ("did the cuisine bug get fixed", "fx-3"),
    ("what does the recruiter need from me", "fx-7"),
    ("what shipped on spotter recently", "fx-5"),
    ("how does spotter learn about my commits", "fx-6"),
    ("what happened with recipe search", "fx-4"),
    ("simmer onboarding progress", "fx-8"),
    ("ideas about summarizing the week", "fx-9"),
]


class _HashEmbedder:
    """Deterministic offline stand-in: pipeline check, not quality measurement."""

    model = "hash-64"

    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        out = []
        for text in texts:
            v = [0.0] * 64
            for token in text.lower().split():
                v[zlib.crc32(token.encode()) % 64] += 1.0
            out.append(v)
        return out


def run_evals() -> dict[str, float]:
    """Build fixtures, rank every query, print the report, return the metrics."""
    from .config import load_config

    embedder = None
    mode = "FAKE-EMBEDDER (plumbing check only — set VOYAGE_API_KEY for real scores)"
    try:
        embedder = get_embedder(load_config())
    except Exception:
        pass  # missing unrelated env (e.g. bot token) shouldn't block evals
    if embedder is not None:
        mode = f"REAL embeddings ({embedder.model})"
    else:
        embedder = _HashEmbedder()

    db_path = Path(tempfile.mkdtemp()) / "evals.db"
    engine = create_db_engine(db_path)
    apply_schema(engine)
    factory = make_session_factory(engine)
    now = datetime.now(timezone.utc)
    with factory() as session, session.begin():
        session.add(Project(name="Simmer", priority=10))
        session.add(Project(name="Spotter", priority=9))
        session.flush()
        ids = {"Simmer": 1, "Spotter": 2}
        for ext, project, kind, source, confidence, days, summary in _FIXTURES:
            session.add(
                Event(
                    source=source,
                    kind=kind,
                    project_id=ids[project],
                    summary=summary,
                    confidence=confidence,
                    occurred_at=(now - timedelta(days=days)).strftime(_UTC_FMT),
                    external_id=ext,
                )
            )

    hits1 = hits3 = 0
    reciprocal_ranks = []
    print(f"Retrieval evals — {mode}\n")
    with factory() as session, session.begin():
        retriever = Retriever(embedder)
        retriever.ensure_indexed(session)
        events = list(session.scalars(select(Event)))
        # One batched call for all query embeddings: free-tier rate limits
        # (429s) punish per-query calls.
        query_vectors = embedder.embed([q for q, _ in _QUERIES], "query")
        for (query, expected), query_vector in zip(_QUERIES, query_vectors):
            ranked = retriever.rank(session, query, events, query_vector)
            position = next(
                (i for i, r in enumerate(ranked) if r.event.external_id == expected),
                None,
            )
            rank_label = "MISS" if position is None else f"#{position + 1}"
            marker = "PASS" if position == 0 else ("ok  " if position is not None and position < 3 else "FAIL")
            top = ranked[0]
            print(
                f"[{marker}] {rank_label:>4}  {query!r}\n"
                f"        top: {top.event.summary[:72]} "
                f"(score {top.score:.2f}, sem {top.semantic:.2f})"
            )
            if position == 0:
                hits1 += 1
            if position is not None and position < 3:
                hits3 += 1
            reciprocal_ranks.append(0.0 if position is None else 1.0 / (position + 1))

    n = len(_QUERIES)
    metrics = {
        "hit@1": hits1 / n,
        "hit@3": hits3 / n,
        "mrr": sum(reciprocal_ranks) / n,
    }
    print(
        f"\nhit@1 {metrics['hit@1']:.2f} · hit@3 {metrics['hit@3']:.2f} · "
        f"MRR {metrics['mrr']:.2f}  ({n} queries)"
    )
    return metrics


def main() -> None:
    run_evals()


if __name__ == "__main__":
    sys.exit(main())
