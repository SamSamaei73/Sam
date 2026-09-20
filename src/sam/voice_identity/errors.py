"""Errors for the voice-identity layer.

Every message is generic: no biometric value, similarity score, path,
provider text, or transcript ever appears in an exception.
"""

from __future__ import annotations


class VoiceIdentityError(Exception):
    """Base class. ``reason_code`` is a short, safe, machine-readable tag."""

    reason_code = "voice_identity_error"

    def __init__(self, message: str = "voice identity operation failed") -> None:
        super().__init__(message)


class EnrollmentError(VoiceIdentityError):
    reason_code = "enrollment_error"


class ProfileStoreError(VoiceIdentityError):
    reason_code = "profile_store_error"


class ChallengeError(VoiceIdentityError):
    reason_code = "challenge_error"


class GuestModeError(VoiceIdentityError):
    reason_code = "guest_mode_error"


class AuthorizationError(VoiceIdentityError):
    """A step-up grant or owner proof was missing, forged, stale or reused."""

    reason_code = "authorization_error"


class ModelSetupError(VoiceIdentityError):
    reason_code = "model_setup_error"


__all__ = [
    "AuthorizationError",
    "ChallengeError",
    "EnrollmentError",
    "GuestModeError",
    "ModelSetupError",
    "ProfileStoreError",
    "VoiceIdentityError",
]
