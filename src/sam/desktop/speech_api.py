"""Fixed owner-only speech controls. No caller-provided speech text or authority."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from sam.desktop.runtime import DesktopRuntime
from sam.desktop.security import owner_bridge_runtime
from sam.desktop.speech import (
    PREVIEW,
    SamVoiceStatus,
    SpeechPlaybackResponse,
    SpeechTicketResponse,
)
from sam.language.catalog import LanguageCode

router = APIRouter(prefix="/desktop/v1/speech", tags=["sam-speech"])
_owner = Depends(owner_bridge_runtime)


class SpeechLanguage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    language: LanguageCode = "en"


class SpeechSettings(SpeechLanguage):
    enabled: StrictBool
    external_approved: StrictBool = False


class SpeechPlayback(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    speech_id: str = Field(pattern=r"^[a-f0-9]{32}$")


@router.post("/status", response_model=SamVoiceStatus)
def speech_status(
    payload: SpeechLanguage, runtime: DesktopRuntime = _owner
) -> SamVoiceStatus:
    return runtime.sam_speech.status(payload.language)


@router.post("/settings", response_model=SamVoiceStatus)
def speech_settings(
    payload: SpeechSettings, runtime: DesktopRuntime = _owner
) -> SamVoiceStatus:
    runtime.sam_speech.configure(payload.enabled, payload.external_approved)
    return runtime.sam_speech.status(payload.language)


@router.post("/preview", response_model=SpeechTicketResponse)
def speech_preview(
    payload: SpeechLanguage, runtime: DesktopRuntime = _owner
) -> SpeechTicketResponse:
    text = PREVIEW.get(payload.language)
    if text is None:
        return SpeechTicketResponse(
            status="rejected", reason_code="preview_language_unavailable"
        )
    return runtime.sam_speech.queue(text, payload.language)


@router.post("/play", response_model=SpeechPlaybackResponse)
def speech_play(
    payload: SpeechPlayback, runtime: DesktopRuntime = _owner
) -> SpeechPlaybackResponse:
    return runtime.sam_speech.play(payload.speech_id)
