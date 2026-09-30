"""Durable Knowledge store and deterministic index rebuild (Phase 17
remediation).

The Phase 7 ``InMemoryKnowledgeStore``, write-through mirrored to its own
``knowledge_documents`` table: collections, resource metadata (name, type,
source label and kind, size, checksum, document metadata, timestamps: the
provenance Knowledge already records) and the chunks Knowledge policy already
accepted. Original files are never re-read on startup and no document content
is ever executed: a restart restores exactly what was ingested.

The lexical index is NOT persisted: it is rebuilt deterministically from the
persisted chunks (``rebuild_index``), so retrieval identity and ranking are
identical before and after a restart.

Defence in depth: chunk text or resource metadata that looks like a secret
is refused before any byte is written, and refused again on load (fail
closed). A corrupt or invalid persisted record fails validation and stops
startup instead of being silently skipped. Knowledge never writes Memory.
"""

from __future__ import annotations

import json
from typing import Any

from sam.knowledge.errors import SecretDetectedError
from sam.knowledge.index import KnowledgeIndex
from sam.knowledge.metadata import is_restricted_content
from sam.knowledge.models import DocumentChunk, KnowledgeCollection, KnowledgeResource
from sam.knowledge.store import InMemoryKnowledgeStore
from sam.storage.database import Database
from sam.storage.documents import DocumentMirror, State, WriteThrough, durable

_SEP = "\x1f"


def _check_resource(resource: KnowledgeResource) -> None:
    if is_restricted_content(resource.name) or is_restricted_content(resource.source):
        raise SecretDetectedError("secret-like resource metadata is never persisted")


def _check_chunks(chunks: tuple[DocumentChunk, ...]) -> None:
    if any(is_restricted_content(chunk.text) for chunk in chunks):
        raise SecretDetectedError("secret-like content is never persisted")


class SQLiteKnowledgeStore(WriteThrough, InMemoryKnowledgeStore):
    def __init__(self, db: Database) -> None:
        super().__init__()
        mirror = DocumentMirror(db, "knowledge")
        self._load(mirror.load())
        self._start_mirror(mirror)

    def _serialize(self) -> State:
        # A resource reaches disk only together with its complete chunk set
        # (the engine saves them in two calls): ingestion is atomic on disk.
        resources: dict[str, str] = {}
        chunks: dict[str, str] = {}
        for collection_id, bucket in self._resources.items():
            for resource_id, resource in bucket.items():
                items = self._chunks.get(resource_id, ())
                if len(items) != resource.chunk_count:
                    continue
                _check_resource(resource)
                _check_chunks(items)
                resources[f"{collection_id}{_SEP}{resource_id}"] = (
                    resource.model_dump_json()
                )
                chunks[resource_id] = (
                    "[" + ",".join(c.model_dump_json() for c in items) + "]"
                )
        return {
            "collection": {
                k: v.model_dump_json() for k, v in self._collections.items()
            },
            "resource": resources,
            "chunks": chunks,
        }

    def _snapshot(self) -> Any:
        return (
            {k: dict(v) for k, v in self._resources.items()},
            dict(self._chunks),
            dict(self._collections),
        )

    def _restore(self, snapshot: Any) -> None:
        resources, chunks, collections = snapshot
        self._resources = {k: dict(v) for k, v in resources.items()}
        self._chunks = dict(chunks)
        self._collections = dict(collections)

    def _load(self, state: State) -> None:
        self._collections = {
            k: KnowledgeCollection.model_validate_json(v)
            for k, v in state.get("collection", {}).items()
        }
        resources: dict[str, dict[str, KnowledgeResource]] = {}
        for key, body in state.get("resource", {}).items():
            resource = KnowledgeResource.model_validate_json(body)
            collection_id, resource_id = key.split(_SEP, 1)
            if (resource.collection_id, resource.resource_id) != (
                collection_id,
                resource_id,
            ):
                raise ValueError("persisted knowledge resource key mismatch")
            _check_resource(resource)
            resources.setdefault(collection_id, {})[resource_id] = resource
        chunks: dict[str, tuple[DocumentChunk, ...]] = {}
        known = {r for bucket in resources.values() for r in bucket}
        for resource_id, body in state.get("chunks", {}).items():
            items = tuple(DocumentChunk.model_validate(c) for c in json.loads(body))
            if resource_id not in known or any(
                c.resource_id != resource_id for c in items
            ):
                raise ValueError("persisted knowledge chunks without their resource")
            _check_chunks(items)
            chunks[resource_id] = items
        for bucket in resources.values():
            for resource_id, resource in bucket.items():
                if len(chunks.get(resource_id, ())) != resource.chunk_count:
                    raise ValueError("persisted knowledge resource is incomplete")
        self._resources = resources
        self._chunks = chunks

    def all_chunks(self) -> tuple[DocumentChunk, ...]:
        """Every persisted chunk in a stable order (for index rebuild)."""

        with self._lock:
            return tuple(
                chunk
                for resource_id in sorted(self._chunks)
                for chunk in sorted(
                    self._chunks[resource_id], key=lambda c: c.sequence_index
                )
            )

    save_resource = durable(InMemoryKnowledgeStore.save_resource)
    delete_resource = durable(InMemoryKnowledgeStore.delete_resource)
    save_chunks = durable(InMemoryKnowledgeStore.save_chunks)
    ensure_collection = durable(InMemoryKnowledgeStore.ensure_collection)


def rebuild_index(store: SQLiteKnowledgeStore, index: KnowledgeIndex) -> int:
    """Deterministically rebuild the lexical index from persisted chunks."""

    chunks = store.all_chunks()
    if chunks:
        index.index_chunks(chunks)
    return len(chunks)


MUTATORS = ("save_resource", "delete_resource", "save_chunks", "ensure_collection")

__all__ = ["MUTATORS", "SQLiteKnowledgeStore", "rebuild_index"]
