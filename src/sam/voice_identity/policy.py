"""Sam-owned policy constants for owner voice identity.

Voice identity is an authentication signal, not authorization. Nothing here
maps a speaker match onto a PermissionEngine decision, a confirmation, or a
CRITICAL authentication. Speaker verification is probabilistic: a match means
"likely owner", never "identity proven".

Thresholds are Sam's, not a model library's default. They are bounded so a
misconfiguration cannot silently make verification trivially permissive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

# ---- enrollment ------------------------------------------------------------
MIN_ENROLLMENT_SAMPLES = 3
MAX_ENROLLMENT_SAMPLES = 5
MIN_SAMPLE_SECONDS = 2.0
MIN_VERIFICATION_SECONDS = 1.0
MIN_RMS_PCM16 = 150.0  # of 32768: rejects near-silence
MAX_CLIPPED_FRACTION = 0.2  # rejects heavily clipped audio
DUPLICATE_SIMILARITY = 0.9995  # a re-submitted identical sample
MIN_SELF_CONSISTENCY = 0.30  # samples must plausibly be one speaker
ENROLLMENT_SESSION_TTL = timedelta(minutes=15)

# ---- verification threshold ------------------------------------------------
THRESHOLD_MIN = 0.30
THRESHOLD_MAX = 0.90
DEFAULT_THRESHOLD = 0.50
MAX_EMBEDDING_DIMENSION = 1024
MIN_EMBEDDING_DIMENSION = 8

# ---- short-lived authority objects ----------------------------------------
STEP_UP_GRANT_TTL = timedelta(seconds=60)
CHALLENGE_TTL = timedelta(seconds=60)
OWNER_PROOF_TTL = timedelta(seconds=60)
MAX_TRACKED_CHALLENGES = 64
MAX_TRACKED_PROOFS = 256

# ---- guest mode ------------------------------------------------------------
GUEST_MIN_MINUTES = 1
GUEST_DEFAULT_MINUTES = 15
GUEST_MAX_MINUTES = 30  # hard ceiling: there is no indefinite guest mode
MAX_GUEST_CONTEXT_TURNS = 40


class SpeakerResult(StrEnum):
    OWNER_VERIFIED = "owner_verified"
    OWNER_NOT_VERIFIED = "owner_not_verified"
    UNKNOWN = "unknown"
    NOT_ENROLLED = "not_enrolled"
    INSUFFICIENT_AUDIO = "insufficient_audio"
    VERIFICATION_ERROR = "verification_error"


class SpeakerClass(StrEnum):
    OWNER = "owner"
    GUEST = "guest"
    BLOCKED = "blocked"


class IdentityMode(StrEnum):
    OWNER_ONLY = "owner_only"
    GUEST_MODE = "guest_mode"


@dataclass(frozen=True)
class ThresholdPolicy:
    """The cosine-similarity threshold Sam requires for ``OWNER_VERIFIED``.

    Trusted configuration only: no request, UI field, LLM output, or document
    can set it.
    """

    threshold: float = DEFAULT_THRESHOLD

    def __post_init__(self) -> None:
        value = self.threshold
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or not THRESHOLD_MIN <= value <= THRESHOLD_MAX
        ):
            raise ValueError("verification threshold is outside the safe range")

    def accepts(self, score: float) -> bool:
        return math.isfinite(score) and score >= self.threshold


def classify_speaker(result: SpeakerResult, *, guest_active: bool) -> SpeakerClass:
    """Fail-closed classification.

    * ``OWNER_VERIFIED`` -> owner.
    * ``OWNER_NOT_VERIFIED`` -> guest **only** while an owner-authorized Guest
      Mode is active, otherwise blocked.
    * everything else -> blocked, even in Guest Mode, because the speaker could
      not be classified. That includes ``NOT_ENROLLED``: having no biometric
      profile NEVER implies owner identity. (Enrollment itself is gated by the
      Desktop step-up secret, not by a voice match.)
    """

    if result is SpeakerResult.OWNER_VERIFIED:
        return SpeakerClass.OWNER
    if result is SpeakerResult.OWNER_NOT_VERIFIED and guest_active:
        return SpeakerClass.GUEST
    return SpeakerClass.BLOCKED


__all__ = [
    "CHALLENGE_TTL",
    "DEFAULT_THRESHOLD",
    "DUPLICATE_SIMILARITY",
    "ENROLLMENT_SESSION_TTL",
    "GUEST_DEFAULT_MINUTES",
    "GUEST_MAX_MINUTES",
    "GUEST_MIN_MINUTES",
    "MAX_CLIPPED_FRACTION",
    "MAX_EMBEDDING_DIMENSION",
    "MAX_ENROLLMENT_SAMPLES",
    "MAX_GUEST_CONTEXT_TURNS",
    "MAX_TRACKED_CHALLENGES",
    "MAX_TRACKED_PROOFS",
    "MIN_EMBEDDING_DIMENSION",
    "MIN_ENROLLMENT_SAMPLES",
    "MIN_RMS_PCM16",
    "MIN_SAMPLE_SECONDS",
    "MIN_SELF_CONSISTENCY",
    "MIN_VERIFICATION_SECONDS",
    "OWNER_PROOF_TTL",
    "STEP_UP_GRANT_TTL",
    "THRESHOLD_MAX",
    "THRESHOLD_MIN",
    "IdentityMode",
    "SpeakerClass",
    "SpeakerResult",
    "ThresholdPolicy",
    "classify_speaker",
]
