"""Typed errors for the speech-synthesis layer.

Every error carries a closed ``category`` and a fixed, generic message. No
message ever contains the API key, request text, generated audio, a provider
response body, or provider exception text: underlying exceptions are
collapsed with ``raise ... from None`` at every boundary.
"""

from __future__ import annotations

from sam.tts.models import TTSErrorCategory


class TTSError(Exception):
    category: TTSErrorCategory = TTSErrorCategory.INTERNAL_ERROR


class TTSValidationError(TTSError):
    category = TTSErrorCategory.TEXT_INVALID

    def __init__(
        self,
        message: str = "text is invalid",
        *,
        category: TTSErrorCategory = TTSErrorCategory.TEXT_INVALID,
    ) -> None:
        super().__init__(message)
        self.category = category


class TTSPermissionError(TTSError):
    category = TTSErrorCategory.PERMISSION_DENIED


class TTSSecretDetectedError(TTSError):
    category = TTSErrorCategory.SECRET_DETECTED


class TTSProfileNotFoundError(TTSError):
    category = TTSErrorCategory.UNKNOWN_PROFILE


class TTSCredentialError(TTSError):
    category = TTSErrorCategory.CREDENTIAL_ERROR


class TTSProviderError(TTSError):
    category = TTSErrorCategory.PROVIDER_ERROR


class TTSAuthenticationError(TTSProviderError):
    category = TTSErrorCategory.AUTHENTICATION_ERROR


class TTSRateLimitError(TTSProviderError):
    category = TTSErrorCategory.RATE_LIMITED


class TTSTimeoutError(TTSProviderError):
    category = TTSErrorCategory.TIMEOUT


class TTSOutputTooLargeError(TTSProviderError):
    category = TTSErrorCategory.OUTPUT_TOO_LARGE


class TTSInvalidAudioError(TTSProviderError):
    category = TTSErrorCategory.INVALID_AUDIO


__all__ = [
    "TTSAuthenticationError",
    "TTSCredentialError",
    "TTSError",
    "TTSInvalidAudioError",
    "TTSOutputTooLargeError",
    "TTSPermissionError",
    "TTSProfileNotFoundError",
    "TTSProviderError",
    "TTSRateLimitError",
    "TTSSecretDetectedError",
    "TTSTimeoutError",
    "TTSValidationError",
]
