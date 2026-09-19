"""Index abstraction: index chunks, remove a resource, search.

``KnowledgeIndex`` is a ``Protocol`` so a future vector index (backed by
pgvector or similar) can be substituted without changing
``KnowledgeEngine`` or ``KnowledgeRetriever`` — see
``sam.knowledge.embeddings`` for the (currently unused-by-default)
embedding-provider boundary this is designed to eventually sit behind.

``InMemoryLexicalIndex`` implements deterministic term-overlap scoring —
no vector database, no external service, no randomness. Tie-breaking is
always by ``(resource_id, sequence_index)`` ascending, so identical
inputs always produce identical result ordering — see
``docs/knowledge.md``'s "Determinism" section.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from threading import RLock
from typing import Protocol

from sam.knowledge.models import DocumentChunk

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TOKEN_RE.finditer(text)]


@dataclass(frozen=True)
class IndexMatch:
    """One scored hit — the index never returns full chunk/resource
    objects; ``sam.knowledge.retrieval`` joins these back against the
    store."""

    chunk_id: str
    resource_id: str
    score: float


class KnowledgeIndex(Protocol):
    """Persistence-and-search contract ``KnowledgeRetriever`` depends on."""

    def index_chunks(self, chunks: tuple[DocumentChunk, ...]) -> None: ...

    def remove_resource(self, resource_id: str) -> None: ...

    def search(
        self,
        *,
        collection_id: str | None,
        resource_id: str | None,
        query: str,
        limit: int,
    ) -> tuple[IndexMatch, ...]: ...


@dataclass(frozen=True)
class _Entry:
    resource_id: str
    collection_id: str
    sequence_index: int
    tokens: tuple[str, ...]


class InMemoryLexicalIndex:
    """A deterministic term-frequency lexical index.

    Score for a chunk is the sum, over each distinct query token, of how
    many times that token appears in the chunk — a simple, explainable,
    fully deterministic measure. This is explicitly not semantic
    retrieval; see ``sam.knowledge.embeddings`` for the architectural
    boundary reserved for a future real embedding-based index.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = RLock()

    def index_chunks(self, chunks: tuple[DocumentChunk, ...]) -> None:
        with self._lock:
            for chunk in chunks:
                self._entries[chunk.chunk_id] = _Entry(
                    resource_id=chunk.resource_id,
                    collection_id=chunk.collection_id,
                    sequence_index=chunk.sequence_index,
                    tokens=tuple(_tokenize(chunk.text)),
                )

    def remove_resource(self, resource_id: str) -> None:
        with self._lock:
            stale = [
                chunk_id
                for chunk_id, entry in self._entries.items()
                if entry.resource_id == resource_id
            ]
            for chunk_id in stale:
                del self._entries[chunk_id]

    def search(
        self,
        *,
        collection_id: str | None,
        resource_id: str | None,
        query: str,
        limit: int,
    ) -> tuple[IndexMatch, ...]:
        query_tokens = _tokenize(query)
        if not query_tokens:
            return ()
        query_set = set(query_tokens)

        with self._lock:
            candidates = list(self._entries.items())

        scored: list[IndexMatch] = []
        for chunk_id, entry in candidates:
            if collection_id is not None and entry.collection_id != collection_id:
                continue
            if resource_id is not None and entry.resource_id != resource_id:
                continue
            counts = Counter(entry.tokens)
            score = sum(counts[token] for token in query_set)
            if score > 0:
                scored.append(
                    IndexMatch(
                        chunk_id=chunk_id, resource_id=entry.resource_id, score=score
                    )
                )

        scored.sort(
            key=lambda match: (
                -match.score,
                self._entries[match.chunk_id].resource_id,
                self._entries[match.chunk_id].sequence_index,
            )
        )
        return tuple(scored[:limit])


__all__ = ["IndexMatch", "InMemoryLexicalIndex", "KnowledgeIndex"]
