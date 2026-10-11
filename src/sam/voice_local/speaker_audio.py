"""Shared enrollment/verification preparation, entirely local and ephemeral.

Silero VAD is bundled in the pinned faster-whisper installation. Its CPU
session uses one thread and performs no model download. Only outer silence
is removed; sentence-internal pauses and 150 ms around speech are preserved.
The duration floor counts unpadded speech regions, not recording length.
"""

from __future__ import annotations

import math
from typing import Any

from sam.voice.models import MAX_AUDIO_DURATION_SECONDS
from sam.voice_identity.errors import InsufficientSpeechError
from sam.voice_identity.policy import MIN_VERIFICATION_SECONDS
from sam.voice_local.audio import TARGET_RATE

SPEAKER_PREPROCESSING_REVISION = "silero-v6-edges-v1"
SPEECH_PADDING_SAMPLES = int(0.15 * TARGET_RATE)


def _speech_regions(samples: Any) -> object:
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    return get_speech_timestamps(
        samples,
        VadOptions(
            threshold=0.5,
            min_speech_duration_ms=100,
            min_silence_duration_ms=300,
            speech_pad_ms=0,
        ),
        sampling_rate=TARGET_RATE,
    )


def prepare_speaker_waveform(samples: Any, *, minimum_speech_seconds: float) -> Any:
    """Return a private copy of the speech span or fail without an embedding."""

    import numpy as np

    if (
        isinstance(minimum_speech_seconds, bool)
        or not math.isfinite(minimum_speech_seconds)
        or not MIN_VERIFICATION_SECONDS
        <= minimum_speech_seconds
        <= MAX_AUDIO_DURATION_SECONDS
        or samples.ndim != 1
        or not 0 < samples.size <= MAX_AUDIO_DURATION_SECONDS * TARGET_RATE
        or not np.isfinite(samples).all()
    ):
        raise ValueError("speaker audio is invalid")
    # Keep provider mutation, including VAD work buffers, off the source audio.
    work = np.asarray(samples, dtype=np.float32).copy()
    try:
        raw_regions = _speech_regions(work)
        if not isinstance(raw_regions, list):
            raise ValueError("speech regions are invalid")
        regions: list[tuple[int, int]] = []
        previous_end = 0
        for region in raw_regions:
            if not isinstance(region, dict):
                raise ValueError("speech regions are invalid")
            start, end = region.get("start"), region.get("end")
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, int)
                or not isinstance(end, int)
                or not previous_end <= start < end <= work.size
            ):
                raise ValueError("speech regions are invalid")
            regions.append((start, end))
            previous_end = end
        speech_samples = sum(end - start for start, end in regions)
        if speech_samples < math.ceil(minimum_speech_seconds * TARGET_RATE):
            raise InsufficientSpeechError
        first = max(0, regions[0][0] - SPEECH_PADDING_SAMPLES)
        last = min(work.size, regions[-1][1] + SPEECH_PADDING_SAMPLES)
        return work[first:last].copy()
    finally:
        work.fill(0)


__all__ = ["SPEAKER_PREPROCESSING_REVISION", "prepare_speaker_waveform"]
