"""Durable long-term Memory store (Phase 17 remediation).

The Phase 4 ``InMemoryMemoryStore`` (ownership enforced by the store, real
deletion), write-through mirrored to its own ``memory_documents`` table. It
persists exactly the records ``MemoryEngine`` already decided to store: the
policy (secret rejection, scoping, duplicate handling) is unchanged and runs
before anything reaches the store.

Defence in depth: a record whose content, tags or metadata look like a
secret is refused HERE as well, before any byte is written, so a policy
regression can never put a secret into SQLite. Memory never reads or writes
Professional, Knowledge or Career data; those domains have their own tables.

Short-term working memory (``WorkingMemoryStore``) stays intentionally
ephemeral: it is session state with expiry.
"""

from __future__ import annotations

from typing import Any

from sam.memory.errors import MemoryStoreError
from sam.memory.models import Memory
from sam.memory.sanitization import looks_like_secret
from sam.memory.store import InMemoryMemoryStore
from sam.storage.database import Database
from sam.storage.documents import DocumentMirror, State, WriteThrough, durable


def _secret_free(memory: Memory) -> bool:
    texts = (memory.content, *memory.tags, *memory.metadata.values())
    return not any(looks_like_secret(text) for text in texts)


class SQLiteMemoryStore(WriteThrough, InMemoryMemoryStore):
    def __init__(self, db: Database) -> None:
        super().__init__()
        mirror = DocumentMirror(db, "memory")
        self._load(mirror.load())
        self._start_mirror(mirror)

    def _serialize(self) -> State:
        for memory in self._memories.values():
            if not _secret_free(memory):
                raise MemoryStoreError("secret-like content is never persisted")
        return {"memory": {k: v.model_dump_json() for k, v in self._memories.items()}}

    def _snapshot(self) -> Any:
        return dict(self._memories)

    def _restore(self, snapshot: Any) -> None:
        self._memories = dict(snapshot)

    def _load(self, state: State) -> None:
        memories = {
            k: Memory.model_validate_json(v) for k, v in state.get("memory", {}).items()
        }
        if not all(_secret_free(m) for m in memories.values()):
            raise MemoryStoreError("persisted memory failed its secret check")
        self._memories = memories

    save = durable(InMemoryMemoryStore.save)
    replace = durable(InMemoryMemoryStore.replace)
    delete = durable(InMemoryMemoryStore.delete)


MUTATORS = ("save", "replace", "delete")

__all__ = ["MUTATORS", "SQLiteMemoryStore"]
