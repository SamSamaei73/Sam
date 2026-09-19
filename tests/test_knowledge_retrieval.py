"""Tests for sam.knowledge.retrieval.KnowledgeRetriever."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from sam.knowledge.errors import RetrievalError
from sam.knowledge.index import InMemoryLexicalIndex
from sam.knowledge.models import (
    ChunkLocation,
    DocumentChunk,
    DocumentMetadata,
    KnowledgeResource,
    ResourceSourceKind,
    ResourceType,
    RetrievalQuery,
)
from sam.knowledge.retrieval import KnowledgeRetriever, retrieve_or_raise
from sam.knowledge.store import InMemoryKnowledgeStore

NOW = datetime.now(UTC)


def _resource(resource_id: str, collection_id: str = "docs") -> KnowledgeResource:
    return KnowledgeResource(
        resource_id=resource_id,
        collection_id=collection_id,
        name=f"{resource_id}.txt",
        resource_type=ResourceType.TXT,
        source=f"{resource_id}.txt",
        source_kind=ResourceSourceKind.UPLOAD,
        size_bytes=10,
        checksum="a" * 64,
        metadata=DocumentMetadata(),
        chunk_count=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _chunk(
    chunk_id: str, resource_id: str, collection_id: str, text: str
) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        resource_id=resource_id,
        collection_id=collection_id,
        sequence_index=0,
        text=text,
        location=ChunkLocation(page_number=3),
        created_at=NOW,
    )


class _RaisingIndex:
    def index_chunks(self, chunks: object) -> None:
        raise RuntimeError("boom")

    def remove_resource(self, resource_id: str) -> None:
        raise RuntimeError("boom")

    def search(self, **kwargs: object) -> tuple[Any, ...]:
        raise RuntimeError("boom")


class TestKnowledgeRetriever:
    def test_returns_full_source_attribution(self) -> None:
        store = InMemoryKnowledgeStore()
        index = InMemoryLexicalIndex()
        store.save_resource(_resource("r1"))
        chunk = _chunk("r1:000000", "r1", "docs", "graph neural networks")
        store.save_chunks("r1", (chunk,))
        index.index_chunks((chunk,))

        retriever = KnowledgeRetriever(store=store, index=index)
        results = retriever.retrieve(
            RetrievalQuery(query="graph networks", collection_id="docs")
        )
        assert len(results) == 1
        assert results[0].chunk == chunk
        assert results[0].resource.resource_id == "r1"
        assert results[0].location == chunk.location

    def test_multi_resource_retrieval_keeps_source_identity(self) -> None:
        store = InMemoryKnowledgeStore()
        index = InMemoryLexicalIndex()
        for i in (1, 2):
            rid = f"r{i}"
            store.save_resource(_resource(rid))
            chunk = _chunk(
                f"{rid}:000000", rid, "docs", "graph misinformation detection"
            )
            store.save_chunks(rid, (chunk,))
            index.index_chunks((chunk,))

        retriever = KnowledgeRetriever(store=store, index=index)
        results = retriever.retrieve(
            RetrievalQuery(query="graph misinformation", collection_id="docs")
        )
        resource_ids = {r.resource.resource_id for r in results}
        assert resource_ids == {"r1", "r2"}

    def test_stale_index_entry_skipped_not_fabricated(self) -> None:
        store = InMemoryKnowledgeStore()
        index = InMemoryLexicalIndex()
        store.save_resource(_resource("r1"))
        chunk = _chunk("r1:000000", "r1", "docs", "graph networks")
        store.save_chunks("r1", (chunk,))
        index.index_chunks((chunk,))
        # Simulate a resource removed from the store without updating the
        # index (the retriever must never fabricate a result for it).
        store.delete_resource("docs", "r1")

        retriever = KnowledgeRetriever(store=store, index=index)
        results = retriever.retrieve(
            RetrievalQuery(query="graph networks", collection_id="docs")
        )
        assert results == ()

    def test_no_match_returns_empty(self) -> None:
        store = InMemoryKnowledgeStore()
        index = InMemoryLexicalIndex()
        retriever = KnowledgeRetriever(store=store, index=index)
        results = retriever.retrieve(RetrievalQuery(query="nothing here"))
        assert results == ()


class TestRetrieveOrRaise:
    def test_wraps_unexpected_failure(self) -> None:
        retriever = KnowledgeRetriever(
            store=InMemoryKnowledgeStore(), index=_RaisingIndex()
        )
        with pytest.raises(RetrievalError):
            retrieve_or_raise(retriever, RetrievalQuery(query="x"))
