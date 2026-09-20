"""Typed errors for the voice gateway.

Every error carries a closed ``category`` and a fixed, generic message. No
message ever contains raw audio, transcript text, spoken content, provider
exception text, or identity material — underlying exceptions are collapsed
with ``raise ... from None`` at every boundary.
"""

from __future__ import annotations

from sam.voice.models import VoiceErrorCategory


class VoiceError(Exception):
    category: VoiceErrorCategory = VoiceErrorCategory.INTERNAL_ERROR


class UnsupportedAudioError(VoiceError):
    category = VoiceErrorCategory.UNSUPPORTED_AUDIO


class MalformedAudioError(VoiceError):
    category = VoiceErrorCategory.MALFORMED_AUDIO


class AudioTooLargeError(VoiceError):
    category = VoiceErrorCategory.AUDIO_TOO_LARGE


class AudioDurationError(VoiceError):
    category = VoiceErrorCategory.AUDIO_DURATION


class VoicePolicyError(VoiceError):
    category = VoiceErrorCategory.POLICY_ERROR


class VoicePermissionError(VoiceError):
    category = VoiceErrorCategory.PERMISSION_DENIED


class TranscriptionError(VoiceError):
    category = VoiceErrorCategory.TRANSCRIPTION_ERROR


class TranscriptionTimeoutError(VoiceError):
    category = VoiceErrorCategory.TRANSCRIPTION_TIMEOUT


class InvalidTranscriptError(VoiceError):
    category = VoiceErrorCategory.INVALID_TRANSCRIPT

    def __init__(
        self,
        message: str = "transcript is invalid",
        *,
        category: VoiceErrorCategory = VoiceErrorCategory.INVALID_TRANSCRIPT,
    ) -> None:
        super().__init__(message)
        self.category = category


class VoiceIdentityError(VoiceError):
    category = VoiceErrorCategory.IDENTITY_SIGNAL_INVALID


class VoiceSessionError(VoiceError):
    category = VoiceErrorCategory.SESSION_ERROR


class DuplicateUtteranceError(VoiceSessionError):
    category = VoiceErrorCategory.DUPLICATE_UTTERANCE


class VoiceLimitError(VoiceError):
    category = VoiceErrorCategory.SESSION_LIMIT


__all__ = [
    "AudioDurationError",
    "AudioTooLargeError",
    "DuplicateUtteranceError",
    "InvalidTranscriptError",
    "MalformedAudioError",
    "TranscriptionError",
    "TranscriptionTimeoutError",
    "UnsupportedAudioError",
    "VoiceError",
    "VoiceIdentityError",
    "VoiceLimitError",
    "VoicePermissionError",
    "VoicePolicyError",
    "VoiceSessionError",
]
