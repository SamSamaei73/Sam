"""Typed, immutable domain models for the voice gateway (Phase 9).

Hard invariants encoded here:

* ``Voice input != authorization`` — no model in this module has a field
  that can carry an allow/approval. ``VoiceProcessingResult.
  grants_authorization`` exists only to state, structurally, that it is
  always ``False``.
* A transcript is untrusted *user input*, never an instruction to Sam's
  policy: ``transcript_untrusted_input`` is always ``True``.
* Voice identity is an authentication *signal*, not an authorization
  decision (``VoiceIdentitySignal``).
* Raw audio and transcript text are excluded from every ``repr`` and from
  every audit model — there is simply no field to put them in.

Every security-critical limit is a module constant here, in one place,
following the convention of the earlier phases.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sam.permissions.models import (
    PermissionAction,
    PermissionResource,
    Principal,
    RiskLevel,
)

# --------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------- #

MIN_SAMPLE_RATE = 8_000
MAX_SAMPLE_RATE = 48_000
MAX_CHANNELS = 2
SUPPORTED_SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM only
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_AUDIO_DURATION_SECONDS = 30.0
MAX_AUDIO_METADATA_LENGTH = 200
MAX_TRANSCRIPT_LENGTH = 10_000
MAX_VOICE_SESSION_UTTERANCES = 50
MAX_OPEN_VOICE_SESSIONS = 1_000
MAX_VOICE_PROVIDER_TIMEOUT_SECONDS = 30.0
DEFAULT_VOICE_PROVIDER_TIMEOUT_SECONDS = 10.0
MAX_IDENTITY_SIGNAL_AGE_SECONDS = 60.0
MAX_TRACKED_IDENTITY_SIGNALS = 10_000
MAX_WAV_CHUNKS = 16
MAX_VOICE_ID_LENGTH = 100
MAX_VOICE_REASON_LENGTH = 500
MAX_LANGUAGE_LENGTH = 16
MAX_PROVIDER_ID_LENGTH = 64

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,2}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Labels are metadata that may reach logs/UIs: reject *every* C0 control
# character, including tab and newline (log injection), not just the exotic ones.
_LABEL_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f]")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def new_id() -> str:
    return uuid4().hex


def utc_now() -> datetime:
    return datetime.now(UTC)


def sanitize_display_text(value: str | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()
    return cleaned[:max_length] if cleaned else None


# --------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------- #


class AudioFormat(StrEnum):
    """The only two accepted encodings. No codec support of any kind."""

    WAV_PCM16 = "wav_pcm16"  # RIFF/WAVE container holding 16-bit PCM
    RAW_PCM16LE = "raw_pcm16le"  # headerless 16-bit little-endian PCM


class VoiceIdentityStatus(StrEnum):
    VERIFIED = "verified"
    NOT_VERIFIED = "not_verified"
    UNKNOWN = "unknown"
    NOT_CHECKED = "not_checked"


class VoiceOperation(StrEnum):
    START_SESSION = "start_session"
    PROCESS_UTTERANCE = "process_utterance"
    END_SESSION = "end_session"


class VoiceProcessingStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    DENIED = "denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    FAILED = "failed"


class VoiceErrorCategory(StrEnum):
    UNSUPPORTED_AUDIO = "unsupported_audio"
    MALFORMED_AUDIO = "malformed_audio"
    AUDIO_TOO_LARGE = "audio_too_large"
    AUDIO_DURATION = "audio_duration"
    POLICY_ERROR = "policy_error"
    PERMISSION_DENIED = "permission_denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_INVALID = "confirmation_invalid"
    SESSION_ERROR = "session_error"
    SESSION_LIMIT = "session_limit"
    DUPLICATE_UTTERANCE = "duplicate_utterance"
    IDENTITY_SIGNAL_INVALID = "identity_signal_invalid"
    TRANSCRIPTION_ERROR = "transcription_error"
    TRANSCRIPTION_TIMEOUT = "transcription_timeout"
    INVALID_TRANSCRIPT = "invalid_transcript"
    EMPTY_TRANSCRIPT = "empty_transcript"
    TRANSCRIPT_TOO_LARGE = "transcript_too_large"
    SECRET_DETECTED = "secret_detected"
    AGENT_ERROR = "agent_error"
    INTERNAL_ERROR = "internal_error"


class VoiceForwardingDecision(StrEnum):
    """Whether a transcript may be forwarded to AgentCore / an LLM provider.

    ELIGIBLE: a validated transcript with no secret-like content.
    WITHHELD_SECRET_DETECTED: the transcript looked like it contained a
        secret. It is *not* redacted-and-sent: it is withheld entirely and
        never reaches AgentCore or any LLM provider.
    NOT_APPLICABLE: there is no transcript (denied, rejected, failed, ...).
    """

    ELIGIBLE = "eligible"
    WITHHELD_SECRET_DETECTED = "withheld_secret_detected"
    NOT_APPLICABLE = "not_applicable"


class PermissionOutcomeSummary(StrEnum):
    ALLOW = "allow"
    CONFIRM_REQUIRED = "confirm_required"
    DENY = "deny"


# --------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------- #


class AudioInput(BaseModel):
    """Untrusted, caller-supplied audio. ``content`` is excluded from
    ``repr`` and no size cap is enforced *by the model* — the validator
    checks the size first (before parsing) so an oversized input becomes a
    categorized rejection, not an opaque construction error.

    ``sample_rate``/``channels`` are required for headerless PCM and must be
    absent for WAV (the container is the only source of truth). ``label`` is
    display-only metadata: it is never used to choose a parser, and control
    characters or oversize values are rejected outright.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: bytes = Field(repr=False)
    declared_format: AudioFormat
    sample_rate: int | None = None
    channels: int | None = None
    label: str | None = None

    @field_validator("label")
    @classmethod
    def _label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > MAX_AUDIO_METADATA_LENGTH or _LABEL_FORBIDDEN.search(value):
            raise ValueError("audio label is invalid")
        return value


