"""Audit sink for voice operations. Same shape and fail-safe contract as
every earlier phase: ``record(event) -> None``, and a failing sink never
changes a result. Events are content-free by construction — see
``sam.voice.models.VoiceAuditEvent``: there is no field for audio, transcript
text, spoken content, or biometric material.
"""

from __future__ import annotations

from collections.abc import Sequence
from threading import RLock
from typing import Protocol

from sam.voice.models import VoiceAuditEvent


class VoiceAuditSink(Protocol):
    def record(self, event: VoiceAuditEvent) -> None: ...


class InMemoryVoiceAuditSink:
    def __init__(self) -> None:
        self._events: list[VoiceAuditEvent] = []
        self._lock = RLock()

    def record(self, event: VoiceAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def list_events(self) -> Sequence[VoiceAuditEvent]:
        with self._lock:
            return tuple(self._events)


class FailingVoiceAuditSink:
    def record(self, event: VoiceAuditEvent) -> None:
        raise RuntimeError("audit sink unavailable")


__all__ = ["FailingVoiceAuditSink", "InMemoryVoiceAuditSink", "VoiceAuditSink"]
