"""Activation warms model weights only, without granting speaker authority."""

from __future__ import annotations

import threading

import pytest

from sam.voice_identity.errors import ModelSetupError
from sam.voice_local.speaker import SpeechBrainEcapaEmbeddingProvider
from tests.test_voice_wake import WakeBridge


def test_activation_warms_both_models_without_reading_audio_or_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = WakeBridge()
    finished = threading.Event()
    calls: list[str] = []

    def wake() -> None:
        calls.append("wake")

    def speaker() -> None:
        calls.append("speaker")
        finished.set()

    def forbidden() -> None:
        pytest.fail("model warmup must not read an owner profile")

    monkeypatch.setattr(bridge.wake, "preload", wake, raising=False)
    monkeypatch.setattr(bridge.embedder, "preload", speaker, raising=False)
    monkeypatch.setattr(bridge.store, "load", forbidden)
    assert bridge.activate()["status"] == "ok"
    assert finished.wait(2)
    assert calls == ["wake", "speaker"]
    assert bridge.embedder.call_count == bridge.wake.call_count == 0
    assert bridge.agent.messages == []


def test_repeated_activation_does_not_start_parallel_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = WakeBridge()
    started, release, finished = (threading.Event() for _ in range(3))
    calls: list[str] = []

    def wake() -> None:
        calls.append("wake")
        started.set()
        assert release.wait(2)

    def speaker() -> None:
        calls.append("speaker")
        finished.set()

    monkeypatch.setattr(bridge.wake, "preload", wake, raising=False)
    monkeypatch.setattr(bridge.embedder, "preload", speaker, raising=False)
    bridge.activate()
    assert started.wait(2)
    try:
        bridge.activate()
        assert calls == ["wake"]
    finally:
        release.set()
    assert finished.wait(2)
    assert calls == ["wake", "speaker"]


def test_warmup_failure_is_contained_and_verification_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = WakeBridge()
    bridge.enroll()
    attempted = threading.Event()

    def unavailable(*args: object, **kwargs: object) -> None:
        attempted.set()
        raise ModelSetupError("local model unavailable")

    monkeypatch.setattr(bridge.embedder, "preload", unavailable, raising=False)
    monkeypatch.setattr(bridge.embedder, "embed", unavailable)
    assert bridge.activate()["status"] == "ok"
    assert attempted.wait(2)
    outcome = bridge.turn()
    assert outcome["status"] == "denied"
    assert outcome["speaker_result"] == "verification_error"
    assert bridge.agent.messages == []


def test_local_speaker_preload_only_loads_and_preserves_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SpeechBrainEcapaEmbeddingProvider()
    revision = provider.model_revision
    loaded: list[bool] = []
    monkeypatch.setattr(provider, "_load", lambda: loaded.append(True))
    provider.preload()
    assert loaded == [True]
    assert provider.model_revision == revision


def test_settings_distinguishes_insufficient_speech_from_verification_error() -> None:
    bridge = WakeBridge()
    bridge.enroll()
    bridge.post("/voice/utterance", {"audio_base64": bridge.clip(frames=3_984)})
    status = bridge.get("/voice/identity").json()
    assert status["last_verification"] == "insufficient_audio"
    assert status["enrolled"] is True and status["profile_compatible"] is True