class AudioMetadata(BaseModel):
    """What the validator actually measured — never what the caller claimed."""

    model_config = ConfigDict(frozen=True)

    audio_format: AudioFormat
    sample_rate: int = Field(ge=MIN_SAMPLE_RATE, le=MAX_SAMPLE_RATE)
    channels: int = Field(ge=1, le=MAX_CHANNELS)
    sample_width_bytes: int = Field(
        ge=SUPPORTED_SAMPLE_WIDTH_BYTES, le=SUPPORTED_SAMPLE_WIDTH_BYTES
    )
    frame_count: int = Field(ge=1)
    duration_seconds: float = Field(gt=0, le=MAX_AUDIO_DURATION_SECONDS)
    byte_size: int = Field(ge=1, le=MAX_AUDIO_BYTES)
    digest_sha256: str = Field(pattern=_SHA256_RE.pattern)


class ValidatedAudio(BaseModel):
    """Audio that passed structural validation. ``pcm`` is excluded from
    ``repr``. Never persisted by the voice layer."""

    model_config = ConfigDict(frozen=True)

    metadata: AudioMetadata
    pcm: bytes = Field(repr=False)


# --------------------------------------------------------------------- #
# Transcription
# --------------------------------------------------------------------- #


class TranscriptionRequest(BaseModel):
    """What a provider is given. It deliberately carries **no timeout**: the
    deadline is passed separately, by the gateway, as ``timeout_seconds`` to
    ``TranscriptionProvider.transcribe`` — so no request field, and nothing a
    provider returns, can influence it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    utterance_id: str
    audio: ValidatedAudio
    language_hint: str | None = Field(default=None, max_length=MAX_LANGUAGE_LENGTH)


class TranscriptionResult(BaseModel):
    """A *validated* provider result. ``reported_confidence`` is the
    provider's own untrusted claim: informational only, never consulted by
    any decision. ``text`` is excluded from ``repr``."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(repr=False, min_length=1, max_length=MAX_TRANSCRIPT_LENGTH)
    provider_id: str = Field(
        pattern=_PROVIDER_ID_RE.pattern, max_length=MAX_PROVIDER_ID_LENGTH
    )
    language: str | None = Field(default=None, max_length=MAX_LANGUAGE_LENGTH)
    reported_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("language")
    @classmethod
    def _language(cls, value: str | None) -> str | None:
        if value is not None and not _LANGUAGE_RE.match(value):
            raise ValueError("language tag is invalid")
        return value

    @field_validator("reported_confidence")
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("confidence must be finite")
        return value


# --------------------------------------------------------------------- #
# Identity (an authentication signal, never an authorization decision)
# --------------------------------------------------------------------- #


