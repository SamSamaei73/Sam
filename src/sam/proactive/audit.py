"""Metadata-only audit for the proactive engine.

An event records WHAT happened (operation, ids, closed-set categories,
timestamps), never any text: not the task title, not the instruction, not a
prompt, not a summary, not an observation. There is no free-text field, so
none can be recorded by mistake.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import RLock
from typing import Protocol
from uuid import uuid4

from sam.proactive.models import (
    ChangeKind,
    RunFailure,
    RunResult,
    RunTrigger,
    TaskType,
    TimingMode,
)

MAX_AUDIT_EVENTS = 2_000


class ProactiveOperation(StrEnum):
    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    RUN = "run"
    RUN_NOW = "run_now"
    NOTIFICATION = "notification"
    PROPOSAL = "proposal"


@dataclass(frozen=True)
class ProactiveAuditEvent:
    event_id: str
    occurred_at: datetime
    operation: ProactiveOperation
    principal_id: str
    permission: str
    status: str
    task_id: str | None = None
    task_type: TaskType | None = None
    timing_mode: TimingMode | None = None
    trigger: RunTrigger | None = None
    result: RunResult | None = None
    failure: RunFailure | None = None
    change: ChangeKind | None = None
    reason_code: str | None = None
    notification_created: bool = False
    provider_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ProactiveAuditSink(Protocol):
    def record(self, event: ProactiveAuditEvent) -> None: ...
    def events(self) -> tuple[ProactiveAuditEvent, ...]: ...


class InMemoryProactiveAuditSink:
    def __init__(self, maxlen: int = MAX_AUDIT_EVENTS) -> None:
        self._events: deque[ProactiveAuditEvent] = deque(maxlen=maxlen)
        self._lock = RLock()

    def record(self, event: ProactiveAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def events(self) -> tuple[ProactiveAuditEvent, ...]:
        with self._lock:
            return tuple(self._events)


def new_event(
    operation: ProactiveOperation,
    *,
    occurred_at: datetime,
    principal_id: str,
    permission: str,
    status: str,
    **fields: object,
) -> ProactiveAuditEvent:
    return ProactiveAuditEvent(
        event_id=uuid4().hex,
        occurred_at=occurred_at,
        operation=operation,
        principal_id=principal_id,
        permission=permission,
        status=status,
        **fields,  # type: ignore[arg-type]
    )


__all__ = [
    "InMemoryProactiveAuditSink",
    "ProactiveAuditEvent",
    "ProactiveAuditSink",
    "ProactiveOperation",
    "new_event",
]
