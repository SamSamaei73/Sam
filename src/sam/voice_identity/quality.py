"""Local audio quality checks (no model, no persistence)."""

from __future__ import annotations

import math
import struct

from sam.voice.models import ValidatedAudio
from sam.voice_identity.policy import MAX_CLIPPED_FRACTION, MIN_RMS_PCM16


def pcm16_stats(audio: ValidatedAudio) -> tuple[float, float]:
    """(rms, clipped_fraction) of the first channel's samples."""

    data = audio.pcm
    count = len(data) // 2
    if count == 0:
        return 0.0, 0.0
    samples = struct.unpack(f"<{count}h", data[: count * 2])
    channels = max(1, audio.metadata.channels)
    mono = samples[::channels]
    rms = math.sqrt(sum(s * s for s in mono) / len(mono))
    clipped = sum(1 for s in mono if abs(s) >= 32000) / len(mono)
    return rms, clipped


def quality_problem(audio: ValidatedAudio) -> str | None:
    """A safe reason code if the audio is unusable for identity, else None."""

    rms, clipped = pcm16_stats(audio)
    if rms < MIN_RMS_PCM16:
        return "audio_too_quiet"
    if clipped > MAX_CLIPPED_FRACTION:
        return "audio_clipped"
    return None


__all__ = ["pcm16_stats", "quality_problem"]
