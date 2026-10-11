"""Reused encoding preserves complete waveforms, bounds and failure handling."""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Any

import pytest

from sam.voice_local import whisper_decode
from sam.voice_local.stt import _supported_language
from tests.whisper_support import FakeWhisper, mock_components, np


def _upstream_recognizer(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run the installed window/segment code with synthetic model outputs."""

    whisper_type = pytest.importorskip("faster_whisper").WhisperModel
    mock_components(monkeypatch)
    _, options, suppressed, pad = whisper_decode._components()

    def tokenizer(_tokenizer: object, _multilingual: bool, **kwargs: Any) -> Any:
        return SimpleNamespace(
            timestamp_begin=1000,
            decode=lambda tokens: {1: " head", 2: " tail"}[tokens[0]],
            **kwargs,
        )

    monkeypatch.setattr(
        whisper_decode, "_components", lambda: (tokenizer, options, suppressed, pad)
    )

    class Recognizer(FakeWhisper):
        frames_per_second = 100
        time_precision = 0.02
        input_stride = 2
        logger = logging.getLogger("test.whisper_windows")

        def __init__(self) -> None:
            super().__init__([("en", 0.8), ("fa", 0.2)])
            self.decode_calls = 0

        def get_prompt(self, *_args: Any, **_kwargs: Any) -> list[int]:
            return []

        def generate_with_fallback(self, *_args: Any) -> tuple[Any, float, int, int]:
            self.decode_calls += 1
            return (
                SimpleNamespace(
                    no_speech_prob=0,
                    sequences_ids=[[self.decode_calls]],
                ),
                -0.1,
                0,
                1,
            )

        def _split_segments_by_timestamps(self, **kwargs: Any) -> Any:
            return whisper_type._split_segments_by_timestamps(self, **kwargs)

        def generate_segments(self, *args: Any, **kwargs: Any) -> Any:
            return whisper_type.generate_segments(self, *args, **kwargs)

    return Recognizer()


@pytest.mark.parametrize("samples", [4_800, 48_000, 480_000])
def test_short_and_full_window_clips_encode_once_without_mutating_waveform(
    samples: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_components(monkeypatch)
    recognizer = FakeWhisper([("fa", 0.2), ("en", 0.8)])
    audio = np.linspace(-0.2, 0.2, samples, dtype=np.float32)
    original = audio.copy()
    result = whisper_decode.decode_bounded_audio(
        recognizer,
        audio,
        language_hint=None,
        language_for=_supported_language,
        deadline=time.monotonic() + 10,
    )
    assert result["text"] == "synthetic transcript"
    assert recognizer.events == ["features", "encode", "detect", "decode"]
    assert recognizer.encoded_inputs[0].shape == (80, 3000)
    frame_count = samples // 160
    expected = np.arange(80 * (frame_count + 1), dtype=np.float32).reshape(80, -1)
    np.testing.assert_array_equal(
        recognizer.encoded_inputs[0][:, :frame_count], expected[:, :-1]
    )
    assert np.count_nonzero(recognizer.encoded_inputs[0][:, frame_count:]) == 0
    np.testing.assert_array_equal(audio, original)
    assert np.count_nonzero(recognizer.feature_buffers[0]) == 0


def test_expired_budget_performs_no_feature_extraction_or_model_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_components(monkeypatch)
    recognizer = FakeWhisper()
    with pytest.raises(TimeoutError):
        whisper_decode.decode_bounded_audio(
            recognizer,
            np.ones(16_000),
            language_hint="en",
            language_for=_supported_language,
            deadline=time.monotonic() - 1,
        )
    assert recognizer.events == []


def test_overlong_input_cannot_start_model_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_components(monkeypatch)
    recognizer = FakeWhisper()
    with pytest.raises(ValueError):
        whisper_decode.decode_bounded_audio(
            recognizer,
            np.ones(960_001),
            language_hint="en",
            language_for=_supported_language,
            deadline=time.monotonic() + 10,
        )
    assert recognizer.events == []


@pytest.mark.parametrize("seconds", [30, 31, 60])
def test_upstream_generator_keeps_complete_tail_and_encodes_each_window_once(
    seconds: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recognizer = _upstream_recognizer(monkeypatch)
    audio = np.linspace(-0.2, 0.2, seconds * 16_000, dtype=np.float32)
    original = audio.copy()
    result = whisper_decode.decode_bounded_audio(
        recognizer,
        audio,
        language_hint=None,
        language_for=_supported_language,
        deadline=time.monotonic() + 10,
    )
    windows = 1 if seconds == 30 else 2
    assert result["text"] == ("head" if windows == 1 else "head tail")
    assert recognizer.decode_calls == windows
    assert recognizer.events.count("encode") == windows
    assert recognizer.events.count("detect") == 1
    frames = seconds * 100
    expected = np.arange(80 * (frames + 1), dtype=np.float32).reshape(80, -1)
    for index, encoded in enumerate(recognizer.encoded_inputs):
        start = index * 3000
        end = min(start + 3000, frames)
        np.testing.assert_array_equal(encoded[:, : end - start], expected[:, start:end])
        assert np.count_nonzero(encoded[:, end - start :]) == 0
    np.testing.assert_array_equal(audio, original)
    assert np.count_nonzero(recognizer.feature_buffers[0]) == 0


def test_deadline_expiry_after_first_window_cannot_return_a_partial_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recognizer = _upstream_recognizer(monkeypatch)
    monkeypatch.setattr(
        "sam.voice_local.whisper_decode.time.monotonic",
        lambda: 20 if recognizer.decode_calls else 0,
    )
    with pytest.raises(TimeoutError):
        whisper_decode.decode_bounded_audio(
            recognizer,
            np.ones(60 * 16_000, dtype=np.float32),
            language_hint=None,
            language_for=_supported_language,
            deadline=10,
        )
    assert recognizer.decode_calls == 1
    assert recognizer.events.count("encode") == 1
    assert np.count_nonzero(recognizer.feature_buffers[0]) == 0


def test_incompatible_feature_shape_is_rejected_and_wiped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_components(monkeypatch)
    recognizer = FakeWhisper()
    recognizer.feature_extractor.nb_max_frames = 2999
    with pytest.raises(ValueError):
        whisper_decode.decode_bounded_audio(
            recognizer,
            np.ones(16_000),
            language_hint="en",
            language_for=_supported_language,
            deadline=time.monotonic() + 10,
        )
    assert recognizer.events == ["features"]
    assert np.count_nonzero(recognizer.feature_buffers[0]) == 0


def test_version_guard_refuses_unreviewed_faster_whisper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    faster_whisper = pytest.importorskip("faster_whisper")
    from sam.voice_identity.errors import ModelSetupError

    monkeypatch.setattr(faster_whisper, "__version__", "unreviewed")
    with pytest.raises(ModelSetupError):
        whisper_decode._components()
