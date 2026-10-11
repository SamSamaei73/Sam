"""Complete, bounded long questions through the trusted desktop voice path."""

import base64

import pytest

from sam.voice.models import TranscriptionRequest
from tests.test_desktop_identity import IdentityBridge
from tests.voice_identity_support import OTHER_VOICE, OWNER_VOICE
from tests.voice_support import make_wav


@pytest.mark.parametrize("guest", [False, True])
def test_complete_sixty_second_turn_reaches_stt_with_a_trusted_deadline(
    guest: bool,
) -> None:
    app = IdentityBridge()
    app.enroll()
    if guest:
        assert app.start_guest()["status"] == "ok"
    audio = app.clip(OTHER_VOICE if guest else OWNER_VOICE, 60 * 16_000)
    original = base64.b64decode(audio)
    seen_durations: list[float] = []

    def recognize(request: TranscriptionRequest) -> str:
        seen_durations.append(request.audio.metadata.duration_seconds)
        assert len(request.audio.pcm) == 60 * 16_000 * 2
        assert request.audio.pcm[-128:] == original[-128:]
        return "The complete long question includes this final sentence."

    app.voice_stt._result = recognize
    before = app.embedder.call_count
    result = app.post("/voice/utterance", {"audio_base64": audio}).json()
    assert result["status"] == "ok"
    assert result["speaker"] == ("guest" if guest else "owner")
    assert result["reply"]
    assert seen_durations == [60]
    assert app.embedder.call_count == before + 1
    assert app.voice_stt.seen_timeouts[-1] == 30.0


def test_audio_above_sixty_seconds_cannot_reach_embedding_stt_or_model() -> None:
    app = IdentityBridge()
    app.enroll()
    before_embedding = app.embedder.call_count
    before_stt = app.voice_stt.call_count
    audio = base64.b64encode(make_wav(frames=60 * 16_000 + 1)).decode()
    result = app.post("/voice/utterance", {"audio_base64": audio}).json()
    assert result["status"] == "denied"
    assert result["transcript"] is None and result["reply"] is None
    assert app.embedder.call_count == before_embedding
    assert app.voice_stt.call_count == before_stt
    assert app.agent.messages == []
