"""Production speech routing with synthetic identity/audio providers; no LLM/network."""

from __future__ import annotations

from pathlib import Path

import pytest

from sam.agent.errors import AgentError
from sam.desktop.speech import GREETING, PREVIEW, SamSpeechService
from sam.models.models import PrivacyClass
from sam.permissions.models import (
    DecisionOutcome,
    PermissionAction,
    PermissionRequest,
    PermissionResource,
    PermissionScope,
)
from sam.storage.database import Database
from sam.storage.migrations import MIGRATIONS, migrate
from sam.storage.settings import SQLiteOwnerSettings
from sam.tts.provider import FakeSpeechSynthesisProvider
from tests.test_voice_wake import WakeBridge
from tests.voice_identity_support import OTHER_VOICE


@pytest.fixture(autouse=True)
def local_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        SamSpeechService,
        "local_voice",
        lambda self, lang: "Samantha" if lang == "en" else None,
    )


def enable(b: WakeBridge, *, external: bool = False) -> None:
    result = b.post(
        "/speech/settings", {"enabled": True, "external_approved": external}
    ).json()
    assert result["enabled"] is True


def play(b: WakeBridge, ticket: str) -> dict:
    return b.post("/speech/play", {"speech_id": ticket}).json()


def test_owner_speech_setting_updates_readiness_and_invalidates_tickets() -> None:
    b = WakeBridge()
    assert b.post("/speech/status", {}).json()["reason_code"] == "automatic_speech_off"
    enable(b)
    assert b.post("/speech/status", {}).json()["state"] == "SAM_VOICE_READY"
    ticket = b.post("/speech/preview", {}).json()["speech_id"]
    b.post("/speech/settings", {"enabled": False})
    assert play(b, ticket)["status"] == "denied"


@pytest.mark.parametrize("language", ["en", "fa"])
def test_preview_is_fixed_language_scoped_speech_and_never_calls_agent(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = WakeBridge()
    monkeypatch.setattr(
        b.runtime.sam_speech,
        "local_voice",
        lambda lang: "Samantha" if lang == "en" else "installed-persian-test-voice",
    )
    enable(b)
    ticket = b.post("/speech/preview", {"language": language}).json()
    result = play(b, ticket["speech_id"])
    assert result["status"] == "ok" and result["kind"] == "local"
    assert result["text"] == PREVIEW[language] and result["language"] == language
    assert play(b, ticket["speech_id"])["reason_code"] == "speech_session_invalid"
    assert not b.agent.messages and b.voice_stt.call_count == b.wake.call_count == 0


def test_voice_unavailable_never_claims_ready_or_switches_language() -> None:
    b = WakeBridge()
    enable(b)
    state = b.post("/speech/status", {"language": "fa"}).json()
    assert state["state"] == "SAM_VOICE_NOT_CONFIGURED"
    assert state["reason_code"] == "speech_language_unavailable"
    assert b.post("/speech/preview", {"language": "fa"}).json()["speech_id"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"text": "Forged speech"},
        {"principal": "owner"},
        {"provider": "evil"},
        {"language": "xx"},
    ],
)
def test_webview_cannot_supply_preview_text_authority_or_provider(body: dict) -> None:
    b = WakeBridge()
    enable(b)
    assert b.post("/speech/preview", body).status_code == 422


def test_guest_cannot_enable_preview_or_play_owner_speech() -> None:
    b = WakeBridge()
    b.enroll()
    enable(b)
    ticket = b.post("/speech/preview", {}).json()["speech_id"]
    assert b.start_guest()["status"] == "ok"
    for path, body in (
        ("settings", {"enabled": True}),
        ("preview", {}),
        ("play", {"speech_id": ticket}),
    ):
        assert b.post("/speech/" + path, body).status_code == 403
    assert b.runtime.sam_speech.play(ticket).reason_code == "guest_not_allowed"


