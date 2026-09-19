"""Long-term memory storage abstraction (episodic / semantic / project).

``MemoryStore`` is deliberately dumb, the same discipline as
``sam.permissions.store.PermissionStore``: it persists and queries memory
records scoped by principal, but it never decides whether a memory
*should* exist — that judgment (policy, duplicate handling, ranking)
lives in ``sam.memory.policy`` and ``sam.memory.engine`` so the domain
logic stays in one auditable place, and so a future cloud store only has
to implement narrow, mechanical operations.

Ownership is enforced *by the store*, not by caller discipline: ``get``,
``update``, and ``delete`` all require a ``principal`` and silently behave
as "not found" for a memory that exists but belongs to someone else — a
lookup can never be used to probe whether another principal's memory id
exists.

``InMemoryMemoryStore`` is a Phase 4 implementation only: no database, no
disk. It is explicitly a development/test implementation, not intended
for production-scale storage — see ``docs/memory.md`` for the intended
future ``PostgresMemoryStore`` migration, which can implement this exact
protocol without any change to ``MemoryEngine``.
"""

from __future__ import annotations

from collections.abc import Sequence
from threading import RLock
from typing import Protocol

from sam.memory.errors import DuplicateMemoryError, MemoryNotFoundError
from sam.memory.models import Memory, MemoryType
from sam.permissions.models import Principal


class MemoryStore(Protocol):
    """Persistence contract ``MemoryEngine`` depends on."""

    def save(self, memory: Memory) -> Memory:
        """Persist a new memory. Raises ``DuplicateMemoryError`` on id reuse."""

    def get(self, memory_id: str, *, principal: Principal) -> Memory | None:
        """Return the memory if it exists *and* belongs to ``principal``.

        Returns ``None`` both when the id does not exist and when it
        belongs to a different principal — the two cases are
        indistinguishable to the caller by design.
        """

    def replace(self, memory: Memory, *, principal: Principal) -> Memory:
        """Replace an existing memory record with ``memory`` (same id).

        Raises ``MemoryNotFoundError`` if no record with that id exists
        for ``principal``. The caller (``MemoryEngine``) is responsible
        for constructing ``memory`` with an updated ``updated_at``.
        """

    def delete(self, memory_id: str, *, principal: Principal) -> None:
        """Permanently remove a memory. Raises ``MemoryNotFoundError`` if
        it does not exist for ``principal``. Unlike a Phase 3 permission
        grant, this is a real deletion, not a status change — see
        ``sam.memory.models.MemoryStatus``."""

    def list_candidates(
        self,
        *,
        principal: Principal,
        memory_types: Sequence[MemoryType] | None = None,
        project_id: str | None = None,
    ) -> Sequence[Memory]:
        """List every memory for ``principal`` matching the given filters.

        A dumb, unranked, unbounded-by-limit listing — ``MemoryEngine``
        and ``sam.memory.retrieval`` apply ranking and the caller's
        ``limit``. Never returns another principal's memories.
        """


class InMemoryMemoryStore:
    """A process-local, lock-protected long-term memory store for Phase 4.

    Not a persistence layer for production use. Each instance owns its
    own state — there is no module-level singleton — so tests and future
    request-scoped composition get fully isolated stores by constructing
    a new instance.
    """

    def __init__(self) -> None:
        self._memories: dict[str, Memory] = {}
        self._lock = RLock()

    def save(self, memory: Memory) -> Memory:
        with self._lock:
            if memory.memory_id in self._memories:
                raise DuplicateMemoryError(
                    f"a memory with id {memory.memory_id!r} already exists"
                )
            self._memories[memory.memory_id] = memory
            return memory

    def get(self, memory_id: str, *, principal: Principal) -> Memory | None:
        with self._lock:
            existing = self._memories.get(memory_id)
            if existing is None or existing.principal != principal:
                return None
            return existing

    def replace(self, memory: Memory, *, principal: Principal) -> Memory:
        with self._lock:
            existing = self._memories.get(memory.memory_id)
            if existing is None or existing.principal != principal:
                raise MemoryNotFoundError(f"no memory with id {memory.memory_id!r}")
            if memory.principal != principal:
                # Defensive: the engine must never be able to reassign a
                # memory's owner through an update.
                raise MemoryNotFoundError("a memory's principal cannot change")
            self._memories[memory.memory_id] = memory
            return memory

    def delete(self, memory_id: str, *, principal: Principal) -> None:
        with self._lock:
            existing = self._memories.get(memory_id)
            if existing is None or existing.principal != principal:
                raise MemoryNotFoundError(f"no memory with id {memory_id!r}")
            del self._memories[memory_id]

    def list_candidates(
        self,
        *,
        principal: Principal,
        memory_types: Sequence[MemoryType] | None = None,
        project_id: str | None = None,
    ) -> Sequence[Memory]:
        types = set(memory_types) if memory_types is not None else None
        with self._lock:
            return tuple(
                memory
                for memory in self._memories.values()
                if memory.principal == principal
                and (types is None or memory.memory_type in types)
                and (project_id is None or memory.project_id == project_id)
            )
