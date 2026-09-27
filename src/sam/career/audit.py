"""Metadata-only audit for the Career & PhD Agent.

Recorded: operation, principal id, permission outcome, status, opportunity /
draft / document / outreach ids and versions, source kind, confirmation
reference, a closed-set reason code and timestamps. Never: a CV, a cover
letter, an application answer, a salary, a visa / work-authorization answer, a
message body, an opportunity description or a credential. There is no free-text
field, so none can be recorded by mistake.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import RLock
from typing import Protocol
from uuid import uuid4

MAX_EVENTS = 5_000


class CareerOperation(StrEnum):
    READ = "read"
    IMPORT = "import"
    DISCOVER = "discover"
    TRACK = "track"
    DRAFT = "draft"
    ANSWER = "answer"
    EDIT = "edit"
    APPROVE = "approve"
    SUBMIT = "submit"
    WITHDRAW = "withdraw"
    CONTACT = "contact"
    OUTREACH = "outreach"
    SEND = "send"
    FOLLOW_UP = "follow_up"
    PREFERENCES = "preferences"


@dataclass(frozen=True)
class CareerAuditEvent:
    event_id: str
    occurred_at: datetime
    operation: CareerOperation
    principal_id: str
    permission: str
    status: str
    opportunity_id: str | None = None
    item_id: str | None = None
    version: int | None = None
    source: str | None = None
    confirmation_id: str | None = None
    reason_code: str | None = None
    attempt_id: str | None = None
    manifest_hash: str | None = None  # the hash only, never the manifest


class CareerAuditSink(Protocol):
    def record(self, event: CareerAuditEvent) -> None: ...
    def events(self) -> tuple[CareerAuditEvent, ...]: ...


class InMemoryCareerAuditSink:
    def __init__(self, maxlen: int = MAX_EVENTS) -> None:
        self._events: deque[CareerAuditEvent] = deque(maxlen=maxlen)
        self._lock = RLock()

    def record(self, event: CareerAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def events(self) -> tuple[CareerAuditEvent, ...]:
        with self._lock:
            return tuple(self._events)


def new_event(
    operation: CareerOperation,
    *,
    at: datetime,
    principal_id: str,
    permission: str,
    status: str,
    **fields: object,
) -> CareerAuditEvent:
    return CareerAuditEvent(
        event_id=uuid4().hex,
        occurred_at=at,
        operation=operation,
        principal_id=principal_id,
        permission=permission,
        status=status,
        **fields,  # type: ignore[arg-type]
    )


__all__ = [
    "CareerAuditEvent",
    "CareerAuditSink",
    "CareerOperation",
    "InMemoryCareerAuditSink",
    "new_event",
]
