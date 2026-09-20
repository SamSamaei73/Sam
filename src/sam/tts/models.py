"""Typed, immutable domain models for the speech-synthesis layer (Phase 10).

Invariants encoded here:

* ``Text != provider configuration``. ``SynthesisRequest`` has exactly the
  fields ``principal``, ``text``, ``trusted_voice_profile`` and
  ``request_id`` and forbids everything else, so an LLM-facing request cannot
  carry an API key, endpoint, model, voice reference, timeout, risk, or
  permission.
* The provider voice reference and model live only in a
  ``TrustedVoiceProfile`` chosen by Sam's configuration.
* ``TTS output != proof of action`` and ``Fish != authorization``: nothing in
  a result can carry an allow/approval (``grants_authorization`` is pinned
  ``False``).
* Request text and generated audio are excluded from every ``repr``, and no
  audit model has a field that could hold either.

Every security-critical limit is a module constant here, in one place.
"""

from __future__ import annotations

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

MAX_TTS_TEXT_CHARS = 5_000
MAX_TTS_TEXT_BYTES = 20_000  # UTF-8
MAX_TTS_AUDIO_BYTES = 10 * 1024 * 1024
DEFAULT_TTS_TIMEOUT_SECONDS = 30.0
MAX_TTS_TIMEOUT_SECONDS = 60.0
MAX_TTS_PROFILE_ID_LENGTH = 64
MAX_TTS_PROVIDER_ID_LENGTH = 64
MAX_TTS_REFERENCE_LENGTH = 64
MAX_TTS_MODEL_LENGTH = 64
MAX_TTS_REQUEST_ID_LENGTH = 100
MAX_TTS_PROFILES = 64
MAX_TRACKED_TTS_REQUEST_IDS = 10_000
MAX_TTS_CREDENTIAL_LENGTH = 4_096
MAX_TTS_CONTENT_TYPE_LENGTH = 100

_PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_CREDENTIAL_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def new_id() -> str:
    return uuid4().hex


def utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------- #


class TTSAudioFormat(StrEnum):
    """Output encoding. Phase 10 requests exactly one: MP3 (Fish's documented
    default), validated by signature. No other format is accepted."""

    MP3 = "mp3"


class TTSStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    DENIED = "denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    FAILED = "failed"


class TTSErrorCategory(StrEnum):
    TEXT_INVALID = "text_invalid"
    TEXT_TOO_LARGE = "text_too_large"
    SECRET_DETECTED = "secret_detected"
    UNKNOWN_PROFILE = "unknown_profile"
    PROFILE_DISABLED = "profile_disabled"
    POLICY_ERROR = "policy_error"
    PERMISSION_DENIED = "permission_denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_INVALID = "confirmation_invalid"
    DUPLICATE_REQUEST = "duplicate_request"
    CREDENTIAL_ERROR = "credential_error"
    PROVIDER_ERROR = "provider_error"
    AUTHENTICATION_ERROR = "authentication_error"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    OUTPUT_TOO_LARGE = "output_too_large"
    INVALID_AUDIO = "invalid_audio"
    INTERNAL_ERROR = "internal_error"


class PermissionOutcomeSummary(StrEnum):
    ALLOW = "allow"
    CONFIRM_REQUIRED = "confirm_required"
    DENY = "deny"


# --------------------------------------------------------------------- #
# Trusted voice profile
# --------------------------------------------------------------------- #


class TrustedVoiceProfile(BaseModel):
    """Sam's trusted mapping from a local profile id to a provider voice.

    Constructed only by Sam's own configuration code. The LLM, a transcript,
    a document, an MCP result and a provider response can never create,
    edit, or select anything but a *profile id* out of the fixed catalog.
    There is no field for reference audio, a voice sample, or any cloning
    material — only an already-approved provider voice id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = Field(
        min_length=1,
        max_length=MAX_TTS_PROFILE_ID_LENGTH,
        pattern=_PROFILE_ID_RE.pattern,
    )
    provider_id: str = Field(
        min_length=1,
        max_length=MAX_TTS_PROVIDER_ID_LENGTH,
        pattern=_PROVIDER_ID_RE.pattern,
    )
    provider_voice_reference: str = Field(
        min_length=1, max_length=MAX_TTS_REFERENCE_LENGTH, pattern=_REFERENCE_RE.pattern
    )
    provider_model: str = Field(
        min_length=1, max_length=MAX_TTS_MODEL_LENGTH, pattern=_MODEL_RE.pattern
    )
    output_format: TTSAudioFormat = TTSAudioFormat.MP3
    enabled: bool = True


# --------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------- #


class SynthesisRequest(BaseModel):
    """The only request shape the gateway accepts.

    ``extra="forbid"``: a request that tries to carry ``api_key``,
    ``endpoint``, ``reference_id``, ``model``, ``timeout``, ``temperature``,
    ``risk``, ``permission`` or ``authorization`` is rejected loudly. The text
    is length/size-checked by the gateway (so oversize becomes a categorized
    result, not an opaque construction error) and excluded from ``repr``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: Principal
    text: str = Field(repr=False, exclude=True)
    trusted_voice_profile: str = Field(
        min_length=1,
        max_length=MAX_TTS_PROFILE_ID_LENGTH,
        pattern=_PROFILE_ID_RE.pattern,
    )
    request_id: str = Field(
        default_factory=new_id,
        min_length=1,
        max_length=MAX_TTS_REQUEST_ID_LENGTH,
        pattern=_REQUEST_ID_RE.pattern,
    )


