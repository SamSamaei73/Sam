"""Audit sink abstraction for Knowledge operations.

Mirrors ``sam.coding.audit``/``sam.permissions.audit``/``sam.memory.audit``:
the same shape (``record(event) -> None``), the same fail-safe contract
(a failing sink never changes the result of the operation it is
recording), and the same "never log sensitive content" discipline. Its
own type — not a reuse of any prior phase's ``AuditEvent`` — for the same
reason every prior phase's audit module already documents.
"""

from __future__ import annotations

from collections.abc import Sequence
from threading import RLock
from typing import Protocol

from sam.knowledge.models import KnowledgeAuditEvent


class KnowledgeAuditSink(Protocol):
    """Persistence contract ``KnowledgeEngine`` depends on."""

    def record(self, event: KnowledgeAuditEvent) -> None: ...


class InMemoryKnowledgeAuditSink:
    """A process-local, lock-protected, append-only audit log for Phase 7."""

    def __init__(self) -> None:
        self._events: list[KnowledgeAuditEvent] = []
        self._lock = RLock()

    def record(self, event: KnowledgeAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def list_events(self) -> Sequence[KnowledgeAuditEvent]:
        with self._lock:
            return tuple(self._events)


class FailingKnowledgeAuditSink:
    """A sink that always raises — used to prove the fail-safe audit
    path: a result never depends on audit sink availability."""

    def record(self, event: KnowledgeAuditEvent) -> None:
        raise RuntimeError("audit sink unavailable")


__all__ = [
    "FailingKnowledgeAuditSink",
    "InMemoryKnowledgeAuditSink",
    "KnowledgeAuditSink",
]
