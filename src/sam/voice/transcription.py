"""Transcription provider boundary.

``TranscriptionProvider`` is a narrow Protocol. The provider is **not
trusted**: its result goes through ``sam.voice.validation`` before anything
uses it and its exceptions are contained (no message ever escapes).

Timeout ownership (precise guarantee)
-------------------------------------
Sam owns the timeout. The gateway passes it to the provider as the
``timeout_seconds`` keyword argument — it is *not* a field of the request, and
nothing the request or the provider returns can change it. The call runs
**synchronously in the caller's thread**: the voice layer creates no thread,
worker, or background task, so nothing can outlive the call and nothing keeps
the audio after ``process`` returns.

Because Python cannot preempt synchronous code, Sam cannot forcibly stop a
provider that ignores the timeout. What Sam *does* enforce: (1) a provider
that raises ``TimeoutError`` is normalized to ``TranscriptionTimeoutError``;
(2) a result that arrives after the deadline is **discarded** and reported as
a timeout (a late result is never used); (3) exactly one call is made and
there is no retry. A real adapter must enforce ``timeout_seconds`` at its own
transport boundary — that is a requirement for production approval, not
something this layer can supply.

Phase 9 ships only ``FakeTranscriptionProvider`` — deterministic, in-process,
and finite. No cloud/local STT engine, no network, no subprocess. The fake
does not perform real speech recognition.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from threading import Event, RLock
from typing import Protocol

from sam.voice.errors import TranscriptionError, TranscriptionTimeoutError
from sam.voice.models import TranscriptionRequest, TranscriptionResult
from sam.voice.validation import validate_transcription_result

_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class TranscriptionProvider(Protocol):
    provider_id: str

    def transcribe(
        self, request: TranscriptionRequest, *, timeout_seconds: float
    ) -> object:
        """Return the raw (untrusted) result for one utterance.

        ``timeout_seconds`` is Sam's bound. The adapter must enforce it at
        its transport boundary and raise ``TimeoutError`` if it is exceeded.
        """


def require_valid_provider_id(provider_id: object) -> str:
    if not isinstance(provider_id, str) or not _PROVIDER_ID_RE.match(provider_id):
        raise ValueError("provider_id must be a short lowercase identifier")
    return provider_id


def simulate_provider_delay(
    delay_seconds: float, timeout_seconds: float, *, honor_timeout: bool
) -> None:
    """Deterministic, finite stand-in for provider latency (test fakes only).

    A cooperative provider (``honor_timeout=True``) raises ``TimeoutError``
    immediately when its delay would exceed the timeout — it never waits
    longer than Sam allowed. A non-cooperative one really waits the whole
    delay, then returns, so the gateway's late-result rejection is testable.
    """

    if delay_seconds <= 0:
        return
    if honor_timeout and delay_seconds > timeout_seconds:
        raise TimeoutError
    Event().wait(delay_seconds)


def transcribe(
    provider: TranscriptionProvider,
    request: TranscriptionRequest,
    *,
    timeout_seconds: float,
) -> TranscriptionResult:
    """Exactly one synchronous provider call; contained errors; validated
    result; a late result is discarded. No thread, no retry."""

    deadline = time.monotonic() + timeout_seconds
    try:
        raw = provider.transcribe(request, timeout_seconds=timeout_seconds)
    except TimeoutError:
        raise TranscriptionTimeoutError("transcription timed out") from None
    except Exception:
        raise TranscriptionError("transcription failed") from None
    if time.monotonic() > deadline:
        raise TranscriptionTimeoutError("transcription timed out") from None
    return validate_transcription_result(raw, provider.provider_id)


class FakeTranscriptionProvider:
    """Deterministic test provider. Records call counts, the timeout it was
    given, and audio *digests* only — never audio content or transcripts."""

    def __init__(
        self,
        result: object | Callable[[TranscriptionRequest], object] = "hello world",
        *,
        provider_id: str = "fake-stt",
        raises: BaseException | None = None,
        delay_seconds: float = 0.0,
        honor_timeout: bool = True,
    ) -> None:
        self.provider_id = require_valid_provider_id(provider_id)
        self._result = result
        self._raises = raises
        self._delay = delay_seconds
        self._honor = honor_timeout
        self._lock = RLock()
        self.call_count = 0
        self.seen_digests: list[str] = []
        self.seen_utterances: list[str] = []
        self.seen_timeouts: list[float] = []

    def transcribe(
        self, request: TranscriptionRequest, *, timeout_seconds: float
    ) -> object:
        with self._lock:
            self.call_count += 1
            self.seen_digests.append(request.audio.metadata.digest_sha256)
            self.seen_utterances.append(request.utterance_id)
            self.seen_timeouts.append(timeout_seconds)
        simulate_provider_delay(self._delay, timeout_seconds, honor_timeout=self._honor)
        if self._raises is not None:
            raise self._raises
        if callable(self._result):
            return self._result(request)
        return self._result

    def __repr__(self) -> str:
        return f"FakeTranscriptionProvider(provider_id={self.provider_id!r})"


__all__ = [
    "FakeTranscriptionProvider",
    "TranscriptionProvider",
    "require_valid_provider_id",
    "simulate_provider_delay",
    "transcribe",
]
