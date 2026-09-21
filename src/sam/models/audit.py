"""Content-free routing audit.

Only safe metadata: no prompt, response, credential, header or provider body.
Every free-text field is a closed lowercase code, so nothing private can be
smuggled in through an event.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from threading import RLock
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from sam.models.models import (
    BillingMode,
    FailureCategory,
    PrivacyClass,
    ProviderId,
    ReasonCode,
    TaskProfile,
)

MAX_EVENTS = 500


class RoutingAuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    occurred_at: datetime
    task_profile: TaskProfile
    privacy_class: PrivacyClass
    provider_id: ProviderId | None
    model_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    cost_class: BillingMode | None
    reason_codes: tuple[ReasonCode, ...]
    fallback_used: bool
    status: FailureCategory
    latency_ms: int = Field(ge=0)
    input_size: int = Field(ge=0)
    output_size: int = Field(ge=0)


class RoutingAuditSink(Protocol):
    def record(self, event: RoutingAuditEvent) -> None: ...


class InMemoryRoutingAuditSink:
    def __init__(self) -> None:
        self._events: deque[RoutingAuditEvent] = deque(maxlen=MAX_EVENTS)
        self._lock = RLock()

    def record(self, event: RoutingAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def events(self) -> tuple[RoutingAuditEvent, ...]:
        with self._lock:
            return tuple(self._events)


def now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "InMemoryRoutingAuditSink",
    "RoutingAuditEvent",
    "RoutingAuditSink",
]
