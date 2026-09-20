"""Audit sink for synthesis requests. Same shape and fail-safe contract as
every earlier phase (a failing sink never changes a result). Events are
content-free by construction — see ``sam.tts.models.TTSAuditEvent``."""

from __future__ import annotations

from collections.abc import Sequence
from threading import RLock
from typing import Protocol

from sam.tts.models import TTSAuditEvent


class TTSAuditSink(Protocol):
    def record(self, event: TTSAuditEvent) -> None: ...


class InMemoryTTSAuditSink:
    def __init__(self) -> None:
        self._events: list[TTSAuditEvent] = []
        self._lock = RLock()

    def record(self, event: TTSAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def list_events(self) -> Sequence[TTSAuditEvent]:
        with self._lock:
            return tuple(self._events)


class FailingTTSAuditSink:
    def record(self, event: TTSAuditEvent) -> None:
        raise RuntimeError("audit sink unavailable")


__all__ = ["FailingTTSAuditSink", "InMemoryTTSAuditSink", "TTSAuditSink"]
