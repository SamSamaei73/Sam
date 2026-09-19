"""Deterministic retrieval ranking.

**Phase 4 implements deterministic retrieval only.** There is no
embedding model, no vector similarity, and no semantic search here —
"relevance" is a simple, explainable combination of exact/token text
match, recency, and importance. A future phase may add real
embedding/vector retrieval; when it does, it should be an additional
signal or an alternative ranking strategy behind the same
``RetrievalQuery`` → ``RetrievalResult`` contract, not a change to this
module's basic guarantee: given the same memories, the same query, and
the same ``now``, ``rank`` always returns the same order.

Weights (see ``_score``) are a plain, documented, hand-picked formula —
not learned, not tuned against real usage. They exist to make ordering
predictable and testable, not to model relevance well.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sam.memory.models import (
    Memory,
    RetrievalQuery,
    RetrievalResult,
    RetrievedMemory,
    importance_weight,
    normalize_for_comparison,
)

_RECENCY_WINDOW_SECONDS = 30 * 24 * 60 * 60  # 30 days

_EXACT_MATCH_WEIGHT = 0.5
_TOKEN_OVERLAP_WEIGHT = 0.3
_RECENCY_WEIGHT = 0.15
_IMPORTANCE_WEIGHT = 0.05

_NO_TEXT_RECENCY_WEIGHT = 0.7
_NO_TEXT_IMPORTANCE_WEIGHT = 0.3


def _token_overlap(content: str, query_text: str) -> float:
    query_tokens = set(normalize_for_comparison(query_text).split())
    if not query_tokens:
        return 0.0
    content_tokens = set(normalize_for_comparison(content).split())
    return len(query_tokens & content_tokens) / len(query_tokens)


def _recency_score(memory: Memory, *, now: datetime) -> float:
    age_seconds = max(0.0, (now - memory.created_at).total_seconds())
    return max(0.0, 1.0 - (age_seconds / _RECENCY_WINDOW_SECONDS))


def _score(memory: Memory, query: RetrievalQuery, *, now: datetime) -> float:
    recency = _recency_score(memory, now=now)
    importance = importance_weight(memory.importance)

    if not query.text:
        return min(
            1.0,
            _NO_TEXT_RECENCY_WEIGHT * recency + _NO_TEXT_IMPORTANCE_WEIGHT * importance,
        )

    exact = (
        1.0
        if memory.normalized_content() == normalize_for_comparison(query.text)
        else 0.0
    )
    overlap = _token_overlap(memory.content, query.text)
    return min(
        1.0,
        _EXACT_MATCH_WEIGHT * exact
        + _TOKEN_OVERLAP_WEIGHT * overlap
        + _RECENCY_WEIGHT * recency
        + _IMPORTANCE_WEIGHT * importance,
    )


def rank(
    candidates: Sequence[Memory], query: RetrievalQuery, *, now: datetime
) -> RetrievalResult:
    """Filter (usability, tags), score, and deterministically order.

    Filtering by principal/type/project is the store's job (see
    ``MemoryStore.list_candidates``); this function only applies signals
    that depend on the query's text/tags and the current time, so it
    never needs storage-implementation knowledge.
    """

    query_tags = set(query.tags)
    eligible = [
        memory
        for memory in candidates
        if memory.is_usable(now=now)
        and (not query_tags or query_tags & set(memory.tags))
    ]
    scored = [
        RetrievedMemory(memory=memory, score=_score(memory, query, now=now))
        for memory in eligible
    ]
    ordered = sorted(
        scored,
        key=lambda item: (
            -item.score,
            -item.memory.created_at.timestamp(),
            item.memory.memory_id,
        ),
    )
    return RetrievalResult(items=tuple(ordered[: query.limit]), query=query)
