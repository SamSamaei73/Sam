"""Local speech preparation: duration, silence, containment, no real biometrics."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from sam.voice_identity.errors import InsufficientSpeechError
from sam.voice_identity.policy import MIN_VERIFICATION_SECONDS
from sam.voice_local import speaker_audio
from sam.voice_local.speaker import SpeechBrainEcapaEmbeddingProvider
from tests.voice_identity_support import OWNER_VOICE, Harness

np = pytest.importorskip("numpy")


def test_edges_trimmed_with_padding_and_internal_pauses_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = np.arange(96_000, dtype=np.float32) / 96_000
    original = samples.copy()
    work_buffers: list[Any] = []

    def regions(work: Any) -> object:
        work_buffers.append(work)
        return [{"start": 16_000, "end": 40_000}, {"start": 56_000, "end": 80_000}]

    monkeypatch.setattr(speaker_audio, "_speech_regions", regions)
    prepared = speaker_audio.prepare_speaker_waveform(samples, minimum_speech_seconds=2)
    np.testing.assert_array_equal(prepared, original[13_600:82_400])
    np.testing.assert_array_equal(samples, original)
    assert np.count_nonzero(work_buffers[0]) == 0
    assert not np.shares_memory(prepared, samples)


def test_capture_padding_cannot_satisfy_minimum_speech_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = np.ones(8 * 16_000, dtype=np.float32)
    monkeypatch.setattr(
        speaker_audio,
        "_speech_regions",
        lambda _: [{"start": 32_000, "end": 56_000}],  # 1.5 seconds spoken
    )
    assert (
        speaker_audio.prepare_speaker_waveform(samples, minimum_speech_seconds=1).size
        == 28_800
    )
    with pytest.raises(InsufficientSpeechError):
        speaker_audio.prepare_speaker_waveform(samples, minimum_speech_seconds=2)


@pytest.mark.parametrize("speech_samples", [0, 3_999, 4_000, 8_000])
def test_quarter_second_floor_counts_actual_speech_not_padding(
    speech_samples: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    samples = np.linspace(-0.2, 0.2, 80_000, dtype=np.float32)
    start = 32_000
    regions = (
        [{"start": start, "end": start + speech_samples}] if speech_samples else []
    )
    monkeypatch.setattr(speaker_audio, "_speech_regions", lambda _: regions)
    if speech_samples < 4_000:
        with pytest.raises(InsufficientSpeechError):
            speaker_audio.prepare_speaker_waveform(
                samples, minimum_speech_seconds=MIN_VERIFICATION_SECONDS
            )
    else:
        prepared = speaker_audio.prepare_speaker_waveform(
            samples, minimum_speech_seconds=MIN_VERIFICATION_SECONDS
        )
        np.testing.assert_array_equal(
            prepared, samples[start - 2_400 : start + speech_samples + 2_400]
        )
        assert prepared.size == speech_samples + 4_800
        assert not np.shares_memory(prepared, samples)
        # A usable verification clip is still too short for enrollment.
        with pytest.raises(InsufficientSpeechError):
            speaker_audio.prepare_speaker_waveform(samples, minimum_speech_seconds=2)


@pytest.mark.parametrize("minimum", [0.0, 0.249, float("nan"), float("inf"), True])
def test_invalid_speech_floor_is_rejected_before_vad(
    minimum: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(speaker_audio, "_speech_regions", lambda _: calls.append(True))
    with pytest.raises(ValueError, match="^speaker audio is invalid$"):
        speaker_audio.prepare_speaker_waveform(
            np.ones(16_000, dtype=np.float32), minimum_speech_seconds=minimum
        )
    assert calls == []


def test_silence_and_fragmented_speech_do_not_receive_an_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = np.zeros(16_000 * 10, dtype=np.float32)
    for regions in ([], [{"start": 0, "end": 4_000}, {"start": 80_000, "end": 84_000}]):
        monkeypatch.setattr(speaker_audio, "_speech_regions", lambda _, r=regions: r)
        with pytest.raises(InsufficientSpeechError):
            speaker_audio.prepare_speaker_waveform(samples, minimum_speech_seconds=1)


@pytest.mark.parametrize(
    "regions",
    [
        None,
        "private provider output",
        [{"start": -1, "end": 20_000}],
        [{"start": False, "end": 20_000}],
        [{"start": 0, "end": 32_001}],
        [{"start": 10_000, "end": 9_000}],
        [{"start": 0, "end": 20_000}, {"start": 16_000, "end": 30_000}],
    ],
)
def test_malformed_speech_regions_fail_closed(
    regions: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(speaker_audio, "_speech_regions", lambda _: regions)
    with pytest.raises(ValueError, match="^speech regions are invalid$"):
        speaker_audio.prepare_speaker_waveform(
            np.ones(32_000), minimum_speech_seconds=1
        )


def test_vad_failure_wipes_its_copy_and_excludes_content_from_local_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_buffers: list[Any] = []

    def fail(work: Any) -> object:
        work_buffers.append(work)
        raise RuntimeError("provider failure")

    monkeypatch.setattr(speaker_audio, "_speech_regions", fail)
    with pytest.raises(RuntimeError):
        speaker_audio.prepare_speaker_waveform(
            np.ones(32_000), minimum_speech_seconds=1
        )
    assert np.count_nonzero(work_buffers[0]) == 0


def test_local_provider_checks_speech_before_loading_ecapa_and_wipes_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sam.voice_local.speaker as speaker_module

    provider = SpeechBrainEcapaEmbeddingProvider()
    audio = Harness().audio(OWNER_VOICE)
    source = np.ones(32_000, dtype=np.float32)
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace())
    monkeypatch.setattr(speaker_module, "to_16k_numpy", lambda _: source)
    monkeypatch.setattr(speaker_audio, "_speech_regions", lambda _: [])
    loads: list[bool] = []
    monkeypatch.setattr(provider, "_load", lambda: loads.append(True))
    with pytest.raises(InsufficientSpeechError):
        provider.embed_speech(audio, minimum_speech_seconds=2, timeout_seconds=10)
    assert loads == []
    assert np.count_nonzero(source) == 0


def test_preprocessing_is_part_of_the_template_revision() -> None:
    from sam.voice_local.registry import SPEAKER_MODEL

    revision = SpeechBrainEcapaEmbeddingProvider.model_revision
    assert revision.startswith(SPEAKER_MODEL.revision + ":")
    assert speaker_audio.SPEAKER_PREPROCESSING_REVISION in revision


def test_sixty_second_speaker_input_preserves_the_entire_speech_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = np.linspace(-0.3, 0.3, 60 * 16_000, dtype=np.float32)
    work_buffers: list[Any] = []

    def regions(work: Any) -> object:
        work_buffers.append(work)
        return [{"start": 0, "end": work.size}]

    monkeypatch.setattr(speaker_audio, "_speech_regions", regions)
    prepared = speaker_audio.prepare_speaker_waveform(samples, minimum_speech_seconds=1)
    np.testing.assert_array_equal(prepared, samples)
    assert prepared.size == 60 * 16_000
    assert not np.shares_memory(prepared, samples)
    assert np.count_nonzero(work_buffers[0]) == 0


def test_over_limit_speaker_input_is_rejected_before_any_vad_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(speaker_audio, "_speech_regions", lambda _: calls.append(True))
    with pytest.raises(ValueError, match="^speaker audio is invalid$"):
        speaker_audio.prepare_speaker_waveform(
            np.zeros(60 * 16_000 + 1, dtype=np.float32), minimum_speech_seconds=1
        )
    assert calls == []
