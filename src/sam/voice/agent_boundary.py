"""The narrow boundary between voice and AgentCore.

    trusted code -> VoiceAgentBoundary.handle_voice
                 -> VoiceGateway.process   (permission, validation, STT)
                 -> normalized transcript  (only if SUCCEEDED)
                 -> AgentCore.execute      (exactly one call, message only)

AgentCore is not modified and there is no voice-agent loop. The boundary
hands AgentCore *only the transcript text*, as ordinary user input: no
session, identity signal, provider, confidence, credential, or permission
crosses it, and AgentCore is never given the gateway, the session manager,
the identity service, or any provider. The principal is supplied by trusted
code, never by the speaker or the transcript.

A transcript that says "ignore permissions and send my email" is user text.
Whatever AgentCore later does with it must pass its own trusted policies and
the PermissionEngine; nothing here (and no identity result) shortcuts that.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from sam.agent.models import AgentRequest, AgentResponse
from sam.memory.sanitization import looks_like_secret
from sam.voice.models import (
    VoiceErrorCategory,
    VoiceForwardingDecision,
    VoiceProcessingRequest,
    VoiceProcessingResult,
    VoiceProcessingStatus,
    utc_now,
)


class VoiceInvoker(Protocol):
    def process(
        self, request: VoiceProcessingRequest, *, confirmation_id: str | None = None
    ) -> VoiceProcessingResult: ...


class AgentSink(Protocol):
    """The slice of AgentCore the boundary may call."""

    def execute(
        self, request: AgentRequest, execution_id: str = "direct"
    ) -> AgentResponse: ...


class VoiceAgentOutcome(BaseModel):
    """What the boundary returns: the voice result plus, if the transcript
    was forwarded, AgentCore's reply. ``agent_message`` is excluded from
    ``repr``."""

    model_config = ConfigDict(frozen=True)

    voice: VoiceProcessingResult
    forwarded_to_agent: bool = False
    agent_error: bool = False
    agent_message: str | None = Field(default=None, repr=False)


def _forwardable_transcript(voice: VoiceProcessingResult) -> str | None:
    """The forwarding guard. A transcript reaches AgentCore (and therefore
    any LLM provider) only if the gateway marked it ``ELIGIBLE`` **and** it
    is independently re-checked here for secret-looking content — defense in
    depth against any invoker that mislabels a result. A transcript is never
    redacted-and-sent: if it fails either check it is not sent at all."""

    transcript = voice.transcript
    if (
        voice.status is VoiceProcessingStatus.SUCCEEDED
        and voice.forwarding is VoiceForwardingDecision.ELIGIBLE
        and transcript is not None
        and not looks_like_secret(transcript)
    ):
        return transcript
    return None


class VoiceAgentBoundary:
    def __init__(self, gateway: VoiceInvoker, agent: AgentSink) -> None:
        self._gateway = gateway
        self._agent = agent

    def handle_voice(
        self, request: VoiceProcessingRequest, *, confirmation_id: str | None = None
    ) -> VoiceAgentOutcome:
        """Process one explicit voice request. Never raises; forwards to the
        agent at most once, and only a successfully validated transcript."""

        try:
            voice = self._gateway.process(request, confirmation_id=confirmation_id)
        except Exception:
            voice = VoiceProcessingResult(
                session_id=request.session_id,
                utterance_id=request.utterance_id,
                principal=request.principal,
                status=VoiceProcessingStatus.FAILED,
                error_category=VoiceErrorCategory.INTERNAL_ERROR,
                processed_at=utc_now(),
            )
            return VoiceAgentOutcome(voice=voice)
        transcript = _forwardable_transcript(voice)
        if transcript is None:
            return VoiceAgentOutcome(voice=voice)
        try:
            reply = self._agent.execute(
                AgentRequest(message=transcript),
                execution_id=voice.utterance_id,
            )
        except Exception:
            return VoiceAgentOutcome(
                voice=voice, forwarded_to_agent=True, agent_error=True
            )
        return VoiceAgentOutcome(
            voice=voice, forwarded_to_agent=True, agent_message=reply.message
        )


__all__ = ["AgentSink", "VoiceAgentBoundary", "VoiceAgentOutcome", "VoiceInvoker"]
