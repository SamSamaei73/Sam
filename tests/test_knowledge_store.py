"""Tests for sam.knowledge.store.InMemoryKnowledgeStore."""

from __future__ import annotations

from datetime import UTC, datetime

from sam.knowledge.models import (
    ChunkLocation,
    DocumentChunk,
    DocumentMetadata,
    KnowledgeResource,
    ResourceSourceKind,
    ResourceType,
)
from sam.knowledge.store import InMemoryKnowledgeStore

NOW = datetime.now(UTC)


def _resource(
    resource_id: str, collection_id: str = "docs", checksum: str = "a" * 64
) -> KnowledgeResource:
    return KnowledgeResource(
        resource_id=resource_id,
        collection_id=collection_id,
        name=f"{resource_id}.txt",
        resource_type=ResourceType.TXT,
        source=f"{resource_id}.txt",
        source_kind=ResourceSourceKind.UPLOAD,
        size_bytes=10,
        checksum=checksum,
        metadata=DocumentMetadata(),
        chunk_count=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _chunk(resource_id: str, collection_id: str, idx: int) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=f"{resource_id}:{idx:06d}",
        resource_id=resource_id,
        collection_id=collection_id,
        sequence_index=idx,
        text="hello",
        location=ChunkLocation(),
        created_at=NOW,
    )


class TestResourceLifecycle:
    def test_save_and_get(self) -> None:
        store = InMemoryKnowledgeStore()
        resource = _resource("r1")
        store.save_resource(resource)
        assert store.get_resource("docs", "r1") == resource

    def test_get_missing_returns_none(self) -> None:
        store = InMemoryKnowledgeStore()
        assert store.get_resource("docs", "missing") is None

    def test_list_resources_scoped_to_collection(self) -> None:
        store = InMemoryKnowledgeStore()
        store.save_resource(_resource("r1", collection_id="docs"))
        store.save_resource(_resource("r2", collection_id="research"))
        assert [r.resource_id for r in store.list_resources("docs")] == ["r1"]
        assert [r.resource_id for r in store.list_resources("research")] == ["r2"]

    def test_list_resources_empty_collection(self) -> None:
        store = InMemoryKnowledgeStore()
        assert store.list_resources("nothing-here") == ()

    def test_delete_resource_removes_metadata_and_chunks(self) -> None:
        store = InMemoryKnowledgeStore()
        resource = _resource("r1")
        store.save_resource(resource)
        store.save_chunks("r1", (_chunk("r1", "docs", 0),))
        assert store.delete_resource("docs", "r1") is True
        assert store.get_resource("docs", "r1") is None
        assert store.get_chunks("r1") == ()

    def test_delete_missing_returns_false(self) -> None:
        store = InMemoryKnowledgeStore()
        assert store.delete_resource("docs", "missing") is False

    def test_delete_does_not_affect_other_collections(self) -> None:
        store = InMemoryKnowledgeStore()
        store.save_resource(_resource("r1", collection_id="docs"))
        store.save_resource(_resource("r1", collection_id="research"))
        store.delete_resource("docs", "r1")
        assert store.get_resource("docs", "r1") is None
        assert store.get_resource("research", "r1") is not None


class TestChunks:
    def test_save_and_get_chunks(self) -> None:
        store = InMemoryKnowledgeStore()
        chunks = (_chunk("r1", "docs", 0), _chunk("r1", "docs", 1))
        store.save_chunks("r1", chunks)
        assert store.get_chunks("r1") == chunks

    def test_get_chunk_by_id(self) -> None:
        store = InMemoryKnowledgeStore()
        chunk = _chunk("r1", "docs", 0)
        store.save_chunks("r1", (chunk,))
        assert store.get_chunk("r1", chunk.chunk_id) == chunk

    def test_get_chunk_missing_returns_none(self) -> None:
        store = InMemoryKnowledgeStore()
        assert store.get_chunk("r1", "nope") is None


class TestDuplicateDetection:
    def test_find_by_checksum_matches(self) -> None:
        store = InMemoryKnowledgeStore()
        resource = _resource("r1", checksum="deadbeef")
        store.save_resource(resource)
        assert store.find_by_checksum("docs", "deadbeef") == resource

    def test_find_by_checksum_no_match(self) -> None:
        store = InMemoryKnowledgeStore()
        store.save_resource(_resource("r1", checksum="deadbeef"))
        assert store.find_by_checksum("docs", "other") is None

    def test_find_by_checksum_scoped_to_collection(self) -> None:
        store = InMemoryKnowledgeStore()
        store.save_resource(
            _resource("r1", collection_id="research", checksum="cafebabe")
        )
        assert store.find_by_checksum("docs", "cafebabe") is None


class TestCollections:
    def test_ensure_collection_creates_once(self) -> None:
        store = InMemoryKnowledgeStore()
        first = store.ensure_collection("docs", now=NOW)
        second = store.ensure_collection("docs", now=NOW)
        assert first == second

    def test_get_collection_missing_returns_none(self) -> None:
        store = InMemoryKnowledgeStore()
        assert store.get_collection("nope") is None

    def test_list_collections(self) -> None:
        store = InMemoryKnowledgeStore()
        store.ensure_collection("docs", now=NOW)
        store.ensure_collection("research", now=NOW)
        ids = {c.collection_id for c in store.list_collections()}
        assert ids == {"docs", "research"}


class TestConsistencyAfterFailedDelete:
    def test_resource_still_readable_after_noop_delete(self) -> None:
        store = InMemoryKnowledgeStore()
        store.save_resource(_resource("r1"))
        store.delete_resource("docs", "not-r1")
        assert store.get_resource("docs", "r1") is not None
