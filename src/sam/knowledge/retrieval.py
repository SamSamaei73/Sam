"""``KnowledgeRetriever``: joins ``KnowledgeIndex`` search hits back
against ``KnowledgeStore`` to build fully source-attributed results.

Deliberately does no authorization of its own — the same layering as
``sam.coding.repository.RepositoryBackend``. ``KnowledgeEngine`` is the
only caller, and only after ``PermissionEngine.evaluate`` returns ALLOW.
"""

from __future__ import annotations

from sam.knowledge.errors import RetrievalError
from sam.knowledge.index import KnowledgeIndex
from sam.knowledge.models import RetrievalQuery, RetrievalResult
from sam.knowledge.store import KnowledgeStore


class KnowledgeRetriever:
    def __init__(self, *, store: KnowledgeStore, index: KnowledgeIndex) -> None:
        self._store = store
        self._index = index

    def retrieve(self, query: RetrievalQuery) -> tuple[RetrievalResult, ...]:
        matches = self._index.search(
            collection_id=query.collection_id,
            resource_id=query.resource_id,
            query=query.query,
            limit=query.top_k,
        )
        results: list[RetrievalResult] = []
        for match in matches:
            chunk = self._store.get_chunk(match.resource_id, match.chunk_id)
            if chunk is None:
                # Stale index entry (the resource was removed after the
                # index snapshot was read) — skip rather than fabricate
                # a result with no backing chunk.
                continue
            resource = self._store.get_resource(chunk.collection_id, chunk.resource_id)
            if resource is None:
                continue
            results.append(
                RetrievalResult(
                    chunk=chunk,
                    score=match.score,
                    resource=resource,
                    location=chunk.location,
                )
            )
        return tuple(results)


def retrieve_or_raise(
    retriever: KnowledgeRetriever, query: RetrievalQuery
) -> tuple[RetrievalResult, ...]:
    """A thin wrapper used by ``KnowledgeEngine`` so an unexpected
    retriever failure surfaces as a typed ``RetrievalError``, never a
    raw exception."""

    try:
        return retriever.retrieve(query)
    except Exception as exc:  # noqa: BLE001 - re-raised as a typed domain error
        raise RetrievalError("retrieval failed") from exc


__all__ = ["KnowledgeRetriever", "retrieve_or_raise"]
