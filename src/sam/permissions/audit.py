"""Audit sink abstraction for permission evaluations.

``AuditSink`` is deliberately narrow: it accepts a fully-formed
``AuditEvent`` (already sanitized by the engine — see
``PermissionEngine._to_audit_event``) and persists it. It never sees the
raw ``PermissionRequest`` and therefore cannot accidentally record secrets,
hidden prompts, or anything not already deemed safe by the domain model.

``InMemoryAuditSink`` is a Phase 3 implementation only. Persistent storage
(PostgreSQL, per PROJECT_SPEC.json) is future-phase scope; this exists so
the engine has a deterministic, dependency-free sink to run against today
and so tests can assert on exactly what would have been recorded.
"""

from __future__ import annotations

from collections.abc import Sequence
from threading import RLock
from typing import Protocol

from sam.permissions.models import AuditEvent


class AuditSink(Protocol):
    """Persistence contract the engine depends on for audit events."""

    def record(self, event: AuditEvent) -> None:
        """Persist one audit event. Must not raise for a well-formed event
        in the in-memory implementation; a real sink's failure mode is
        implementation-defined and documented at its call site — see
        ``PermissionEngine.evaluate`` for how the engine treats a sink
        that does raise (best-effort: it never changes the returned
        decision)."""


class InMemoryAuditSink:
    """A process-local, lock-protected, append-only audit log for Phase 3."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = RLock()

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def list_events(self) -> Sequence[AuditEvent]:
        """Test/inspection helper: an immutable snapshot of recorded events."""

        with self._lock:
            return tuple(self._events)


class FailingAuditSink:
    """A sink that always raises — used to test the engine's fail-safe path.

    Exists in production code (not only in tests) because the contract it
    exercises — "the engine's returned decision never depends on audit
    sink availability" — is a security property, and the harness needs a
    deterministic collaborator that reliably fails to prove it.
    """

    def record(self, event: AuditEvent) -> None:
        raise RuntimeError("audit sink unavailable")
