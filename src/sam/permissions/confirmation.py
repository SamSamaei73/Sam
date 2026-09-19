"""Confirmation abstraction: separate from authorization, one-time, bound.

A grant answers "is this within the user's authority?". A confirmation
answers "does the user approve this exact, specific execution right now?".
The two are never conflated: see ``PermissionEngine``, which requires a
confirmation to be freshly ``consume``d — matching the exact principal,
action, resource, scope, *and* target — before a HIGH/CRITICAL request can
become ``ALLOW``. Consuming transitions the record to ``CONSUMED`` so the
same confirmation id can never authorize a second action (replay
protection). No real UI is implemented here; ``InMemoryConfirmationProvider``
is a deterministic fake for tests and future wiring.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from threading import RLock
from typing import Protocol
from uuid import uuid4

from sam.permissions.errors import ConfirmationInvalidError, ConfirmationNotFoundError
from sam.permissions.models import (
    ConfirmationRecord,
    ConfirmationStatus,
    PermissionRequest,
    RiskLevel,
)

DEFAULT_CONFIRMATION_TTL = timedelta(minutes=5)


class ConfirmationProvider(Protocol):
    """Contract the engine depends on for the confirmation lifecycle."""

    def request(
        self, request: PermissionRequest, *, risk: RiskLevel, now: datetime
    ) -> ConfirmationRecord:
        """Create a new PENDING confirmation for exactly this request."""

    def decide(
        self, confirmation_id: str, *, approved: bool, now: datetime
    ) -> ConfirmationRecord:
        """Approve or deny a PENDING confirmation.

        Raises ``ConfirmationNotFoundError`` if unknown, or
        ``ConfirmationInvalidError`` if it is not PENDING (already decided,
        expired, or consumed) or has expired as of ``now``.
        """

    def get(self, confirmation_id: str) -> ConfirmationRecord | None:
        """Return the confirmation by id without changing its state."""

    def consume(
        self, confirmation_id: str, *, request: PermissionRequest, now: datetime
    ) -> ConfirmationRecord:
        """Atomically use an APPROVED confirmation to authorize ``request``.

        Raises ``ConfirmationNotFoundError`` or ``ConfirmationInvalidError``
        (wrong status, expired, or context mismatch) rather than ever
        returning a record that does not fully match. On success the
        record transitions to CONSUMED and can never be consumed again.
        """


class InMemoryConfirmationProvider:
    """A process-local, lock-protected confirmation store for Phase 3."""

    def __init__(self, *, ttl: timedelta = DEFAULT_CONFIRMATION_TTL) -> None:
        self._ttl = ttl
        self._records: dict[str, ConfirmationRecord] = {}
        self._lock = RLock()

    def request(
        self, request: PermissionRequest, *, risk: RiskLevel, now: datetime
    ) -> ConfirmationRecord:
        record = ConfirmationRecord(
            confirmation_id=uuid4().hex,
            principal=request.principal,
            action=request.action,
            resource=request.resource,
            scope=request.scope,
            target=request.target,
            reason=request.reason,
            risk=risk,
            status=ConfirmationStatus.PENDING,
            created_at=now,
            expires_at=now + self._ttl,
            correlation_id=request.correlation_id,
        )
        with self._lock:
            self._records[record.confirmation_id] = record
        return record

    def decide(
        self, confirmation_id: str, *, approved: bool, now: datetime
    ) -> ConfirmationRecord:
        with self._lock:
            existing = self._records.get(confirmation_id)
            if existing is None:
                raise ConfirmationNotFoundError(
                    f"no confirmation with id {confirmation_id!r}"
                )
            effective = self._expire_if_needed(existing, now=now)
            if effective.status is not ConfirmationStatus.PENDING:
                self._records[confirmation_id] = effective
                raise ConfirmationInvalidError(
                    "confirmation is not pending a decision"
                )
            decided = effective.model_copy(
                update={
                    "status": (
                        ConfirmationStatus.APPROVED
                        if approved
                        else ConfirmationStatus.DENIED
                    ),
                    "decided_at": now,
                }
            )
            self._records[confirmation_id] = decided
            return decided

    def get(self, confirmation_id: str) -> ConfirmationRecord | None:
        with self._lock:
            return self._records.get(confirmation_id)

    def consume(
        self, confirmation_id: str, *, request: PermissionRequest, now: datetime
    ) -> ConfirmationRecord:
        with self._lock:
            existing = self._records.get(confirmation_id)
            if existing is None:
                raise ConfirmationNotFoundError(
                    f"no confirmation with id {confirmation_id!r}"
                )
            effective = self._expire_if_needed(existing, now=now)
            self._records[confirmation_id] = effective
            if effective.status is not ConfirmationStatus.APPROVED:
                raise ConfirmationInvalidError(
                    "confirmation is not in an approved, consumable state"
                )
            if effective.expires_at <= now:
                # An approval that is never consumed within its validity
                # window must not become authorization arbitrarily later.
                expired = effective.model_copy(
                    update={"status": ConfirmationStatus.EXPIRED}
                )
                self._records[confirmation_id] = expired
                raise ConfirmationInvalidError("confirmation has expired")
            if not effective.matches(request):
                raise ConfirmationInvalidError(
                    "confirmation does not match the requested action"
                )
            consumed = effective.model_copy(
                update={"status": ConfirmationStatus.CONSUMED}
            )
            self._records[confirmation_id] = consumed
            return consumed

    def _list_for_test(self) -> Sequence[ConfirmationRecord]:
        """Test-only introspection helper; not part of the protocol."""

        with self._lock:
            return tuple(self._records.values())

    @staticmethod
    def _expire_if_needed(
        record: ConfirmationRecord, *, now: datetime
    ) -> ConfirmationRecord:
        if record.status is ConfirmationStatus.PENDING and record.expires_at <= now:
            return record.model_copy(update={"status": ConfirmationStatus.EXPIRED})
        return record
