"""Audit sink abstraction for coding-agent operations.

Mirrors ``sam.permissions.audit``, ``sam.memory.audit``, and
``sam.computer.audit``: the same shape (``record(event) -> None``), the
same fail-safe contract (a failing sink never changes the result of the
operation it is recording, never propagates out of the executor), and
the same "never log sensitive content" discipline. Its own type — not a
reuse of any prior phase's ``AuditEvent`` — for the reason
``sam.memory.audit``/``sam.computer.audit`` already document: none of
those vocabularies fit a coding operation cleanly. This module does not
modify ``sam.permissions``, ``sam.memory``, or ``sam.computer``.
"""

from __future__ import annotations

from collections.abc import Sequence
from threading import RLock
from typing import Protocol

from sam.coding.models import CodingAuditEvent


class CodingAuditSink(Protocol):
    """Persistence contract ``CodingExecutor`` depends on."""

    def record(self, event: CodingAuditEvent) -> None:
        """Persist one audit event. A sink that raises has its failure
        caught by ``CodingExecutor`` and never changes the operation's
        result."""


class InMemoryCodingAuditSink:
    """A process-local, lock-protected, append-only audit log for Phase 6."""

    def __init__(self) -> None:
        self._events: list[CodingAuditEvent] = []
        self._lock = RLock()

    def record(self, event: CodingAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def list_events(self) -> Sequence[CodingAuditEvent]:
        """Test/inspection helper: an immutable snapshot of recorded events."""

        with self._lock:
            return tuple(self._events)


class FailingCodingAuditSink:
    """A sink that always raises — used to test the fail-safe audit path.

    Exists in production code, not only in tests, for the same reason
    every prior phase's equivalent does: "a result never depends on
    audit sink availability" needs a deterministic collaborator that
    reliably fails to prove it.
    """

    def record(self, event: CodingAuditEvent) -> None:
        raise RuntimeError("audit sink unavailable")
