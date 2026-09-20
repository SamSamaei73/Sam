"""Content-free voice-identity audit.

The event vocabulary is closed and no event type has a field that could hold
audio, an embedding, a template, a challenge, a transcript, or a secret.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Protocol

from sam.voice_identity.models import utc_now

ALLOWED_EVENTS = frozenset(
    {
        "owner_enrollment_started",
        "owner_enrollment_completed",
        "owner_enrollment_failed",
        "owner_profile_deleted",
        "owner_voice_verified",
        "owner_voice_not_verified",
        "challenge_issued",
        "challenge_passed",
        "challenge_failed",
        "guest_mode_started",
        "guest_mode_expired",
        "guest_mode_revoked",
        "speaker_blocked",
        "language_detected",
    }
)
_REASON = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_EVENTS = 500


@dataclass(frozen=True)
class VoiceIdentityAuditEvent:
    event: str
    reason_code: str
    occurred_at: datetime
    detail: str | None = None  # a closed-vocabulary tag, e.g. a language code


class VoiceIdentityAuditSink(Protocol):
    def record(
        self, event: str, reason_code: str = "ok", detail: str | None = None
    ) -> None: ...


class InMemoryVoiceIdentityAuditSink:
    def __init__(self) -> None:
        self._events: deque[VoiceIdentityAuditEvent] = deque(maxlen=MAX_EVENTS)
        self._lock = RLock()

    def record(
        self, event: str, reason_code: str = "ok", detail: str | None = None
    ) -> None:
        if event not in ALLOWED_EVENTS or not _REASON.match(reason_code):
            raise ValueError("invalid audit event")
        if detail is not None and not _REASON.match(detail):
            raise ValueError("invalid audit detail")
        with self._lock:
            self._events.append(
                VoiceIdentityAuditEvent(event, reason_code, utc_now(), detail)
            )

    def events(self) -> tuple[VoiceIdentityAuditEvent, ...]:
        with self._lock:
            return tuple(self._events)


__all__ = [
    "ALLOWED_EVENTS",
    "InMemoryVoiceIdentityAuditSink",
    "VoiceIdentityAuditEvent",
    "VoiceIdentityAuditSink",
]
