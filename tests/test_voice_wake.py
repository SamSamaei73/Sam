"""Hands-free voice activation: the local wake word, the owner-only switch,
conversation stop phrases and clear identity reasons. Fake local recognizer,
fake speaker embedder and a stub agent: no model or provider is ever called."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from sam.core.config import Settings
from sam.storage.database import Database
from sam.storage.migrations import migrate
from sam.storage.settings import InMemoryOwnerSettings, SQLiteOwnerSettings
from sam.system import cli
from sam.system.secrets import KNOWN_SECRETS, resolve_credentials
from sam.voice.transcription import FakeTranscriptionProvider
from sam.voice.wake import (
    WakeRateLimiter,
    detect_wake,
    is_stop_phrase,
)
from tests.career_support import JOB_OFFICIAL, OFFICIAL_URL
from tests.desktop_support import Bridge
from tests.test_desktop_identity import SECRET, IdentityBridge
from tests.voice_identity_support import OTHER_VOICE, OWNER_VOICE
from tests.voice_support import make_wav

SECONDS = 16_000  # frames per second in the synthetic clips


class WakeBridge(IdentityBridge):
    """An identity-enabled bridge plus a separate LOCAL wake recognizer."""

    def __init__(self, wake_text: str = "Sam", **kw: Any) -> None:
        super().__init__(**kw)
        self.wake = FakeTranscriptionProvider(wake_text)
        self.runtime.wake_transcriber = self.wake

    def activate(self, on: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = self.post("/voice/activation", {"enabled": on}).json()
        return body

    def wake_clip(self, frames: int = SECONDS) -> dict[str, Any]:
        audio = base64.b64encode(make_wav(frames=frames, seed=7)).decode()
        body: dict[str, Any] = self.post("/voice/wake", {"audio_base64": audio}).json()
        return body

    def turn(self, voice: Any = OWNER_VOICE) -> dict[str, Any]:
        body: dict[str, Any] = self.post(
            "/voice/utterance",
            {"audio_base64": self.clip(voice), "hands_free": True},
        ).json()
        return body


# ------------------------------------------------------------ wake word text


@pytest.mark.parametrize(
    ("text", "wake", "followed"),
    [
        ("Sam", True, False),
        ("Sam.", True, False),
        ("Hey Sam!", True, False),
        ("Okay, Sam", True, False),
        ("Sam, what's on my calendar today?", True, True),
        ("سام", True, False),
        ("I told Sam about it", False, False),
        ("Samuel called", False, False),
        ("same as before", False, False),
        ("Thank you.", False, False),
        ("", False, False),
    ],
)
def test_the_wake_word_is_the_name_at_the_start(
    text: str, wake: bool, followed: bool
) -> None:
    match = detect_wake(text)
    assert (match.wake, match.followed) == (wake, followed)


@pytest.mark.parametrize(
    ("text", "stop"),
    [
        ("Sam, go to sleep.", True),
        ("That's all.", True),
        ("Stop listening", True),
        ("stop", True),
        ("بسه", True),
        ("Stop the timer at five", False),
        ("Can you stop reminding me about that?", False),
        ("What's my project status?", False),
    ],
)
def test_stop_phrases_match_the_whole_utterance_only(text: str, stop: bool) -> None:
    assert is_stop_phrase(text) is stop


def test_the_wake_rate_is_bounded() -> None:
    now = [0.0]
    limiter = WakeRateLimiter(limit=3, window=10.0, clock=lambda: now[0])
    assert [limiter.allow() for _ in range(5)] == [True, True, True, False, False]
    now[0] = 11.0
    assert limiter.allow() is True


# --------------------------------------------------------- owner-only switch


def test_voice_activation_is_off_until_the_owner_turns_it_on() -> None:
    b = WakeBridge()
    assert b.get("/status").json()["voice_activation"] == "off"
    off = b.wake_clip()
    assert off["reason_code"] == "voice_activation_off" and off["wake"] is False
    assert b.wake.call_count == 0  # sleeping audio is not even transcribed
    assert b.activate()["voice_activation"] == "on"
    assert b.get("/status").json()["voice_activation"] == "on"
    assert b.activate(False)["voice_activation"] == "off"


def test_a_guest_cannot_change_voice_activation() -> None:
    b = WakeBridge()
    b.enroll()
    assert b.start_guest()["status"] == "ok"
    refused = b.post("/voice/activation", {"enabled": True})
    assert refused.status_code == 403
    assert b.runtime.owner_settings.voice_activation() is False


def test_voice_activation_is_unavailable_without_local_identity() -> None:
    b = Bridge()
    assert b.get("/status").json()["voice_activation"] == "unavailable"
    assert b.post("/voice/activation", {"enabled": True}).json()["status"] == (
        "not_configured"
    )


def test_the_wake_recognizer_is_only_set_with_owner_identity() -> None:
    from typing import cast

    from sam.agent.core import AgentCore
    from sam.desktop.runtime import build_desktop_runtime
    from tests.desktop_support import StubAgent

    runtime = build_desktop_runtime(
        Settings(),
        cast(AgentCore, StubAgent()),
        wake_transcriber=FakeTranscriptionProvider("Sam"),
    )
    assert runtime.identity is None and runtime.wake_transcriber is None


# ------------------------------------------------------------ wake behaviour


def test_the_name_wakes_sam_and_nothing_else_happens() -> None:
    b = WakeBridge()
    b.activate()
    grants_before = b.get("/permissions").json()["grants"]
    woke = b.wake_clip()
    assert woke == {**woke, "status": "ok", "wake": True, "followed": False}
    assert "Sam" not in str(woke)  # never the recognized text
    assert b.agent.messages == []  # no model call
    assert b.voice_stt.call_count == 0  # the conversation recognizer was untouched
    assert b.get("/permissions").json()["grants"] == grants_before
    labels = [i["label"] for i in b.get("/activity").json()["items"]]
    assert not any("Sam" == label for label in labels)


def test_speech_without_the_name_does_not_wake_sam() -> None:
    b = WakeBridge(wake_text="what time is the meeting tomorrow")
    b.activate()
    body = b.wake_clip()
    assert body["status"] == "ok" and body["wake"] is False
    assert b.agent.messages == []
    assert "meeting" not in str(body)


def test_sleeping_audio_writes_no_memory_and_is_not_kept() -> None:
    b = WakeBridge(wake_text="my bank password is hunter2")
    b.activate()
    for _ in range(5):
        assert b.wake_clip()["wake"] is False
    assert b.agent.messages == []
    assert b.post("/memory/search", {"text": ""}).json()["items"] == []
    items = b.get("/activity").json()["items"]
    assert "hunter2" not in str(items) and "password" not in str(items)


def test_a_segment_too_long_to_be_a_wake_word_is_not_transcribed() -> None:
    b = WakeBridge()
    b.activate()
    assert b.wake_clip(frames=6 * SECONDS)["wake"] is False
    assert b.wake.call_count == 0


def test_a_flood_of_wake_candidates_is_bounded() -> None:
    b = WakeBridge(wake_text="noise")
    b.activate()
    codes = [b.wake_clip()["reason_code"] for _ in range(35)]
    assert codes.count("rate_limited") == 5
    assert b.wake.call_count == 30


def test_a_recognizer_failure_fails_closed() -> None:
    b = WakeBridge()
    b.wake = FakeTranscriptionProvider(raises=RuntimeError("boom"))
    b.runtime.wake_transcriber = b.wake
    b.activate()
    body = b.wake_clip()
    assert body["wake"] is False and body["status"] == "failed"


# ------------------------------------------------------------- conversation


def test_a_missing_voice_profile_asks_for_setup_not_a_generic_block() -> None:
    b = WakeBridge()
    body = b.turn()
    assert body["reason_code"] == "voice_not_enrolled"
    assert "Set up your voice" in body["message"]
    assert b.agent.messages == []


def test_the_owner_talks_hands_free_without_saying_sam_each_turn() -> None:
    b = WakeBridge(text="how is phase seventeen going")
    b.enroll()
    b.activate()
    first = b.turn()
    assert first["status"] == "ok" and first["speaker"] == "owner"
    assert first["reply"] == "hi there"
    b.voice_stt._result = "and what remains"
    second = b.turn()
    assert second["status"] == "ok" and second["reply"] == "hi there"
    assert b.agent.messages == ["how is phase seventeen going", "and what remains"]


def test_a_stop_phrase_ends_the_conversation_without_a_model_call() -> None:
    b = WakeBridge(text="Sam, go to sleep.")
    b.enroll()
    body = b.turn()
    assert body["reason_code"] == "conversation_ended" and body["status"] == "ok"
    assert body["transcript"] is None and body["reply"] is None
    assert b.agent.messages == []


def test_a_stop_phrase_only_counts_in_a_hands_free_turn() -> None:
    b = WakeBridge(text="go to sleep")
    b.enroll()
    body = b.utter(OWNER_VOICE)  # push-to-talk: an ordinary request
    assert body["status"] == "ok" and b.agent.messages == ["go to sleep"]


def test_another_speaker_does_not_become_the_owner() -> None:
    b = WakeBridge(text="transfer everything")
    b.enroll()
    b.activate()
    assert b.wake_clip()["wake"] is True  # anyone can say the name...
    body = b.turn(OTHER_VOICE)  # ...but the turn is identity-checked
    assert body["reason_code"] == "owner_verification_required"
    assert body["transcript"] is None and b.agent.messages == []


def test_voice_activation_grants_no_side_effect_permission() -> None:
    b = WakeBridge(text="submit this job application")
    b.enroll()
    before = b.get("/permissions").json()["grants"]
    b.activate()
    assert b.turn()["status"] == "ok"  # a normal owner conversation turn
    after = b.get("/permissions").json()["grants"]
    assert after == before
    career = {g["action"] for g in after if g["resource"] == "career"}
    assert "submit" not in career and "send" not in career
    opportunity = b.post(
        "/career/opportunity",
        {
            "action": "import",
            "text": JOB_OFFICIAL,
            "url": OFFICIAL_URL,
            "source_kind": "official_career_page",
            "opportunity_type": "job",
        },
    ).json()["item_id"]
    draft = b.post(
        "/career/draft", {"action": "create", "opportunity_id": opportunity}
    ).json()["item_id"]
    assert b.post("/career/submit", {"draft_id": draft}).json()["status"] != "ok"


def test_voice_activation_does_not_enable_the_scheduler() -> None:
    b = WakeBridge()
    b.activate()
    assert b.runtime.proactive.scheduler_enabled is False
    assert b.runtime.owner_settings.scheduler_persistent() is False


# ------------------------------------------------------ settings and secrets


def test_voice_activation_persists_in_the_owner_settings(tmp_path: Path) -> None:
    db = Database(tmp_path / "sam.sqlite3")
    migrate(db)
    store = SQLiteOwnerSettings(db)
    assert store.voice_activation() is False
    store.set_voice_activation(True)
    assert SQLiteOwnerSettings(db).voice_activation() is True
    db.close()
    memory = InMemoryOwnerSettings()
    assert memory.voice_activation() is False


def test_the_cli_switch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(sam_data_dir=str(tmp_path))
    db = Database(tmp_path / "sam.sqlite3")
    migrate(db)
    db.close()
    assert cli.main(["voice-activation", "on"], settings=settings) == 0
    assert '"voice_activation": "on"' in capsys.readouterr().out
    assert cli.main(["voice-activation", "status"], settings=settings) == 0
    assert '"voice_activation": "on"' in capsys.readouterr().out
    assert cli.main(["voice-activation", "off"], settings=settings) == 0
    assert '"voice_activation": "off"' in capsys.readouterr().out


class _Store:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def test_the_step_up_secret_comes_from_the_keychain_in_production() -> None:
    assert "desktop_step_up_secret" in KNOWN_SECRETS
    settings = Settings(
        app_env="production",
        desktop_step_up_secret=SecretStr("an-ambient-environment-value"),
    )
    resolved, report = resolve_credentials(
        settings, _Store({"desktop_step_up_secret": SECRET})
    )
    step_up = resolved.desktop_step_up_secret
    assert step_up is not None and step_up.get_secret_value() == SECRET
    assert report.sources["desktop_step_up_secret"].value == "keychain"
    resolved, _ = resolve_credentials(settings, _Store({}))
    assert resolved.desktop_step_up_secret is None  # the environment is ignored
    resolved, _ = resolve_credentials(
        settings, _Store({"desktop_step_up_secret": "short"})
    )
    assert resolved.desktop_step_up_secret is None  # too weak: fails closed


def test_turning_activation_on_warms_the_local_recognizer() -> None:
    import threading

    warmed = threading.Event()

    class Warmable(FakeTranscriptionProvider):
        def preload(self) -> None:
            warmed.set()

    b = WakeBridge()
    b.runtime.wake_transcriber = Warmable("Sam")
    b.activate()
    assert warmed.wait(5)


def test_sleeping_mode_privacy_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While Sam waits for its name, every sleeping-mode check is local and
    leaves nothing behind: counted explicitly across silence, ordinary
    speech, a secret-looking phrase and the name itself."""

    from sam.tts.provider import FakeSpeechSynthesisProvider

    tts = FakeSpeechSynthesisProvider()
    b = WakeBridge(wake_text="Sam", tts=tts)
    b.activate()
    monkeypatch.chdir(tmp_path)  # any stray relative write would land here
    memory_before = b.post("/memory/search", {"text": ""}).json()["items"]
    knowledge_before = b.get("/knowledge/resources").json()["resources"]
    permission_events = len(b.runtime.permission_audit.list_events())
    phrases = [
        "",  # silence / nothing recognised
        "what time is the meeting tomorrow",
        "my bank password is hunter2",
        "Samuel called yesterday",
        "Sam",
        "Sam",
    ]
    woke = 0
    for text in phrases:
        b.wake._result = text
        answer = b.wake_clip()
        assert set(answer) >= {"status", "wake", "followed"}
        assert text == "" or text not in str(answer)
        woke += bool(answer["wake"])
    assert woke == 2  # only the name wakes Sam
    # Wake checks are not permission-checked operations: no audit event, so
    # no audit entry can ever carry sleeping-mode content.
    assert len(b.runtime.permission_audit.list_events()) == permission_events
    assert b.agent.messages == []  # zero Claude / Gemini / any model calls
    assert tts.call_count == 0  # zero speech-provider calls
    assert b.voice_stt.call_count == 0  # the conversation path never ran
    assert b.post("/memory/search", {"text": ""}).json()["items"] == memory_before
    assert b.get("/knowledge/resources").json()["resources"] == knowledge_before
    activity = str(b.get("/activity").json()["items"])
    for text in phrases:
        assert not text or text not in activity
    assert list(tmp_path.iterdir()) == []  # no audio or transcript file
