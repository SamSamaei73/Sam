"""Request-scoped speech-recognition language hint.

Phase 9 deliberately pins ``VoiceProcessingRequest`` to fields that cannot carry
authority, so the user's language preference reaches the recognizer through a
context-scoped wrapper instead of a new request field. The hint only nudges
Whisper toward Persian or English (a 1-second clip is otherwise often
mis-detected); it is never authority and is cleared when the request ends.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sam.voice.models import TranscriptionRequest
from sam.voice.transcription import TranscriptionProvider

_HINT: ContextVar[str | None] = ContextVar("sam_stt_language_hint", default=None)
ALLOWED_HINTS = frozenset({"fa", "en"})


@contextmanager
def language_hint_scope(hint: str | None) -> Iterator[None]:
    token = _HINT.set(hint if hint in ALLOWED_HINTS else None)
    try:
        yield
    finally:
        _HINT.reset(token)


class HintedTranscriptionProvider:
    """Applies the in-scope hint (if any) to each transcription request."""

    def __init__(self, inner: TranscriptionProvider) -> None:
        self._inner = inner
        self.provider_id = inner.provider_id

    def transcribe(
        self, request: TranscriptionRequest, *, timeout_seconds: float
    ) -> object:
        hint = _HINT.get()
        if hint is not None and request.language_hint is None:
            request = request.model_copy(update={"language_hint": hint})
        return self._inner.transcribe(request, timeout_seconds=timeout_seconds)

    def __repr__(self) -> str:
        return f"HintedTranscriptionProvider({self._inner!r})"


__all__ = ["ALLOWED_HINTS", "HintedTranscriptionProvider", "language_hint_scope"]
