"""Pure validation of the text to be synthesized. Runs before any permission
check, provider call, or credential lookup.

The text is *data*: it is validated, never interpreted. It is not searched
for instructions, provider tags, voice/model/endpoint selectors, or anything
else that could act as a control channel — those are, at most, ordinary
characters that Fish may render acoustically (see docs/tts.md).
"""

from __future__ import annotations

import re

from sam.memory.sanitization import looks_like_secret
from sam.tts.errors import TTSSecretDetectedError, TTSValidationError
from sam.tts.models import MAX_TTS_TEXT_BYTES, MAX_TTS_TEXT_CHARS, TTSErrorCategory

# Everything below space except tab/newline/carriage return, plus DEL.
_BAD_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def validate_text(text: object) -> str:
    """Return ``text`` unchanged if acceptable, else raise (fail closed).

    Order: type -> size (cheap, before any scan) -> structure -> control
    characters -> blank -> secret detection. The text is never altered,
    truncated, or redacted; a secret-looking text is *withheld*.
    """

    if not isinstance(text, str):
        raise TTSValidationError("text must be a string")
    if len(text) > MAX_TTS_TEXT_CHARS:
        raise TTSValidationError(
            "text exceeds the size limit", category=TTSErrorCategory.TEXT_TOO_LARGE
        )
    try:
        encoded = text.encode("utf-8")  # strict: lone surrogates fail
    except UnicodeEncodeError:
        raise TTSValidationError("text is not valid Unicode") from None
    if len(encoded) > MAX_TTS_TEXT_BYTES:
        raise TTSValidationError(
            "text exceeds the size limit", category=TTSErrorCategory.TEXT_TOO_LARGE
        )
    if _BAD_CONTROL.search(text):
        raise TTSValidationError("text contains control characters")
    if not text.strip():
        raise TTSValidationError("text is empty")
    if looks_like_secret(text):
        # Never redacted-and-sent: the whole request is withheld, and the
        # value is not echoed anywhere.
        raise TTSSecretDetectedError("text looks like it contains a secret")
    return text


__all__ = ["validate_text"]
