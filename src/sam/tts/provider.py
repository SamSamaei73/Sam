"""Provider-independent speech-synthesis contract.

``TTSGateway`` depends on this protocol, never on Fish Audio. A provider is
**not trusted**: its result is validated by the gateway (bytes, size,
signature, digest computed by Sam), its exceptions are contained, and it can
influence nothing but the bytes it returns — not permission, risk, scope,
voice, model, endpoint, or timeout. Sam passes the timeout as
``timeout_seconds``; a real adapter must enforce it at its own transport
boundary (see docs/tts.md).

``FakeSpeechSynthesisProvider`` is deterministic, in-process, and produces
obviously synthetic MP3-shaped bytes that do not contain the input text.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from threading import Event, RLock
from typing import Protocol

from sam.tts.models import ProviderSynthesisRequest, ProviderSynthesisResult

_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# A minimal, structurally valid ID3v2 header + one MPEG-1 Layer III frame
# header. Synthetic; decodes to nothing meaningful.
_FAKE_MP3_PREFIX = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00"


class SpeechSynthesisProvider(Protocol):
    provider_id: str

    def synthesize(
        self, request: ProviderSynthesisRequest, *, timeout_seconds: float
    ) -> ProviderSynthesisResult:
        """Return raw (untrusted) audio for one request. Must honor
        ``timeout_seconds`` at its transport boundary. Must make at most one
        billable call and must not retry."""


def require_valid_provider_id(provider_id: object) -> str:
    if not isinstance(provider_id, str) or not _PROVIDER_ID_RE.match(provider_id):
        raise ValueError("provider_id must be a short lowercase identifier")
    return provider_id


def fake_audio_for(request: ProviderSynthesisRequest) -> bytes:
    """Deterministic synthetic audio derived from a digest, never the text."""

    digest = hashlib.sha256(request.synthesis_id.encode("utf-8")).digest()
    return _FAKE_MP3_PREFIX + digest * 8


class FakeSpeechSynthesisProvider:
    """Deterministic test provider. Records call counts and the *trusted*
    inputs it was handed (voice, model, timeout) — never the text."""

    def __init__(
        self,
        result: object | Callable[[ProviderSynthesisRequest], object] | None = None,
        *,
        provider_id: str = "fake-tts",
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
        self.seen_voices: list[str] = []
        self.seen_models: list[str] = []
        self.seen_timeouts: list[float] = []
        self.seen_text_lengths: list[int] = []

    def synthesize(
        self, request: ProviderSynthesisRequest, *, timeout_seconds: float
    ) -> ProviderSynthesisResult:
        with self._lock:
            self.call_count += 1
            self.seen_voices.append(request.voice_reference)
            self.seen_models.append(request.model)
            self.seen_timeouts.append(timeout_seconds)
            self.seen_text_lengths.append(len(request.text))
        if self._delay > 0:
            if self._honor and self._delay > timeout_seconds:
                raise TimeoutError
            Event().wait(self._delay)
        if self._raises is not None:
            raise self._raises
        if self._result is None:
            return ProviderSynthesisResult(
                audio_bytes=fake_audio_for(request), content_type="audio/mpeg"
            )
        produced = self._result(request) if callable(self._result) else self._result
        return produced  # type: ignore[return-value]  # deliberately untrusted

    def __repr__(self) -> str:
        return f"FakeSpeechSynthesisProvider(provider_id={self.provider_id!r})"


__all__ = [
    "FakeSpeechSynthesisProvider",
    "SpeechSynthesisProvider",
    "fake_audio_for",
    "require_valid_provider_id",
]
