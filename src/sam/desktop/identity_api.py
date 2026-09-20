"""Owner voice identity, challenge and Guest Mode routes.

Every route here is authenticated by the bridge dependency, has typed
extra-forbid requests, and takes its identity from trusted backend state — never
from the request. Sensitive actions (enroll, re-enroll, delete profile, start
Guest Mode) require the backend-verified step-up secret; a voice match is never
a substitute. No route here grants a permission or answers a confirmation.
"""

from __future__ import annotations

import base64
import binascii
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from sam.desktop.identity import DesktopVoiceIdentity
from sam.desktop.models import (
    ChallengeResponse,
    EnrollBeginRequest,
    EnrollBeginResponse,
    EnrollProgressResponse,
    EnrollSampleRequest,
    EnrollSessionRequest,
    GuestInfo,
    GuestStartRequest,
    IdentityStatusResponse,
    OperationResult,
    ProfileDeleteRequest,
)
from sam.desktop.runtime import DesktopRuntime
from sam.desktop.security import bridge_runtime, owner_bridge_runtime
from sam.voice.audio import validate_audio
from sam.voice.models import AudioFormat, AudioInput
from sam.voice_identity.errors import (
    AuthorizationError,
    ChallengeError,
    EnrollmentError,
    GuestModeError,
)
from sam.voice_identity.policy import (
    MAX_ENROLLMENT_SAMPLES,
    MIN_ENROLLMENT_SAMPLES,
    SpeakerResult,
)

router = APIRouter(prefix="/desktop/v1", tags=["desktop-identity"])
_runtime = Depends(bridge_runtime)
_owner_runtime = Depends(owner_bridge_runtime)
STEP_UP_COOLDOWN = timedelta(minutes=5)

_MESSAGES = {
    "not_configured": "Owner voice identity isn't configured.",
    "step_up_required": "Your step-up secret is required.",
    "already_enrolled": "An owner voice profile already exists.",
    "not_enrolled": "There is no owner voice profile yet.",
    "session_invalid": "That enrollment session is no longer valid.",
    "audio_invalid": "That recording couldn't be used.",
    "audio_too_short": "That recording is too short. Speak for a few seconds.",
    "audio_too_quiet": "That recording is too quiet.",
    "audio_clipped": "That recording is distorted. Move back from the microphone.",
    "duplicate_sample": "That sample was already used. Say something different.",
    "too_few_samples": "More samples are needed.",
    "inconsistent_samples": "The samples didn't sound like one speaker.",
    "unstable_samples": "The samples were too different. Please try again.",
    "store_error": "The secure store couldn't be used. Nothing was saved.",
    "provider_error": "The speaker model couldn't process that.",
    "owner_verification_failed": "Owner verification failed.",
    "guest_active": "Guest Mode is already active. End it first.",
    "challenge_invalid": "That challenge is no longer valid.",
}


def _msg(code: str) -> str:
    return _MESSAGES.get(code, "The request couldn't be completed.")


def _identity(runtime: DesktopRuntime) -> DesktopVoiceIdentity | None:
    return runtime.identity


def _unavailable() -> OperationResult:
    return OperationResult(
        status="not_configured",
        reason_code="not_configured",
        message=_msg("not_configured"),
    )


def _decode(data: str) -> AudioInput | None:
    try:
        content = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not content:
        return None
    return AudioInput(content=content, declared_format=AudioFormat.WAV_PCM16)


def _step_up_or_raise(runtime: DesktopRuntime, key: str, supplied: str) -> None:
    verdict = runtime.check_step_up(key, supplied, cooldown=STEP_UP_COOLDOWN)
    if verdict != "ok":
        runtime.activity.add("permission", "Step-up refused", verdict)
        code = {
            "unavailable": "step_up_unavailable",
            "failed": "step_up_failed",
            "locked": "step_up_locked",
        }[verdict]
        raise HTTPException(status_code=403, detail={"code": code})


def _guest_info(identity: DesktopVoiceIdentity) -> GuestInfo:
    status = identity.guests.status()
    return GuestInfo(active=status.active, seconds_remaining=status.seconds_remaining)


