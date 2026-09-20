"""Voice identity: an authentication *signal*, never an authorization.

    Voice identity is an authentication signal, not an authorization decision.

A ``VoiceIdentityProvider`` reports whether the speaker appears to be the
expected person. ``VoiceIdentityService`` turns that into a transient,
context-bound, signed ``VoiceIdentitySignal``. Nothing in this module — or
anywhere in ``sam.voice`` — maps a signal, however confident, onto an allow,
an approved confirmation, a scope, or a risk level. Sam owns any future
authentication policy; a provider's confidence is informational only.

Binding and replay safety. A signal is bound to one session, one utterance,
one audio digest and one provider, carries a short expiry, and is signed with
a per-service HMAC key that never leaves the service, so a signal cannot be
forged or edited, applied to a different utterance or session, or used twice
(consumption is one-time, in a bounded ledger).

No biometric persistence: the provider receives audio in memory for one
call; no voiceprint, embedding, or template is stored or returned, and there
is no enrollment. ``FakeVoiceIdentityProvider`` is deterministic test code.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import Protocol

from pydantic import ValidationError

from sam.voice.errors import VoiceIdentityError
from sam.voice.models import (
    DEFAULT_VOICE_PROVIDER_TIMEOUT_SECONDS,
    MAX_IDENTITY_SIGNAL_AGE_SECONDS,
    MAX_TRACKED_IDENTITY_SIGNALS,
    MAX_VOICE_PROVIDER_TIMEOUT_SECONDS,
    ValidatedAudio,
    VoiceIdentitySignal,
    VoiceIdentityStatus,
    new_id,
    utc_now,
)
from sam.voice.transcription import require_valid_provider_id, simulate_provider_delay

_CLOCK_SKEW = timedelta(seconds=5)
_PROVIDER_STATUSES = frozenset(
    {
        VoiceIdentityStatus.VERIFIED,
        VoiceIdentityStatus.NOT_VERIFIED,
        VoiceIdentityStatus.UNKNOWN,
    }
)


@dataclass(frozen=True)
class VoiceIdentityRequest:
    """What an identity provider is given for one call."""

    session_id: str
    utterance_id: str
    audio: ValidatedAudio

    def __repr__(self) -> str:
        return (
            f"VoiceIdentityRequest(session_id={self.session_id!r}, "
            f"utterance_id={self.utterance_id!r})"
        )


class VoiceIdentityProvider(Protocol):
    provider_id: str

    def assess(
        self, request: VoiceIdentityRequest, *, timeout_seconds: float
    ) -> object:
        """Return an untrusted assessment: a status (``verified`` /
        ``not_verified`` / ``unknown``) and optional confidence."""


class FakeVoiceIdentityProvider:
    def __init__(
        self,
        status: VoiceIdentityStatus | str = VoiceIdentityStatus.VERIFIED,
        confidence: float | None = 1.0,
        *,
        provider_id: str = "fake-speaker",
        result: object | None = None,
        raises: BaseException | None = None,
        delay_seconds: float = 0.0,
        honor_timeout: bool = True,
    ) -> None:
        self.provider_id = require_valid_provider_id(provider_id)
        self._delay = delay_seconds
        self._honor = honor_timeout
        self._status = status
        self._confidence = confidence
        self._result = result
        self._raises = raises
        self.call_count = 0

    def assess(
        self, request: VoiceIdentityRequest, *, timeout_seconds: float
    ) -> object:
        self.call_count += 1
        simulate_provider_delay(self._delay, timeout_seconds, honor_timeout=self._honor)
        if self._raises is not None:
            raise self._raises
        if self._result is not None:
            return self._result
        return {"status": self._status, "confidence": self._confidence}

    def __repr__(self) -> str:
        return f"FakeVoiceIdentityProvider(provider_id={self.provider_id!r})"


def _normalize(raw: object) -> tuple[VoiceIdentityStatus, float | None]:
    """Validate an untrusted provider assessment. Anything malformed raises."""

    if not isinstance(raw, dict) or set(raw) - {"status", "confidence"}:
        raise VoiceIdentityError("identity assessment is malformed")
    try:
        status = VoiceIdentityStatus(str(raw.get("status")))
    except ValueError:
        raise VoiceIdentityError("identity assessment is malformed") from None
    if status not in _PROVIDER_STATUSES:
        raise VoiceIdentityError("identity assessment is malformed")
    confidence = raw.get("confidence")
    if confidence is not None:
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not math.isfinite(confidence)
            or not 0.0 <= confidence <= 1.0
        ):
            raise VoiceIdentityError("identity assessment is malformed")
        confidence = float(confidence)
    return status, confidence


class VoiceIdentityService:
    """Assess, sign, verify, and one-time-consume identity signals.

    Instance-owned state only (key + bounded ledger); nothing global."""

    def __init__(
        self,
        provider: VoiceIdentityProvider,
        *,
        clock: Callable[[], datetime] = utc_now,
        timeout_seconds: float = DEFAULT_VOICE_PROVIDER_TIMEOUT_SECONDS,
        key: bytes | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= MAX_VOICE_PROVIDER_TIMEOUT_SECONDS:
            raise ValueError("timeout must be within the voice provider bound")
        self._provider = provider
        self.provider_id = require_valid_provider_id(provider.provider_id)
        self._clock = clock
        self._timeout = timeout_seconds
        self._key = key if key is not None else secrets.token_bytes(32)
        self._consumed: OrderedDict[str, None] = OrderedDict()
        self._lock = RLock()

    def assess(
        self, session_id: str, utterance_id: str, audio: ValidatedAudio
    ) -> VoiceIdentitySignal:
        """One provider call (finite timeout, contained errors) -> a signed,
        context-bound signal. Raises ``VoiceIdentityError`` on any failure."""

        request = VoiceIdentityRequest(
            session_id=session_id, utterance_id=utterance_id, audio=audio
        )
        deadline = time.monotonic() + self._timeout
        try:
            raw = self._provider.assess(request, timeout_seconds=self._timeout)
        except Exception:
            # Includes TimeoutError: contained, no message, no cause.
            raise VoiceIdentityError("identity provider failed") from None
        if time.monotonic() > deadline:
            # A late assessment is discarded, never used.
            raise VoiceIdentityError("identity provider failed") from None
        status, confidence = _normalize(raw)
        return self._mint(
            status, confidence, session_id, utterance_id, audio.metadata.digest_sha256
        )

    def verify_and_consume(
        self,
        signal: VoiceIdentitySignal,
        *,
        session_id: str,
        utterance_id: str,
        audio_digest: str,
    ) -> None:
        """Accept ``signal`` for exactly this context, once, or raise."""

        expected = self._sign(signal)
        if not hmac.compare_digest(expected, signal.signature):
            raise VoiceIdentityError("identity signal is invalid")
        if signal.provider_id != self.provider_id:
            raise VoiceIdentityError("identity signal is invalid")
        if (
            signal.session_id != session_id
            or signal.utterance_id != utterance_id
            or signal.audio_digest != audio_digest
        ):
            raise VoiceIdentityError("identity signal does not match this utterance")
        now = self._clock()
        if signal.issued_at > now + _CLOCK_SKEW:
            raise VoiceIdentityError("identity signal is invalid")
        if now - signal.issued_at > timedelta(seconds=MAX_IDENTITY_SIGNAL_AGE_SECONDS):
            raise VoiceIdentityError("identity signal has expired")
        with self._lock:
            if signal.signal_id in self._consumed:
                raise VoiceIdentityError("identity signal was already used")
            self._consumed[signal.signal_id] = None
            while len(self._consumed) > MAX_TRACKED_IDENTITY_SIGNALS:
                self._consumed.popitem(last=False)

    # ---------------- internals ----------------

    def _mint(
        self,
        status: VoiceIdentityStatus,
        confidence: float | None,
        session_id: str,
        utterance_id: str,
        digest: str,
    ) -> VoiceIdentitySignal:
        unsigned = VoiceIdentitySignal(
            signal_id=new_id(),
            status=status,
            session_id=session_id,
            utterance_id=utterance_id,
            audio_digest=digest,
            provider_id=self.provider_id,
            issued_at=self._clock(),
            reported_confidence=confidence,
            signature="0" * 64,
        )
        try:
            return unsigned.model_copy(update={"signature": self._sign(unsigned)})
        except ValidationError:
            raise VoiceIdentityError("identity signal could not be created") from None

    def _sign(self, signal: VoiceIdentitySignal) -> str:
        fields = "\x1f".join(
            (
                signal.signal_id,
                signal.status.value,
                signal.session_id,
                signal.utterance_id,
                signal.audio_digest,
                signal.provider_id,
                signal.issued_at.isoformat(),
                repr(signal.reported_confidence),
            )
        )
        return hmac.new(self._key, fields.encode("utf-8"), hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return f"VoiceIdentityService(provider_id={self.provider_id!r})"


__all__ = [
    "FakeVoiceIdentityProvider",
    "VoiceIdentityProvider",
    "VoiceIdentityRequest",
    "VoiceIdentityService",
]
