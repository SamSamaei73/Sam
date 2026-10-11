"""The configured OpenRouter account serves LLM and speech requests safely.

Production composition and desktop routes use the real adapters over mocked
HTTP. Every credential and audio byte in this module is synthetic.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

import sam.desktop.runtime as runtime_module
import sam.main as main_module
from sam.agent.core import AgentCore
from sam.core.config import Settings
from sam.desktop.runtime import DesktopRuntime, build_desktop_runtime
from sam.models.factory import ModelSettingsStore, build_model_router
from sam.models.router import ModelRouter
from sam.tts.credentials import (
    FakeTTSCredentialProvider,
    TTSCredentialProvider,
    TTSCredentialReference,
    openrouter_credentials_from_settings,
)
from sam.tts.errors import TTSCredentialError
from sam.tts.models import TrustedVoiceProfile
from sam.tts.openrouter import (
    OPENAI_SPEECH_LANGUAGE_CODES,
    OPENROUTER_TTS_PROVIDER_ID,
    OpenRouterTTSProvider,
)
from sam.tts.profiles import TrustedVoiceProfiles
from sam.tts.provider import FakeSpeechSynthesisProvider
from tests.desktop_support import HEADERS, TOKEN, StubAgent
from tests.tts_support import MP3, Recorder

KEY = "synthetic-openrouter-shared-test-key"


@dataclass
class SpeechDesktop:
    client: TestClient
    runtime: DesktopRuntime
    speech: Recorder
    llm_requests: list[httpx.Request]
    credentials: FakeTTSCredentialProvider

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post("/desktop/v1" + path, json=body, headers=HEADERS)
        assert response.status_code == 200
        return cast(dict[str, Any], response.json())

    def approve(self, result: dict[str, Any]) -> str:
        confirmation_id = str(result["challenge"]["confirmation_id"])
        self.post(
            "/confirmations/decide",
            {"confirmation_id": confirmation_id, "approved": True},
        )
        return confirmation_id


@pytest.fixture
def desktop(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[SpeechDesktop]:
    speech = Recorder(getattr(request, "param", None))
    resolved: list[FakeTTSCredentialProvider] = []
    llm_requests: list[httpx.Request] = []

    def provider(
        *,
        credentials: TTSCredentialProvider,
        credential_reference: TTSCredentialReference,
    ) -> OpenRouterTTSProvider:
        assert isinstance(credentials, FakeTTSCredentialProvider)
        resolved.append(credentials)
        return OpenRouterTTSProvider(
            credentials=credentials,
            credential_reference=credential_reference,
            transport=httpx.MockTransport(speech),
        )

    def llm_response(incoming: httpx.Request) -> httpx.Response:
        llm_requests.append(incoming)
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-4.1-mini",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Hello from Sam"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    def model_router(settings: Settings) -> tuple[ModelRouter, ModelSettingsStore]:
        return build_model_router(
            settings, openrouter_transport=httpx.MockTransport(llm_response)
        )

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a legacy speech provider was selected")

    monkeypatch.setattr(runtime_module, "OpenRouterTTSProvider", provider)
    monkeypatch.setattr(runtime_module, "fish_speech_from_settings", forbidden)
    monkeypatch.setattr(runtime_module, "gemini_speech_from_settings", forbidden)
    monkeypatch.setattr(main_module, "build_model_router", model_router)
    settings = Settings(
        desktop_bridge_token=SecretStr(TOKEN),
        openrouter_api_key=SecretStr(KEY),
        # These must not override the configured OpenRouter speech account.
        fish_audio_api_key=SecretStr("synthetic-fish-key"),
        fish_audio_voice_reference="legacy-voice",
        gemini_api_key=SecretStr("synthetic-gemini-key"),
    )
    app = main_module.create_app(settings)
    assert len(resolved) == 1
    with TestClient(
        app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000)
    ) as client:
        yield SpeechDesktop(
            client, app.state.desktop_runtime, speech, llm_requests, resolved[0]
        )


def test_same_key_bootstrap_advertises_both_languages_without_provider_calls(
    desktop: SpeechDesktop,
) -> None:
    runtime = desktop.runtime
    assert runtime.speech_provider_id == OPENROUTER_TTS_PROVIDER_ID
    assert runtime.speech_profiles is not None
    profiles = runtime.speech_profiles.list_profiles()
    assert {profile.profile_id for profile in profiles} == {
        "sam_default",
        "sam_persian",
    }
    assert all(
        profile.provider_id == OPENROUTER_TTS_PROVIDER_ID for profile in profiles
    )
    assert all(profile.provider_voice_reference == "alloy" for profile in profiles)
    assert all(
        profile.provider_model == "openai/gpt-4o-mini-tts-2025-12-15"
        for profile in profiles
    )
    response = desktop.client.get("/desktop/v1/status", headers=HEADERS)
    assert response.status_code == 200
    status = response.json()
    assert status["speech_output"] == status["persian_tts"] == "configured"
    assert {
        profile["profile_id"]: frozenset(profile["languages"])
        for profile in status["speech_profiles"]
    } == {
        "sam_default": OPENAI_SPEECH_LANGUAGE_CODES,
        "sam_persian": OPENAI_SPEECH_LANGUAGE_CODES,
    }
    assert desktop.speech.requests == desktop.llm_requests == []
    assert desktop.credentials.resolve_count == 0


@pytest.mark.parametrize(
    ("language", "profile", "text"),
    [
        ("en", "sam_default", "Read this answer."),
        ("fa", "sam_persian", "سلام سام"),
        ("fr", "sam_default", "Bonjour, je suis Sam."),
        ("es", "sam_default", "Hola, soy Sam."),
        ("ar", "sam_default", "مرحبا، أنا سام."),
        ("hi", "sam_default", "नमस्ते, मैं सैम हूँ।"),
        ("zh", "sam_default", "你好，我是 Sam。"),
    ],
)
def test_llm_and_authorized_multilingual_speech_use_the_same_key(
    desktop: SpeechDesktop, language: str, profile: str, text: str
) -> None:
    chat = desktop.post("/chat", {"message": "Hello Sam"})
    assert chat["status"] == "ok" and chat["reply"] == "Hello from Sam"
    assert len(desktop.llm_requests) == 1
    assert desktop.speech.requests == []  # a model reply never triggers speech
    body = {"text": text, "voice_profile": profile, "language": language}
    first = desktop.post("/tts/speak", body)
    assert first["status"] == "confirmation_required"
    assert first["challenge"]["resource"] == "speech_synthesis"
    assert first["challenge"]["action"] == "send"
    assert desktop.speech.requests == [] and desktop.credentials.resolve_count == 0
    cid = desktop.approve(first)
    assert desktop.speech.requests == [] and desktop.credentials.resolve_count == 0
    done = desktop.post("/tts/speak", {**body, "confirmation_id": cid})
    assert done["status"] == "ok"
    assert base64.b64decode(done["audio_base64"]) == MP3
    assert len(desktop.speech.requests) == 1
    assert desktop.credentials.resolve_count == 1
    sent = desktop.speech.requests[0]
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    assert desktop.llm_requests[0].headers["Authorization"] == f"Bearer {KEY}"
    assert str(sent.url) == "https://openrouter.ai/api/v1/audio/speech"
    payload = json.loads(sent.content)
    assert payload["input"] == text
    assert payload["model"] == "openai/gpt-4o-mini-tts-2025-12-15"
    assert payload["voice"] == "alloy"
    replay = desktop.post("/tts/speak", {**body, "confirmation_id": cid})
    assert replay["status"] != "ok"
    assert len(desktop.speech.requests) == 1


def test_denial_changed_text_or_wrong_language_never_sends_speech(
    desktop: SpeechDesktop,
) -> None:
    body = {
        "text": "Read this answer.",
        "voice_profile": "sam_default",
        "language": "en",
    }
    first = desktop.post("/tts/speak", body)
    cid = first["challenge"]["confirmation_id"]
    desktop.post("/confirmations/decide", {"confirmation_id": cid, "approved": False})
    assert (
        desktop.post("/tts/speak", {**body, "confirmation_id": cid})["status"] != "ok"
    )
    cid = desktop.approve(desktop.post("/tts/speak", body))
    changed = desktop.post(
        "/tts/speak", {**body, "text": "Different answer.", "confirmation_id": cid}
    )
    assert changed["status"] != "ok"
    # A recognized ASR language is not automatically a supported speech voice.
    wrong_language = desktop.post("/tts/speak", {**body, "language": "bn"})
    assert wrong_language["reason_code"] == "language_unsupported"
    unknown = desktop.post("/tts/speak", {**body, "voice_profile": "other-voice"})
    assert unknown["status"] != "ok"
    assert desktop.speech.requests == [] and desktop.credentials.resolve_count == 0


def test_credentials_never_appear_in_desktop_status_profiles_or_results(
    desktop: SpeechDesktop,
) -> None:
    for path in ("/status", "/models", "/permissions"):
        response = desktop.client.get("/desktop/v1" + path, headers=HEADERS)
        assert response.status_code == 200
        assert KEY not in response.text
        assert "synthetic-fish-key" not in response.text
        assert "synthetic-gemini-key" not in response.text
    first = desktop.post(
        "/tts/speak", {"text": "Hello", "voice_profile": "sam_default"}
    )
    assert KEY not in json.dumps(first)
    assert KEY not in repr(desktop.credentials)
    assert KEY not in repr(desktop.runtime.speech_profiles)


@pytest.mark.parametrize(
    "desktop", [httpx.Response(402, text=f"provider diagnostic {KEY}")], indirect=True
)
def test_speech_credit_failure_never_retries_falls_back_or_exposes_provider_body(
    desktop: SpeechDesktop,
) -> None:
    body = {"text": "Read this answer.", "voice_profile": "sam_default"}
    cid = desktop.approve(desktop.post("/tts/speak", body))
    result = desktop.post("/tts/speak", {**body, "confirmation_id": cid})
    assert result["status"] != "ok"
    assert result["reason_code"] == "provider_error"
    assert not result.get("audio_base64")
    assert KEY not in json.dumps(result)
    assert "provider diagnostic" not in json.dumps(result)
    assert len(desktop.speech.requests) == desktop.credentials.resolve_count == 1
    repeated = desktop.post("/tts/speak", {**body, "confirmation_id": cid})
    assert repeated["status"] != "ok"
    assert len(desktop.speech.requests) == 1


def test_explicit_injected_speech_provider_and_profiles_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(settings: Settings) -> None:
        raise AssertionError("explicitly injected speech was replaced")

    monkeypatch.setattr(runtime_module, "openrouter_speech_from_settings", forbidden)
    fake = FakeSpeechSynthesisProvider()
    profiles = TrustedVoiceProfiles(
        [
            TrustedVoiceProfile(
                profile_id="sam_default",
                provider_id=fake.provider_id,
                provider_voice_reference="test-voice",
                provider_model="test-model",
            )
        ]
    )
    runtime = build_desktop_runtime(
        Settings(openrouter_api_key=SecretStr(KEY)),
        cast(AgentCore, StubAgent()),
        speech_provider=fake,
        speech_profiles=profiles,
    )
    assert runtime.speech_provider_id == fake.provider_id
    assert runtime.speech_profiles is profiles
    assert runtime.speech_profile_ids == ["sam_default"]
    assert fake.call_count == 0


def test_provider_specific_tts_model_slug_does_not_expand_legacy_validation() -> None:
    values = {
        "profile_id": "sam_default",
        "provider_id": OPENROUTER_TTS_PROVIDER_ID,
        "provider_voice_reference": "alloy",
        "provider_model": "openai/gpt-4o-mini-tts-2025-12-15",
    }
    assert (
        TrustedVoiceProfile.model_validate(values).provider_model
        == values["provider_model"]
    )
    for provider_id in ("fish-audio", "gemini-tts", "fake-tts", "openrouter"):
        with pytest.raises(ValidationError):
            TrustedVoiceProfile.model_validate({**values, "provider_id": provider_id})
    for model in ("openrouter/auto", "https://example.org/voice", "no-vendor"):
        with pytest.raises(ValidationError):
            TrustedVoiceProfile.model_validate({**values, "provider_model": model})
    with pytest.raises(ValidationError):
        TrustedVoiceProfile.model_validate(
            {
                **values,
                "provider_id": "fake-tts",
                "provider_model": "x" * 65,
            }
        )


def test_openrouter_speech_credential_cannot_be_bound_to_another_provider() -> None:
    wrong = TTSCredentialReference(provider_id="fish-audio", credential_id="other")
    with pytest.raises(TTSCredentialError):
        openrouter_credentials_from_settings(
            Settings(openrouter_api_key=SecretStr(KEY)), wrong
        )
    correct = TTSCredentialReference(
        provider_id=OPENROUTER_TTS_PROVIDER_ID, credential_id="openrouter-main"
    )
    with pytest.raises(TTSCredentialError):
        openrouter_credentials_from_settings(Settings(), correct)
