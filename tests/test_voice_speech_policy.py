"""Short requests still need a fresh match; enrollment keeps stronger evidence."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from sam.voice.models import ValidatedAudio
from sam.voice_identity.errors import InsufficientSpeechError
from sam.voice_identity.policy import DEFAULT_THRESHOLD
from sam.voice_identity.providers import FakeSpeakerEmbeddingProvider
from tests.test_desktop_identity import SECRET, IdentityBridge
from tests.voice_identity_support import DIM, OWNER_VOICE, Harness


class SpeechAwareFake(FakeSpeakerEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__(dimension=DIM)
        self.minimums: list[float] = []
        self.insufficient = False

    def embed_speech(
        self,
        audio: ValidatedAudio,
        *,
        minimum_speech_seconds: float,
        timeout_seconds: float,
    ) -> Sequence[float]:
        self.minimums.append(minimum_speech_seconds)
        if self.insufficient:
            raise InsufficientSpeechError
        return self.embed(audio, timeout_seconds=timeout_seconds)


def test_enrollment_and_verification_supply_distinct_speech_floors() -> None:
    provider = SpeechAwareFake()
    harness = Harness(embedder=provider)
    harness.enroll_owner(samples=3)
    assert provider.minimums == [2.0, 2.0, 2.0]
    assert (
        harness.verifier.verify(harness.audio(OWNER_VOICE)).result == "owner_verified"
    )
    assert provider.minimums[-1] == 0.25
    assert harness.policy.threshold == DEFAULT_THRESHOLD == 0.5


def test_desktop_uses_shared_default_verification_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SPEAKER_VERIFICATION_THRESHOLD", raising=False)
    bridge = IdentityBridge()
    assert bridge.settings.speaker_verification_threshold == DEFAULT_THRESHOLD == 0.5
    assert bridge.runtime.identity is not None
    assert bridge.runtime.identity.verifier._policy.threshold == DEFAULT_THRESHOLD


def test_desktop_preserves_a_stricter_trusted_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPEAKER_VERIFICATION_THRESHOLD", "0.6")
    bridge = IdentityBridge()
    assert bridge.settings.speaker_verification_threshold == 0.6
    assert bridge.runtime.identity is not None
    assert bridge.runtime.identity.verifier._policy.threshold == 0.6


def test_enrollment_rejects_long_recording_with_too_little_speech() -> None:
    provider = SpeechAwareFake()
    provider.insufficient = True
    harness = Harness(embedder=provider)
    session = harness.enrollment.begin(harness.step_up.mint("enroll"))
    result = harness.enrollment.add_sample(session, harness.audio(OWNER_VOICE))
    assert result.accepted is False
    assert result.reason_code == "audio_too_short"
    assert result.sample_count == 0
    assert provider.call_count == 0
    assert harness.store.load() is None


def test_insufficient_speech_is_not_a_speaker_mismatch() -> None:
    provider = SpeechAwareFake()
    harness = Harness(embedder=provider)
    harness.enroll_owner()
    provider.insufficient = True
    calls = provider.call_count
    result = harness.verifier.verify(harness.audio(OWNER_VOICE))
    assert result.result == "insufficient_audio"
    assert result.reason_code == "audio_too_short"
    assert provider.call_count == calls


@pytest.mark.parametrize("change", ["model_id", "model_revision", "dimension"])
def test_profile_mismatch_requires_reenrollment_and_keeps_old_profile(
    change: str,
) -> None:
    bridge = IdentityBridge()
    bridge.enroll()
    before = bridge.store.load()
    assert before is not None
    assert bridge.get("/voice/identity").json()["profile_compatible"] is True
    if change == "dimension":
        bridge.embedder.dimension += 1
    else:
        setattr(bridge.embedder, change, "new-version")
    response = bridge.get("/voice/identity").json()
    assert response["enrolled"] is True
    assert response["profile_compatible"] is False
    assert response["setup_state"] == "not_enrolled"
    assert bridge.store.load() is before
    utterance = bridge.utter(OWNER_VOICE)
    assert utterance["speaker_result"] == "verification_error"
    assert bridge.agent.messages == []
    begin = bridge.post(
        "/voice/identity/enroll/begin", {"step_up": SECRET, "re_enroll": True}
    ).json()
    assert begin["status"] == "ok"
    assert bridge.store.load() is before
