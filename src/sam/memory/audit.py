"""Audit sink abstraction for memory operations.

Deliberately mirrors ``sam.permissions.audit`` — the same shape
(``record(event) -> None``), the same fail-safe contract (a failing sink
never changes the result of the operation it is recording, never
propagates out of the engine), and the same "never log content"
discipline. It is its own protocol/type rather than a literal reuse of
``sam.permissions.audit.AuditSink`` because ``AuditEvent`` there is typed
around permission-decision vocabulary (``RiskLevel``, ``DecisionOutcome``,
grant/confirmation ids) that memory operations do not fit — forcing
memory operations through that model would mean inventing a fake "risk"
or "decision outcome" for a plain retrieval. See ``docs/memory.md`` for
the full rationale and the note that a future phase could unify both
under a single cross-cutting ``sam.audit`` package once a third
audit-emitting subsystem actually needs one. This module does not modify
anything under ``sam.permissions``.
"""

from __future__ import annotations

from collections.abc import Sequence
from threading import RLock
from typing import Protocol

from sam.memory.models import MemoryAuditEvent


class MemoryAuditSink(Protocol):
    """Persistence contract ``MemoryEngine`` depends on for audit events."""

    def record(self, event: MemoryAuditEvent) -> None:
        """Persist one audit event. A sink that raises has its failure
        caught by ``MemoryEngine`` — see its module docstring — and never
        changes the operation's result."""


class InMemoryMemoryAuditSink:
    """A process-local, lock-protected, append-only audit log for Phase 4."""

    def __init__(self) -> None:
        self._events: list[MemoryAuditEvent] = []
        self._lock = RLock()

    def record(self, event: MemoryAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def list_events(self) -> Sequence[MemoryAuditEvent]:
        """Test/inspection helper: an immutable snapshot of recorded events."""

        with self._lock:
            return tuple(self._events)


class FailingMemoryAuditSink:
    """A sink that always raises — used to test the engine's fail-safe path.

    Mirrors ``sam.permissions.audit.FailingAuditSink``: it exists in
    production code, not only in tests, because "the engine's result
    never depends on audit sink availability" is a property the test
    suite needs a deterministic collaborator to prove.
    """

    def record(self, event: MemoryAuditEvent) -> None:
        raise RuntimeError("audit sink unavailable")
