"""Working memory: short-lived, explicitly non-permanent context.

A completely separate store and protocol from ``MemoryStore`` — see
``sam.memory.models.WorkingMemoryEntry`` for why that is a distinct type
rather than a ``memory_type`` value on ``Memory``. Working memory never
passes through ``MemoryPolicy`` and can never end up in the long-term
store; there is no code path from one to the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from threading import RLock
from typing import Protocol

from sam.memory.errors import WorkingMemoryFullError
from sam.memory.models import WorkingMemoryEntry
from sam.permissions.models import Principal

DEFAULT_MAX_WORKING_SLOTS = 50


class WorkingMemoryStore(Protocol):
    """Persistence contract for short-lived, per-principal working memory."""

    def set(self, entry: WorkingMemoryEntry, *, now: datetime) -> WorkingMemoryEntry:
        """Create or replace the slot at ``(entry.principal, entry.key)``.

        Raises ``WorkingMemoryFullError`` if this would create a *new*
        slot beyond the store's bounded per-principal capacity —
        replacing an existing key is always allowed.
        """

    def get(
        self, principal: Principal, key: str, *, now: datetime
    ) -> WorkingMemoryEntry | None:
        """Return the entry, or ``None`` if missing or expired."""

    def list(
        self, principal: Principal, *, now: datetime
    ) -> Sequence[WorkingMemoryEntry]:
        """List every unexpired entry for ``principal``."""

    def delete(self, principal: Principal, key: str) -> None:
        """Remove one slot; a no-op if it does not exist."""

    def clear(self, principal: Principal) -> None:
        """Remove every slot for ``principal``."""


class InMemoryWorkingMemoryStore:
    """A process-local, lock-protected working-memory store for Phase 4."""

    def __init__(
        self, *, max_slots_per_principal: int = DEFAULT_MAX_WORKING_SLOTS
    ) -> None:
        if max_slots_per_principal <= 0:
            raise ValueError("max_slots_per_principal must be positive")
        self._max_slots = max_slots_per_principal
        self._entries: dict[tuple[Principal, str], WorkingMemoryEntry] = {}
        self._lock = RLock()

    def set(self, entry: WorkingMemoryEntry, *, now: datetime) -> WorkingMemoryEntry:
        with self._lock:
            self._purge_expired(entry.principal, now=now)
            slot_key = (entry.principal, entry.key)
            is_new_slot = slot_key not in self._entries
            if is_new_slot and self._count_for(entry.principal) >= self._max_slots:
                raise WorkingMemoryFullError(
                    f"principal already has {self._max_slots} working memory slots"
                )
            self._entries[slot_key] = entry
            return entry

    def get(
        self, principal: Principal, key: str, *, now: datetime
    ) -> WorkingMemoryEntry | None:
        with self._lock:
            entry = self._entries.get((principal, key))
            if entry is None:
                return None
            if not entry.is_usable(now=now):
                del self._entries[(principal, key)]
                return None
            return entry

    def list(
        self, principal: Principal, *, now: datetime
    ) -> Sequence[WorkingMemoryEntry]:
        with self._lock:
            self._purge_expired(principal, now=now)
            return tuple(
                entry
                for (owner, _key), entry in self._entries.items()
                if owner == principal
            )

    def delete(self, principal: Principal, key: str) -> None:
        with self._lock:
            self._entries.pop((principal, key), None)

    def clear(self, principal: Principal) -> None:
        with self._lock:
            for slot_key in [k for k in self._entries if k[0] == principal]:
                del self._entries[slot_key]

    def _count_for(self, principal: Principal) -> int:
        return sum(1 for owner, _key in self._entries if owner == principal)

    def _purge_expired(self, principal: Principal, *, now: datetime) -> None:
        expired = [
            slot_key
            for slot_key, entry in self._entries.items()
            if slot_key[0] == principal and not entry.is_usable(now=now)
        ]
        for slot_key in expired:
            del self._entries[slot_key]
