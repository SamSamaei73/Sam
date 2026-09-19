"""Audit sink abstraction for computer-control actions.

Mirrors ``sam.permissions.audit`` and ``sam.memory.audit``: the same
shape (``record(event) -> None``), the same fail-safe contract (a failing
sink never changes the result of the action it is recording, never
propagates out of the controller), and the same "never log sensitive
content" discipline. Its own type — not a reuse of either existing
``AuditEvent`` — for the reason ``sam.memory.audit`` already documents:
neither vocabulary fits a computer action cleanly, and forcing one to
would mean inventing values that do not apply. This module does not
modify ``sam.permissions`` or ``sam.memory`` in any way.
"""

from __future__ import annotations

from collections.abc import Sequence
from threading import RLock
from typing import Protocol

from sam.computer.models import ComputerAuditEvent


class ComputerAuditSink(Protocol):
    """Persistence contract ``ComputerController`` depends on."""

    def record(self, event: ComputerAuditEvent) -> None:
        """Persist one audit event. A sink that raises has its failure
        caught by ``ComputerController`` and never changes the action's
        result."""


class InMemoryComputerAuditSink:
    """A process-local, lock-protected, append-only audit log for Phase 5."""

    def __init__(self) -> None:
        self._events: list[ComputerAuditEvent] = []
        self._lock = RLock()

    def record(self, event: ComputerAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def list_events(self) -> Sequence[ComputerAuditEvent]:
        """Test/inspection helper: an immutable snapshot of recorded events."""

        with self._lock:
            return tuple(self._events)


class FailingComputerAuditSink:
    """A sink that always raises — used to test the fail-safe audit path.

    Exists in production code, not only in tests, for the same reason
    ``sam.permissions.audit.FailingAuditSink`` does: the property "a
    result never depends on audit sink availability" needs a
    deterministic collaborator that reliably fails to prove it.
    """

    def record(self, event: ComputerAuditEvent) -> None:
        raise RuntimeError("audit sink unavailable")
