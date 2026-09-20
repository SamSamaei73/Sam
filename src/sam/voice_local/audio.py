"""PCM16 -> 16 kHz mono float32, in memory (torch-free numpy path)."""

from __future__ import annotations

import struct
from typing import Any

from sam.voice.models import ValidatedAudio

TARGET_RATE = 16_000


def to_mono_float(audio: ValidatedAudio) -> tuple[list[float], int]:
    """(samples in [-1, 1] as the first channel, sample_rate)."""

    count = len(audio.pcm) // 2
    samples = struct.unpack(f"<{count}h", audio.pcm[: count * 2])
    channels = max(1, audio.metadata.channels)
    return [s / 32768.0 for s in samples[::channels]], audio.metadata.sample_rate


def to_16k_numpy(audio: ValidatedAudio) -> Any:
    """A float32 numpy array at 16 kHz (linear resample when needed)."""

    import numpy as np

    mono, rate = to_mono_float(audio)
    data: Any = np.asarray(mono, dtype=np.float32)
    if rate != TARGET_RATE and data.size:
        n = max(1, int(round(data.size * TARGET_RATE / rate)))
        if rate > TARGET_RATE:  # cheap anti-alias before decimating
            width = max(1, int(round(rate / TARGET_RATE)))
            kernel = np.ones(width, dtype=np.float32) / width
            data = np.convolve(data, kernel, mode="same")
        data = np.interp(
            np.linspace(0, data.size - 1, n), np.arange(data.size), data
        ).astype(np.float32)
    return data