class VoiceIdentitySignal(BaseModel):
    """A transient, context-bound, signed identity signal.

    It is bound to one session, one utterance, one audio digest and one
    provider, is short-lived, and can be consumed once. It carries no
    biometric material. It is *evidence about who spoke* and nothing else:
    there is no field here — and no code path anywhere in ``sam.voice`` —
    that turns it into an allow, an approved confirmation, or a permission.
    """

    model_config = ConfigDict(frozen=True)

    signal_id: str = Field(min_length=1, max_length=MAX_VOICE_ID_LENGTH)
    status: VoiceIdentityStatus
    session_id: str = Field(min_length=1, max_length=MAX_VOICE_ID_LENGTH)
    utterance_id: str = Field(min_length=1, max_length=MAX_VOICE_ID_LENGTH)
    audio_digest: str = Field(pattern=_SHA256_RE.pattern)
    provider_id: str = Field(
        pattern=_PROVIDER_ID_RE.pattern, max_length=MAX_PROVIDER_ID_LENGTH
    )
    issued_at: datetime
    reported_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    signature: str = Field(repr=False, pattern=_SHA256_RE.pattern)

    @field_validator("issued_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @field_validator("reported_confidence")
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("confidence must be finite")
        return value


# --------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------- #


class _RequestBase(BaseModel):
    # extra="forbid": a request that tries to smuggle a permission, scope,
    # risk, provider, or confirmation field is rejected, not silently ignored.
    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: Principal
    reason: str | None = Field(default=None, max_length=MAX_VOICE_REASON_LENGTH)

    @field_validator("reason", mode="before")
    @classmethod
    def _clean_reason(cls, value: str | None) -> str | None:
        return sanitize_display_text(value, max_length=MAX_VOICE_REASON_LENGTH)


class StartSessionRequest(_RequestBase):
    pass


class EndSessionRequest(_RequestBase):
    session_id: str = Field(
        min_length=1, max_length=MAX_VOICE_ID_LENGTH, pattern=_ID_RE.pattern
    )


class VoiceProcessingRequest(_RequestBase):
    """One explicit voice request. Nothing here can carry a permission, a
    confirmation, a scope, a risk level, or a provider choice."""

    session_id: str = Field(
        min_length=1, max_length=MAX_VOICE_ID_LENGTH, pattern=_ID_RE.pattern
    )
    utterance_id: str = Field(
        default_factory=new_id,
        min_length=1,
        max_length=MAX_VOICE_ID_LENGTH,
        pattern=_ID_RE.pattern,
    )
    audio: AudioInput
    identity_signal: VoiceIdentitySignal | None = None


# --------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------- #


class VoiceSessionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation: VoiceOperation
    principal: Principal
    status: VoiceProcessingStatus
    session_id: str | None = None
    permission_outcome: PermissionOutcomeSummary | None = None
    error_category: VoiceErrorCategory | None = None
    confirmation_id: str | None = None
    created_at: datetime


class VoiceProcessingResult(BaseModel):
    """The normalized result of one utterance, with provenance.

    ``transcript`` is untrusted user input (never an instruction to policy)
    and is excluded from ``repr``. ``grants_authorization`` and
    ``transcript_untrusted_input`` are structurally pinned: ``False`` and
    ``True`` respectively, always.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str
    utterance_id: str
    principal: Principal
    status: VoiceProcessingStatus
    permission_outcome: PermissionOutcomeSummary | None = None
    error_category: VoiceErrorCategory | None = None
    confirmation_id: str | None = None
    transcript: str | None = Field(default=None, repr=False)
    transcript_truncated: bool = False
    transcript_untrusted_input: bool = True
    transcript_secret_like: bool = False
    forwarding: VoiceForwardingDecision = VoiceForwardingDecision.NOT_APPLICABLE
    provider_id: str | None = None
    language: str | None = None
    identity_status: VoiceIdentityStatus = VoiceIdentityStatus.NOT_CHECKED
    identity_checked: bool = False
    identity_reason_code: str | None = Field(default=None, max_length=64)
    audio: AudioMetadata | None = None
    transcription_attempted: bool = False
    processed_at: datetime
    duration_ms: int = Field(default=0, ge=0)
    grants_authorization: bool = False

    @field_validator("grants_authorization")
    @classmethod
    def _never_authorizes(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("a voice result never grants authorization")
        return value

    @field_validator("transcript_untrusted_input")
    @classmethod
    def _always_untrusted(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("a transcript is always untrusted user input")
        return value

    @field_validator("processed_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _shape(self) -> Self:
        succeeded = self.status is VoiceProcessingStatus.SUCCEEDED
        if succeeded != (self.transcript is not None):
            raise ValueError("only a SUCCEEDED result carries a transcript")
        if succeeded and (self.provider_id is None or self.audio is None):
            raise ValueError("a SUCCEEDED result needs provider and audio provenance")
        if (
            not succeeded
            and self.status is not VoiceProcessingStatus.CONFIRMATION_REQUIRED
            and self.error_category is None
        ):
            raise ValueError("a non-successful result needs an error category")
        withheld = self.forwarding is VoiceForwardingDecision.WITHHELD_SECRET_DETECTED
        if succeeded and (
            self.forwarding is not VoiceForwardingDecision.ELIGIBLE
            or self.transcript_secret_like
        ):
            raise ValueError("a SUCCEEDED result must be forwarding-eligible")
        if withheld and (
            self.status is not VoiceProcessingStatus.FAILED
            or self.error_category is not VoiceErrorCategory.SECRET_DETECTED
            or not self.transcript_secret_like
        ):
            raise ValueError("a withheld result must be a secret_detected failure")
        if (
            not succeeded
            and not withheld
            and (
                self.forwarding is not VoiceForwardingDecision.NOT_APPLICABLE
                or self.transcript_secret_like
            )
        ):
            raise ValueError("only a withheld result may flag a secret")
        if not self.identity_checked and self.identity_status is not (
            VoiceIdentityStatus.NOT_CHECKED
        ):
            raise ValueError("identity status requires identity_checked")
        return self


class VoiceAuditEvent(BaseModel):
    """One immutable, content-free record. There is no field capable of
    holding audio, transcript text, spoken content, or biometric material."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=MAX_VOICE_ID_LENGTH)
    occurred_at: datetime
    operation: VoiceOperation
    principal: Principal
    session_id: str | None = Field(default=None, max_length=MAX_VOICE_ID_LENGTH)
    utterance_id: str | None = Field(default=None, max_length=MAX_VOICE_ID_LENGTH)
    permission_action: PermissionAction | None = None
    permission_resource: PermissionResource | None = None
    risk: RiskLevel | None = None
    authorization_outcome: PermissionOutcomeSummary | None = None
    status: VoiceProcessingStatus
    error_category: VoiceErrorCategory | None = None
    audio_format: AudioFormat | None = None
    audio_bytes: int | None = Field(default=None, ge=0)
    audio_duration_seconds: float | None = Field(default=None, ge=0)
    provider_id: str | None = Field(default=None, max_length=MAX_PROVIDER_ID_LENGTH)
    identity_status: VoiceIdentityStatus | None = None
    transcription_attempted: bool = False
    duration_ms: int = Field(default=0, ge=0)

    @field_validator("occurred_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("audit timestamps must be timezone-aware")
        return value


__all__ = [
    "DEFAULT_VOICE_PROVIDER_TIMEOUT_SECONDS",
    "MAX_AUDIO_BYTES",
    "MAX_AUDIO_DURATION_SECONDS",
    "MAX_AUDIO_METADATA_LENGTH",
    "MAX_CHANNELS",
    "MAX_IDENTITY_SIGNAL_AGE_SECONDS",
    "MAX_OPEN_VOICE_SESSIONS",
    "MAX_SAMPLE_RATE",
    "MAX_TRACKED_IDENTITY_SIGNALS",
    "MAX_TRANSCRIPT_LENGTH",
    "MAX_VOICE_PROVIDER_TIMEOUT_SECONDS",
    "MAX_VOICE_SESSION_UTTERANCES",
    "MAX_WAV_CHUNKS",
    "MIN_SAMPLE_RATE",
    "SUPPORTED_SAMPLE_WIDTH_BYTES",
    "AudioFormat",
    "AudioInput",
    "AudioMetadata",
    "EndSessionRequest",
    "PermissionOutcomeSummary",
    "StartSessionRequest",
    "TranscriptionRequest",
    "TranscriptionResult",
    "ValidatedAudio",
    "VoiceAuditEvent",
    "VoiceErrorCategory",
    "VoiceForwardingDecision",
    "VoiceIdentitySignal",
    "VoiceIdentityStatus",
    "VoiceOperation",
    "VoiceProcessingRequest",
    "VoiceProcessingResult",
    "VoiceProcessingStatus",
    "VoiceSessionResult",
    "new_id",
    "sanitize_display_text",
    "utc_now",
]
