"""Shared fixtures for the desktop bridge tests (fakes only, no network)."""

from __future__ import annotations

import base64
from typing import Any, cast

from fastapi.testclient import TestClient
from pydantic import SecretStr

from sam.agent.core import AgentCore
from sam.agent.errors import AgentError
from sam.agent.models import AgentRequest, AgentResponse, ExecutionStatus
from sam.core.config import Settings
from sam.desktop.runtime import DesktopRuntime, build_desktop_runtime
from sam.main import create_app
from sam.tts.models import TrustedVoiceProfile
from sam.tts.profiles import TrustedVoiceProfiles
from sam.tts.provider import FakeSpeechSynthesisProvider
from sam.voice.transcription import FakeTranscriptionProvider
from tests.voice_support import make_wav

TOKEN = "t" * 40
HEADERS = {"x-sam-desktop-token": TOKEN}


class StubAgent:
    """Records what reaches "AgentCore"; never calls a model."""

    def __init__(
        self, reply: str = "hi there", raises: Exception | None = None
    ) -> None:
        self.reply = reply
        self.raises = raises
        self.messages: list[str] = []
        self.languages: list[str | None] = []

    def execute(
        self,
        request: AgentRequest,
        execution_id: str = "direct",
        *,
        response_language: str | None = None,
    ) -> AgentResponse:
        self.languages.append(response_language)
        self.messages.append(request.message)
        if self.raises is not None:
            raise self.raises
        return AgentResponse(
            message=self.reply,
            execution_id=execution_id,
            status=ExecutionStatus.COMPLETED,
        )


class Bridge:
    def __init__(
        self,
        *,
        agent: StubAgent | None = None,
        stt: FakeTranscriptionProvider | None = None,
        tts: FakeSpeechSynthesisProvider | None = None,
        token: str | None = TOKEN,
        agent_configured: bool = True,
        mcp_admin: Any = None,
        embedder: Any = None,
        profile_store: Any = None,
        step_up: str | None = None,
        clock: Any = None,
        model_router: Any = None,
        model_settings: Any = None,
    ) -> None:
        self.agent = agent or StubAgent()
        self.stt = stt
        self.tts = tts
        settings = Settings(
            desktop_bridge_token=SecretStr(token) if token else None,
            desktop_step_up_secret=SecretStr(step_up) if step_up else None,
        )
        self.settings = settings
        profiles = None
        if tts is not None:
            profiles = TrustedVoiceProfiles(
                [
                    TrustedVoiceProfile(
                        profile_id="sam_default",
                        provider_id=tts.provider_id,
                        provider_voice_reference="abcdef0123456789abcdef0123456789",
                        provider_model="s2.1-pro-free",
                    )
                ]
            )
        self.app = create_app(settings, agent_core=cast(AgentCore, self.agent))
        self.runtime: DesktopRuntime = build_desktop_runtime(
            settings,
            cast(AgentCore, self.agent),
            agent_configured=agent_configured,
            transcription_provider=stt,
            speech_provider=tts,
            speech_profiles=profiles,
            mcp_admin=mcp_admin,
            speaker_embedder=embedder,
            profile_store=profile_store,
            model_router=model_router,
            model_settings=model_settings,
            **({"clock": clock} if clock is not None else {}),
        )
        self.app.state.desktop_runtime = self.runtime
        self.client = TestClient(
            self.app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000)
        )

    def get(self, path: str, **kw: Any) -> Any:
        return self.client.get("/desktop/v1" + path, headers=HEADERS, **kw)

    def post(self, path: str, body: Any, **kw: Any) -> Any:
        return self.client.post("/desktop/v1" + path, json=body, headers=HEADERS, **kw)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def wav_b64(seed: int = 0) -> str:
    return b64(make_wav(seed=seed))


__all__ = ["HEADERS", "TOKEN", "AgentError", "Bridge", "StubAgent", "b64", "wav_b64"]
