"""Interactive speech has one bounded decoder pass and a small CPU budget."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from sam.voice.audio import validate_audio
from sam.voice.models import AudioFormat, AudioInput, TranscriptionRequest
from sam.voice_identity.errors import ModelSetupError
from sam.voice_local import stt
from tests.voice_support import make_wav
from tests.whisper_support import FakeWhisper, mock_components, np


def test_wake_and_owner_requests_have_one_greedy_decoder_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[dict[str, Any]] = []
    recognizers: list[FakeWhisper] = []
    pcm_buffers: list[Any] = []

    def pcm(_audio: object) -> Any:
        buffer = np.ones(32_000, dtype=np.float32)
        pcm_buffers.append(buffer)
        return buffer

    class Recognizer(FakeWhisper):
        def __init__(self, path: str, **kwargs: Any) -> None:
            super().__init__()
            loads.append(kwargs)
            recognizers.append(self)

    monkeypatch.setitem(
        sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Recognizer)
    )
    monkeypatch.setattr(stt, "require_verified", lambda *args: None)
    monkeypatch.setattr(stt, "to_16k_numpy", pcm)
    mock_components(monkeypatch)
    audio = validate_audio(
        AudioInput(
            content=make_wav(frames=32000), declared_format=AudioFormat.WAV_PCM16
        )
    )
    provider = stt.LocalWhisperTranscriptionProvider()
    for session in ("wake", "owner-conversation"):
        result = provider.transcribe(
            TranscriptionRequest(
                session_id=session, utterance_id="test", audio=audio, language_hint="en"
            ),
            timeout_seconds=10,
        )
        assert isinstance(result, dict) and result["text"] == "synthetic transcript"

    assert len(loads) == 1  # Both paths share one loaded model.
    assert loads[0]["cpu_threads"] == 2
    assert loads[0]["num_workers"] == 1
    assert loads[0]["local_files_only"] is True
    assert recognizers[0].events == ["features", "encode", "decode"] * 2
    assert all(np.count_nonzero(buffer) == 0 for buffer in pcm_buffers)
    for options in recognizers[0].options:
        assert options.beam_size == options.best_of == 1
        assert options.temperatures == [0.0]
        assert options.without_timestamps is True
        assert options.no_speech_threshold == 0.6
        assert options.log_prob_threshold == -1.0


@pytest.mark.parametrize("failure", [ValueError, TimeoutError, ModelSetupError])
def test_failed_decode_wipes_converted_pcm(
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception],
) -> None:
    buffer = np.ones(32_000, dtype=np.float32)
    provider = stt.LocalWhisperTranscriptionProvider()
    monkeypatch.setattr(provider, "_load", lambda: object())
    monkeypatch.setattr(stt, "to_16k_numpy", lambda _: buffer)

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise failure

    monkeypatch.setattr(stt, "decode_bounded_audio", fail)
    audio = validate_audio(
        AudioInput(
            content=make_wav(frames=32_000), declared_format=AudioFormat.WAV_PCM16
        )
    )
    with pytest.raises(failure):
        provider.transcribe(
            TranscriptionRequest(session_id="owner", utterance_id="test", audio=audio),
            timeout_seconds=10,
        )
    assert np.count_nonzero(buffer) == 0