class ProviderSynthesisRequest(BaseModel):
    """What a provider is handed after every gate has passed. Built by the
    gateway from the *trusted profile*, never from request fields. Carries no
    credential and no timeout (both are separate, Sam-controlled inputs)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    synthesis_id: str = Field(min_length=1, max_length=MAX_TTS_REQUEST_ID_LENGTH)
    text: str = Field(repr=False, exclude=True)
    voice_reference: str = Field(max_length=MAX_TTS_REFERENCE_LENGTH)
    model: str = Field(max_length=MAX_TTS_MODEL_LENGTH)
    output_format: TTSAudioFormat


class ProviderSynthesisResult(BaseModel):
    """A raw provider result. Untrusted: the gateway validates the bytes and
    ignores everything else (``content_type`` is advisory only). Deliberately
    has no digest, voice, model, permission or metadata field."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    audio_bytes: bytes = Field(repr=False, exclude=True)
    content_type: str | None = Field(
        default=None, max_length=MAX_TTS_CONTENT_TYPE_LENGTH
    )


# --------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------- #


class SynthesizedAudio(BaseModel):
    """Validated generated speech, held in memory for the immediate caller
    only. ``audio_bytes`` is excluded from ``repr`` *and from every
    serialization* (``model_dump``/``model_dump_json``), so raw audio can never
    reach a log or a JSON body by accident; read it explicitly from the
    attribute. Nothing here is ever written to disk, audited, or sent to
    Memory or Knowledge."""

    model_config = ConfigDict(frozen=True)

    synthesis_id: str
    provider_id: str
    trusted_profile_id: str
    format: TTSAudioFormat
    byte_length: int = Field(ge=1, le=MAX_TTS_AUDIO_BYTES)
    sha256: str = Field(pattern=_SHA256_RE.pattern)
    audio_bytes: bytes = Field(repr=False, exclude=True)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value


class SynthesisResult(BaseModel):
    """What ``TTSGateway.synthesize`` always returns."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    synthesis_id: str
    principal: Principal
    status: TTSStatus
    permission_outcome: PermissionOutcomeSummary | None = None
    error_category: TTSErrorCategory | None = None
    confirmation_id: str | None = None
    audio: SynthesizedAudio | None = None
    provider_call_attempted: bool = False
    duration_ms: int = Field(default=0, ge=0)
    created_at: datetime
    grants_authorization: bool = False

    @field_validator("grants_authorization")
    @classmethod
    def _never_authorizes(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("synthesized speech never grants authorization")
        return value

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _shape(self) -> Self:
        succeeded = self.status is TTSStatus.SUCCEEDED
        if succeeded != (self.audio is not None):
            raise ValueError("only a SUCCEEDED result carries audio")
        if (
            not succeeded
            and self.status is not TTSStatus.CONFIRMATION_REQUIRED
            and self.error_category is None
        ):
            raise ValueError("a non-successful result needs an error category")
        if succeeded and self.error_category is not None:
            raise ValueError("a successful result must not carry an error category")
        return self


class TTSAuditEvent(BaseModel):
    """One immutable, content-free record. There is no field capable of
    holding request text, generated audio, an API key, an Authorization
    header, or a provider error body."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=100)
    occurred_at: datetime
    request_id: str
    synthesis_id: str
    principal: Principal
    provider_id: str | None = Field(default=None, max_length=MAX_TTS_PROVIDER_ID_LENGTH)
    trusted_profile_id: str | None = Field(
        default=None, max_length=MAX_TTS_PROFILE_ID_LENGTH
    )
    permission_action: PermissionAction | None = None
    permission_resource: PermissionResource | None = None
    risk: RiskLevel | None = None
    scope: str | None = Field(default=None, max_length=300)
    authorization_outcome: PermissionOutcomeSummary | None = None
    status: TTSStatus
    error_category: TTSErrorCategory | None = None
    text_length: int | None = Field(default=None, ge=0)
    output_size: int | None = Field(default=None, ge=0)
    provider_call_attempted: bool = False
    duration_ms: int = Field(default=0, ge=0)

    @field_validator("occurred_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("audit timestamps must be timezone-aware")
        return value


__all__ = [
    "DEFAULT_TTS_TIMEOUT_SECONDS",
    "MAX_TRACKED_TTS_REQUEST_IDS",
    "MAX_TTS_AUDIO_BYTES",
    "MAX_TTS_CREDENTIAL_LENGTH",
    "MAX_TTS_PROFILE_ID_LENGTH",
    "MAX_TTS_PROFILES",
    "MAX_TTS_TEXT_BYTES",
    "MAX_TTS_TEXT_CHARS",
    "MAX_TTS_TIMEOUT_SECONDS",
    "PermissionOutcomeSummary",
    "ProviderSynthesisRequest",
    "ProviderSynthesisResult",
    "SynthesisRequest",
    "SynthesisResult",
    "SynthesizedAudio",
    "TTSAudioFormat",
    "TTSAuditEvent",
    "TTSErrorCategory",
    "TTSStatus",
    "TrustedVoiceProfile",
    "new_id",
    "utc_now",
]