@router.get("/voice/identity", response_model=IdentityStatusResponse)
def identity_status(runtime: DesktopRuntime = _runtime) -> IdentityStatusResponse:
    identity = _identity(runtime)
    if identity is None:
        return IdentityStatusResponse(available=False)
    enrolled = identity.enrollment.status().enrolled
    last = identity.last_speaker_result
    last_state: Literal["verified", "not_verified", "unknown"] | None = None
    if last == SpeakerResult.OWNER_VERIFIED.value:
        last_state = "verified"
    elif last == SpeakerResult.OWNER_NOT_VERIFIED.value:
        last_state = "not_verified"
    elif last is not None:
        last_state = "unknown"
    guest = _guest_info(identity)
    return IdentityStatusResponse(
        available=True,
        enrolled=enrolled,
        mode="guest_mode" if guest.active else "owner_only",
        guest=guest,
        last_verification=last_state,
        speaker_model="configured",
        local_stt="configured",
        persian_tts="configured"
        if any("fa" in lang for lang in runtime.speech_profile_languages.values())
        else "not_configured",
        samples_needed=MIN_ENROLLMENT_SAMPLES,
        samples_max=MAX_ENROLLMENT_SAMPLES,
    )


@router.post("/voice/identity/enroll/begin", response_model=EnrollBeginResponse)
def enroll_begin(
    payload: EnrollBeginRequest, runtime: DesktopRuntime = _owner_runtime
) -> EnrollBeginResponse:
    identity = _identity(runtime)
    if identity is None:
        return EnrollBeginResponse(**_unavailable().model_dump())
    purpose = "re_enroll" if payload.re_enroll else "enroll"
    _step_up_or_raise(runtime, f"voice:{purpose}", payload.step_up.get_secret_value())
    grant = identity.step_up.mint(purpose)
    try:
        session_id = identity.enrollment.begin(grant, re_enroll=payload.re_enroll)
    except (EnrollmentError, AuthorizationError):
        code = "already_enrolled" if not payload.re_enroll else "not_enrolled"
        return EnrollBeginResponse(
            status="rejected", reason_code=code, message=_msg(code)
        )
    runtime.activity.add("voice", "Owner enrollment started", "started")
    return EnrollBeginResponse(status="ok", session_id=session_id)


@router.post("/voice/identity/enroll/sample", response_model=EnrollProgressResponse)
def enroll_sample(
    payload: EnrollSampleRequest, runtime: DesktopRuntime = _owner_runtime
) -> EnrollProgressResponse:
    identity = _identity(runtime)
    if identity is None:
        return EnrollProgressResponse(**_unavailable().model_dump())
    audio = _decode(payload.audio_base64)
    if audio is None:
        return EnrollProgressResponse(
            status="rejected",
            reason_code="audio_invalid",
            message=_msg("audio_invalid"),
        )
    try:
        validated = validate_audio(audio)
        progress = identity.enrollment.add_sample(payload.session_id, validated)
    except EnrollmentError:
        return EnrollProgressResponse(
            status="rejected",
            reason_code="session_invalid",
            message=_msg("session_invalid"),
        )
    except Exception:
        return EnrollProgressResponse(
            status="rejected",
            reason_code="audio_invalid",
            message=_msg("audio_invalid"),
        )
    return EnrollProgressResponse(
        status="ok" if progress.accepted else "rejected",
        reason_code=None if progress.accepted else progress.reason_code,
        message=None if progress.accepted else _msg(progress.reason_code),
        accepted=progress.accepted,
        sample_count=progress.sample_count,
        samples_needed=progress.needed,
    )


@router.post("/voice/identity/enroll/complete", response_model=OperationResult)
def enroll_complete(
    payload: EnrollSessionRequest, runtime: DesktopRuntime = _owner_runtime
) -> OperationResult:
    identity = _identity(runtime)
    if identity is None:
        return _unavailable()
    try:
        outcome = identity.enrollment.complete(payload.session_id)
    except EnrollmentError:
        return OperationResult(
            status="rejected",
            reason_code="session_invalid",
            message=_msg("session_invalid"),
        )
    if outcome.completed:
        runtime.activity.add("voice", "Owner voice enrolled", "completed")
        return OperationResult(status="ok")
    return OperationResult(
        status="rejected",
        reason_code=outcome.reason_code,
        message=_msg(outcome.reason_code),
    )


