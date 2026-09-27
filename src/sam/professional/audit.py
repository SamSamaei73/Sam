"""Metadata-only audit events for Professional Intelligence.

An event records THAT something happened (which operation, what the permission
engine decided, how many candidates were accepted or rejected, which claim and
source ids were involved), never WHAT the documents said. There is deliberately
no free-text field: nothing typed here can carry a CV line, a transcript, a
private note, a salary or visa detail, evidence text or a source file name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import RLock
from typing import Protocol
from uuid import uuid4

MAX_CLAIM_IDS = 50


class ProfessionalOperation(StrEnum):
    READ = "read"
    SEARCH = "search"
    EVIDENCE_FOR = "evidence_for"
    ASSESS_CLAIM = "assess_claim"
    INGEST = "ingest"
    CONFIRM = "confirm"
    REJECT = "reject"
    RESOLVE_CONFLICT = "resolve_conflict"
    SET_PRIVACY = "set_privacy"
    REMOVE_SOURCE = "remove_source"


# The only count names an event may carry. Values are integers.
COUNT_KEYS = frozenset(
    {
        "results",
        "candidates_proposed",
        "candidates_accepted",
        "candidates_rejected",
        "claims_created",
        "claims_updated",
        "claims_skipped_rejected",
        "evidence_added",
        "conflicts_open",
        "claims_affected",
        "unmapped_skills",
    }
)


@dataclass(frozen=True)
class ProfessionalAuditEvent:
    event_id: str
    occurred_at: datetime
    operation: ProfessionalOperation
    principal_id: str
    permission_outcome: str
    status: str
    source_id: str | None = None
    claim_ids: tuple[str, ...] = ()
    counts: Mapping[str, int] | None = None
    error_category: str | None = None

    def __post_init__(self) -> None:
        if self.counts is not None and not set(self.counts) <= COUNT_KEYS:
            raise ValueError("unknown audit count")


class ProfessionalAuditSink(Protocol):
    def record(self, event: ProfessionalAuditEvent) -> None: ...


class InMemoryProfessionalAuditSink:
    def __init__(self) -> None:
        self._lock = RLock()
        self._events: list[ProfessionalAuditEvent] = []

    def record(self, event: ProfessionalAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def events(self) -> tuple[ProfessionalAuditEvent, ...]:
        with self._lock:
            return tuple(self._events)


def new_event(
    *,
    now: datetime,
    operation: ProfessionalOperation,
    principal_id: str,
    permission_outcome: str,
    status: str,
    source_id: str | None = None,
    claim_ids: tuple[str, ...] = (),
    counts: Mapping[str, int] | None = None,
    error_category: str | None = None,
) -> ProfessionalAuditEvent:
    return ProfessionalAuditEvent(
        event_id=uuid4().hex,
        occurred_at=now,
        operation=operation,
        principal_id=principal_id,
        permission_outcome=permission_outcome,
        status=status,
        source_id=source_id,
        claim_ids=tuple(claim_ids[:MAX_CLAIM_IDS]),
        counts=dict(counts) if counts else None,
        error_category=error_category,
    )


__all__ = [
    "COUNT_KEYS",
    "InMemoryProfessionalAuditSink",
    "ProfessionalAuditEvent",
    "ProfessionalAuditSink",
    "ProfessionalOperation",
    "new_event",
]
