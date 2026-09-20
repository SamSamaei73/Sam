"""Validation of the *untrusted* transcription-provider result.

A provider may return an oversized or empty transcript, a malformed shape,
unexpected metadata, a fake confidence, or hostile text. Everything is
checked here before anything downstream sees it. Policy:

* an oversized transcript is **rejected, never truncated** (a silently
  truncated command is a different command);
* text is otherwise returned unchanged — only surrounding whitespace is
  stripped; NUL and other control characters are rejected;
* unknown result keys are rejected (the provider contract is closed);
* ``confidence`` must be finite in [0, 1] and is informational only.

The transcript text is treated as user input. Nothing here inspects it for
instructions, and nothing about it is ever an authorization.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

from pydantic import ValidationError

from sam.voice.errors import InvalidTranscriptError
from sam.voice.models import (
    MAX_TRANSCRIPT_LENGTH,
    TranscriptionResult,
    VoiceErrorCategory,
)

_ALLOWED_KEYS = frozenset({"text", "language", "confidence"})
_BAD_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def validate_transcription_result(raw: object, provider_id: str) -> TranscriptionResult:
    """Return a validated ``TranscriptionResult`` or raise
    ``InvalidTranscriptError`` (fail closed). Messages never echo content."""

    text: object
    language: object = None
    confidence: object = None
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, Mapping):
        if set(raw) - _ALLOWED_KEYS:
            raise InvalidTranscriptError("provider result has unexpected fields")
        text = raw.get("text")
        language = raw.get("language")
        confidence = raw.get("confidence")
    else:
        raise InvalidTranscriptError("provider result has an unexpected shape")

    if not isinstance(text, str):
        raise InvalidTranscriptError("provider transcript is not text")
    if len(text) > MAX_TRANSCRIPT_LENGTH:
        raise InvalidTranscriptError(
            "transcript exceeds the size limit",
            category=VoiceErrorCategory.TRANSCRIPT_TOO_LARGE,
        )
    if _BAD_CONTROL.search(text):
        raise InvalidTranscriptError("transcript contains control characters")
    text = text.strip()
    if not text:
        raise InvalidTranscriptError(
            "transcript is empty", category=VoiceErrorCategory.EMPTY_TRANSCRIPT
        )

    if language is not None and not isinstance(language, str):
        raise InvalidTranscriptError("provider language is malformed")
    if confidence is not None:
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not math.isfinite(confidence)
            or not 0.0 <= confidence <= 1.0
        ):
            raise InvalidTranscriptError("provider confidence is malformed")
    try:
        return TranscriptionResult(
            text=text,
            provider_id=provider_id,
            language=language,
            reported_confidence=None if confidence is None else float(confidence),
        )
    except ValidationError:
        raise InvalidTranscriptError("provider result is malformed") from None


__all__ = ["validate_transcription_result"]