@router.post("/voice/identity/enroll/cancel", response_model=OperationResult)
def enroll_cancel(
    payload: EnrollSessionRequest, runtime: DesktopRuntime = _owner_runtime
) -> OperationResult:
    identity = _identity(runtime)
    if identity is None:
        return _unavailable()
    identity.enrollment.cancel(payload.session_id)
    return OperationResult(status="ok")


@router.post("/voice/identity/delete", response_model=OperationResult)
def delete_profile(
    payload: ProfileDeleteRequest, runtime: DesktopRuntime = _owner_runtime
) -> OperationResult:
    identity = _identity(runtime)
    if identity is None:
        return _unavailable()
    _step_up_or_raise(
        runtime, "voice:delete_profile", payload.step_up.get_secret_value()
    )
    grant = identity.step_up.mint("delete_profile")
    try:
        identity.enrollment.delete_profile(grant)
    except (EnrollmentError, AuthorizationError):
        return OperationResult(
            status="failed", reason_code="store_error", message=_msg("store_error")
        )
    identity.guests.revoke()  # no owner profile: no guest sessions either
    runtime.activity.add("voice", "Owner voice profile removed", "removed")
    return OperationResult(status="ok")


@router.post("/voice/guest/challenge", response_model=ChallengeResponse)
def guest_challenge(runtime: DesktopRuntime = _runtime) -> ChallengeResponse:
    identity = _identity(runtime)
    if identity is None:
        return ChallengeResponse(**_unavailable().model_dump())
    if identity.enrollment.status().enrolled is not True:
        return ChallengeResponse(
            status="rejected", reason_code="not_enrolled", message=_msg("not_enrolled")
        )
    challenge = identity.coordinator.issue_challenge(identity.session_id)
    return ChallengeResponse(
        status="ok",
        challenge_id=challenge.challenge_id,
        text_en=challenge.display("en"),
        text_fa=challenge.display("fa"),
        expires_in_seconds=int(
            (challenge.expires_at - challenge.issued_at).total_seconds()
        ),
    )


@router.post("/voice/guest/start", response_model=OperationResult)
def guest_start(
    payload: GuestStartRequest, runtime: DesktopRuntime = _runtime
) -> OperationResult:
    identity = _identity(runtime)
    if identity is None:
        return _unavailable()
    _step_up_or_raise(runtime, "voice:start_guest", payload.step_up.get_secret_value())
    audio = _decode(payload.audio_base64)
    if audio is None:
        return OperationResult(
            status="rejected",
            reason_code="audio_invalid",
            message=_msg("audio_invalid"),
        )
    if identity.guests.status().active:
        return OperationResult(
            status="rejected", reason_code="guest_active", message=_msg("guest_active")
        )
    proof = identity.coordinator.prove_owner(
        session_id=identity.session_id,
        challenge_id=payload.challenge_id,
        audio=audio,
        purpose="start_guest",
    )
    if proof is None:
        runtime.activity.add("voice", "Guest Mode refused", "owner_verification_failed")
        return OperationResult(
            status="denied",
            reason_code="owner_verification_failed",
            message=_msg("owner_verification_failed"),
        )
    try:
        identity.guests.activate(
            proof,
            identity.step_up.mint("start_guest"),
            session_id=identity.session_id,
            minutes=payload.minutes,
        )
    except (GuestModeError, AuthorizationError, ChallengeError):
        return OperationResult(
            status="rejected", reason_code="guest_active", message=_msg("guest_active")
        )
    runtime.activity.add("voice", "Guest Mode started", "active")
    return OperationResult(status="ok")


@router.post("/voice/guest/end", response_model=OperationResult)
def guest_end(runtime: DesktopRuntime = _runtime) -> OperationResult:
    """Ending Guest Mode only ever REMOVES access, so it needs no proof."""

    identity = _identity(runtime)
    if identity is None:
        return _unavailable()
    if identity.guests.revoke():
        runtime.activity.add("voice", "Guest Mode ended", "revoked")
    return OperationResult(status="ok")


__all__ = ["router"]