@pytest.mark.parametrize(("wake_text", "language"), [("Sam", "en"), ("سام", "fa")])
def test_verified_wake_mints_greeting_without_agent_or_extra_confirmation(
    wake_text: str, language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = WakeBridge(wake_text=wake_text)
    monkeypatch.setattr(
        b.runtime.sam_speech, "local_voice", lambda lang: "installed-test-voice"
    )
    b.enroll(samples=3)
    b.activate()
    enable(b)
    woke = b.wake_clip()
    assert woke["owner_verified"] is True and woke["language"] == language
    assert play(b, woke["speech_id"])["text"] == GREETING[language]
    assert not b.agent.messages and b.voice_stt.call_count == 0


def test_missing_profile_and_wrong_speaker_never_receive_greeting() -> None:
    b = WakeBridge()
    b.activate()
    enable(b)
    absent = b.wake_clip()
    assert (
        absent["reason_code"] == "voice_not_enrolled" and not absent["owner_verified"]
    )
    b.enroll()
    wrong = b.post("/voice/wake", {"audio_base64": b.clip(OTHER_VOICE)}).json()
    assert wrong["reason_code"] == "speaker_not_recognized"
    assert not wrong["owner_verified"] and wrong["speech_id"] is None
    assert not b.agent.messages


def test_unavailable_model_does_not_prevent_verified_local_wake_greeting() -> None:
    b = WakeBridge()
    b.enroll(samples=3)
    b.activate()
    enable(b)
    b.runtime.agent_configured = False
    woke = b.wake_clip()
    assert woke["owner_verified"] is True
    assert play(b, woke["speech_id"])["text"] == GREETING["en"]
    assert not b.agent.messages and b.voice_stt.call_count == 0


def test_unavailable_model_speaks_failure_only_after_owner_verification() -> None:
    b = WakeBridge()
    b.enroll(samples=3)
    enable(b)
    b.runtime.agent_configured = False
    b.agent.raises = AgentError("No model is available")
    result = b.turn()
    assert result["status"] == "failed" and result["speaker"] == "owner"
    assert (
        play(b, result["speech_id"])["text"]
        == "I can't reach my language model right now."
    )
    assert len(b.agent.messages) == 1
    wrong = b.post("/voice/utterance", {"audio_base64": b.clip(OTHER_VOICE)}).json()
    assert wrong["speech_id"] is None
    assert len(b.agent.messages) == 1


def test_normal_owner_reply_is_automatically_queued_without_step_up() -> None:
    b = WakeBridge()
    b.enroll(samples=3)
    enable(b)
    result = b.turn()
    assert result["speaker"] == "owner" and result["speech_id"]
    assert play(b, result["speech_id"])["text"] == "hi there"
    assert result["challenge"] is None


def test_tts_failure_keeps_successful_response_and_transcript() -> None:
    b = WakeBridge()
    b.enroll()
    enable(b)
    result = b.post(
        "/voice/utterance", {"audio_base64": b.clip(), "language": "fa"}
    ).json()
    assert result["status"] == "ok" and result["transcript"] and result["reply"]
    assert (
        result["speech_id"] is None
        and result["speech_reason"] == "speech_language_unavailable"
    )


def test_speech_mode_grants_no_career_send_or_delete_authority() -> None:
    b = WakeBridge()
    enable(b)
    play(b, b.post("/speech/preview", {}).json()["speech_id"])
    grants = b.runtime.grants.list_grants(b.runtime.principal)
    speech = [g for g in grants if g.metadata.get("origin") == "owner_speech_mode"]
    assert len(speech) == 1
    assert speech[0].resource is PermissionResource.SPEECH_SYNTHESIS
    assert speech[0].scope.segments == ("assistant", "macos-system", "sam-local-en")
    decision = b.runtime.permissions.evaluate(
        PermissionRequest(
            principal=b.runtime.principal,
            resource=PermissionResource.GMAIL,
            action=PermissionAction.SEND,
            scope=PermissionScope(segments=("any",)),
        )
    )
    assert decision.outcome is not DecisionOutcome.ALLOW


def test_external_speech_requires_separate_consent_and_rechecks_privacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeSpeechSynthesisProvider()
    b = WakeBridge(tts=provider)
    monkeypatch.setattr(b.runtime.sam_speech, "local_voice", lambda lang: None)
    enable(b)
    assert (
        b.post("/speech/status", {}).json()["reason_code"]
        == "external_speech_consent_required"
    )
    enable(b, external=True)
    for privacy in (PrivacyClass.PRIVATE, PrivacyClass.SECRET):
        assert (
            b.runtime.sam_speech.queue(
                "A personal record", "en", privacy=privacy
            ).speech_id
            is None
        )
    for text in (
        "api_key=sk-abcdefghijk1234567890abcdefghijklmnop",
        "voice embedding: 0.12, 0.23, 0.45",
    ):
        assert b.runtime.sam_speech.queue(text, "en").speech_id is None
    assert provider.call_count == 0
    preview = b.post("/speech/preview", {}).json()
    assert play(b, preview["speech_id"])["kind"] == "audio"
    assert provider.call_count == 1


def test_setting_survives_backend_restart_and_migration_preserves_existing_choices(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "settings.sqlite3")
    migrate(db, migrations=MIGRATIONS[:3])
    before = SQLiteOwnerSettings(db)
    before.set_voice_activation(True)
    migrate(db)
    settings = SQLiteOwnerSettings(db)
    assert (
        settings.voice_activation()
        and not settings.voice_responses()
        and not settings.voice_external()
    )
    settings.set_voice_responses(True)
    db.close()
    reopened = Database(tmp_path / "settings.sqlite3")
    assert SQLiteOwnerSettings(reopened).voice_responses()
    assert not SQLiteOwnerSettings(reopened).voice_external()
    reopened.close()
