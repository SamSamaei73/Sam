"""Storage abstraction for resources, chunks, and collections.

``KnowledgeStore`` is a ``Protocol`` so a future ``PostgresKnowledgeStore``
(PostgreSQL + pgvector) can be substituted without changing
``KnowledgeEngine`` — see docs/knowledge.md's "Cloud-ready storage"
section. Only ``InMemoryKnowledgeStore`` is implemented in Phase 7; no
database, ORM, or network client is introduced here.

This module never imports ``sam.memory`` — the Knowledge Layer's
persistence is entirely independent of Memory's, by design (see the
Phase 7 task's "Do not import sam.memory into the Knowledge
implementation merely to persist documents" rule).
"""

from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Protocol

from sam.knowledge.models import DocumentChunk, KnowledgeCollection, KnowledgeResource


class KnowledgeStore(Protocol):
    """Persistence contract ``KnowledgeEngine`` depends on."""

    def save_resource(self, resource: KnowledgeResource) -> None: ...

    def get_resource(
        self, collection_id: str, resource_id: str
    ) -> KnowledgeResource | None: ...

    def find_by_checksum(
        self, collection_id: str, checksum: str
    ) -> KnowledgeResource | None:
        """Used for duplicate-content detection at ingestion time."""
        ...

    def delete_resource(self, collection_id: str, resource_id: str) -> bool: ...

    def list_resources(self, collection_id: str) -> tuple[KnowledgeResource, ...]: ...

    def save_chunks(
        self, resource_id: str, chunks: tuple[DocumentChunk, ...]
    ) -> None: ...

    def get_chunks(self, resource_id: str) -> tuple[DocumentChunk, ...]: ...

    def get_chunk(self, resource_id: str, chunk_id: str) -> DocumentChunk | None: ...

    def ensure_collection(
        self, collection_id: str, *, now: datetime
    ) -> KnowledgeCollection: ...

    def get_collection(self, collection_id: str) -> KnowledgeCollection | None: ...

    def list_collections(self) -> tuple[KnowledgeCollection, ...]: ...


class InMemoryKnowledgeStore:
    """A process-local, lock-protected reference implementation.

    Resources and their chunks are always written and removed together
    (see ``save_resource``/``delete_resource`` callers in
    ``sam.knowledge.engine``) so the store never holds chunks for a
    resource it does not also hold metadata for, or vice versa.
    """

    def __init__(self) -> None:
        self._resources: dict[str, dict[str, KnowledgeResource]] = {}
        self._chunks: dict[str, tuple[DocumentChunk, ...]] = {}
        self._collections: dict[str, KnowledgeCollection] = {}
        self._lock = RLock()

    def save_resource(self, resource: KnowledgeResource) -> None:
        with self._lock:
            bucket = self._resources.setdefault(resource.collection_id, {})
            bucket[resource.resource_id] = resource

    def get_resource(
        self, collection_id: str, resource_id: str
    ) -> KnowledgeResource | None:
        with self._lock:
            return self._resources.get(collection_id, {}).get(resource_id)

    def find_by_checksum(
        self, collection_id: str, checksum: str
    ) -> KnowledgeResource | None:
        with self._lock:
            for resource in self._resources.get(collection_id, {}).values():
                if resource.checksum == checksum:
                    return resource
            return None

    def delete_resource(self, collection_id: str, resource_id: str) -> bool:
        with self._lock:
            bucket = self._resources.get(collection_id, {})
            if resource_id not in bucket:
                return False
            del bucket[resource_id]
            self._chunks.pop(resource_id, None)
            return True

    def list_resources(self, collection_id: str) -> tuple[KnowledgeResource, ...]:
        with self._lock:
            return tuple(self._resources.get(collection_id, {}).values())

    def save_chunks(self, resource_id: str, chunks: tuple[DocumentChunk, ...]) -> None:
        with self._lock:
            self._chunks[resource_id] = chunks

    def get_chunks(self, resource_id: str) -> tuple[DocumentChunk, ...]:
        with self._lock:
            return self._chunks.get(resource_id, ())

    def get_chunk(self, resource_id: str, chunk_id: str) -> DocumentChunk | None:
        with self._lock:
            for chunk in self._chunks.get(resource_id, ()):
                if chunk.chunk_id == chunk_id:
                    return chunk
            return None

    def ensure_collection(
        self, collection_id: str, *, now: datetime
    ) -> KnowledgeCollection:
        with self._lock:
            existing = self._collections.get(collection_id)
            if existing is not None:
                return existing
            collection = KnowledgeCollection(
                collection_id=collection_id, name=collection_id, created_at=now
            )
            self._collections[collection_id] = collection
            return collection

    def get_collection(self, collection_id: str) -> KnowledgeCollection | None:
        with self._lock:
            return self._collections.get(collection_id)

    def list_collections(self) -> tuple[KnowledgeCollection, ...]:
        with self._lock:
            return tuple(self._collections.values())


__all__ = ["InMemoryKnowledgeStore", "KnowledgeStore"]
