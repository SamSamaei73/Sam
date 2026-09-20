"""The narrow boundary between an AgentCore text result and speech synthesis.

    AgentCore text  ->  trusted code calls TTSAgentBoundary.speak(...)
                    ->  TTSGateway.synthesize  (profile, validation, secret
                        withholding, PermissionEngine, provider)
                    ->  SynthesisResult (audio in memory only)

AgentCore is not modified and is **not** wired to synthesize automatically.
Synthesis is an explicit call: every request is one deliberate, bounded, and
possibly billable action, and a Phase 11 interface decides when to ask for
voice. Nothing an LLM, a document, an MCP result, a transcript, or a provider
response contains can start one: the LLM-facing shape (``SpeechProposal``)
has exactly ``text`` and a profile id, forbids every other field, and the
principal comes from trusted code.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sam.permissions.models import Principal
from sam.tts.models import (
    MAX_TTS_PROFILE_ID_LENGTH,
    SynthesisRequest,
    SynthesisResult,
    TTSErrorCategory,
    TTSStatus,
    new_id,
    utc_now,
)


class SpeechInvoker(Protocol):
    def synthesize(
        self, request: SynthesisRequest, *, confirmation_id: str | None = None
    ) -> SynthesisResult: ...


class SpeechProposal(BaseModel):
    """All an LLM/agent may propose: text and a *trusted profile id*."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(repr=False)
    voice_profile: str = Field(min_length=1, max_length=MAX_TTS_PROFILE_ID_LENGTH)


class TTSAgentBoundary:
    def __init__(self, gateway: SpeechInvoker) -> None:
        self._gateway = gateway

    def speak(
        self,
        proposal: SpeechProposal,
        *,
        principal: Principal,
        confirmation_id: str | None = None,
        request_id: str | None = None,
    ) -> SynthesisResult:
        """One explicit synthesis. Never raises."""

        rid = request_id or new_id()
        try:
            request = SynthesisRequest(
                principal=principal,
                text=proposal.text,
                trusted_voice_profile=proposal.voice_profile,
                request_id=rid,
            )
        except ValidationError:
            return self._failure(
                principal, rid, TTSStatus.REJECTED, TTSErrorCategory.TEXT_INVALID
            )
        try:
            return self._gateway.synthesize(request, confirmation_id=confirmation_id)
        except Exception:
            return self._failure(
                principal, rid, TTSStatus.FAILED, TTSErrorCategory.INTERNAL_ERROR
            )

    @staticmethod
    def _failure(
        principal: Principal, rid: str, status: TTSStatus, category: TTSErrorCategory
    ) -> SynthesisResult:
        return SynthesisResult(
            request_id=rid,
            synthesis_id=new_id(),
            principal=principal,
            status=status,
            error_category=category,
            created_at=utc_now(),
        )


__all__ = ["SpeechInvoker", "SpeechProposal", "TTSAgentBoundary"]
