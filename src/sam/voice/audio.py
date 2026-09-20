"""Structural audio validation. Pure, bounded, standard-library only.

Accepted encodings (see ``sam.voice.models.AudioFormat``):

* ``wav_pcm16``   — a RIFF/WAVE container holding 16-bit linear PCM
* ``raw_pcm16le`` — headerless 16-bit little-endian PCM

There is deliberately **no codec support**: no MP3/AAC/Opus/FLAC/Ogg
decoding, no ffmpeg, no subprocess, no system codec, no native binary.
Compressed input is rejected, never converted. Because the only accepted
payload is uncompressed PCM, there is nothing to decompress and therefore no
decoder/decompression-bomb surface; the byte cap is checked *before* any
parsing.

A filename or ``label`` is never consulted. The declared format is checked
against the actual bytes: a declared WAV that is not a RIFF/WAVE container,
or a declared raw stream that starts with a known container/codec signature,
is rejected.
"""

from __future__ import annotations

import hashlib
import struct

from sam.voice.errors import (
    AudioDurationError,
    AudioTooLargeError,
    MalformedAudioError,
    UnsupportedAudioError,
)
from sam.voice.models import (
    MAX_AUDIO_BYTES,
    MAX_AUDIO_DURATION_SECONDS,
    MAX_CHANNELS,
    MAX_SAMPLE_RATE,
    MAX_WAV_CHUNKS,
    MIN_SAMPLE_RATE,
    SUPPORTED_SAMPLE_WIDTH_BYTES,
    AudioFormat,
    AudioInput,
    AudioMetadata,
    ValidatedAudio,
)

_PCM_TAG = 1
_FMT_SIZES = (16, 18)

# Leading signatures of formats we explicitly do not decode.
_FOREIGN_PREFIXES: tuple[bytes, ...] = (
    b"ID3",
    b"OggS",
    b"fLaC",
    b"FORM",  # AIFF / IFF
    b"\x1a\x45\xdf\xa3",  # Matroska / WebM
    b"#!AMR",
    b"\xff\xfb",  # MPEG audio frame syncs
    b"\xff\xf3",
    b"\xff\xf2",
    b"\xff\xfa",
)


def _looks_foreign(data: bytes) -> bool:
    if data[:4] == b"RIFF" or data[4:8] == b"ftyp":
        return True
    return data.startswith(_FOREIGN_PREFIXES)


def _u32(data: bytes, offset: int) -> int:
    value: int = struct.unpack_from("<I", data, offset)[0]
    return value


def validate_audio(audio: AudioInput) -> ValidatedAudio:
    """Validate untrusted audio and return measured metadata + PCM bytes.

    Raises a typed ``VoiceError`` subclass (fail closed) and never includes
    audio content or caller-supplied text in a message.
    """

    data = audio.content
    if len(data) == 0:
        raise MalformedAudioError("audio is empty")
    if len(data) > MAX_AUDIO_BYTES:
        raise AudioTooLargeError("audio exceeds the size limit")

    if audio.declared_format is AudioFormat.WAV_PCM16:
        if audio.sample_rate is not None or audio.channels is not None:
            # The container is the only source of truth for its own format.
            raise UnsupportedAudioError("stream parameters are not allowed for a WAV")
        rate, channels, pcm = _parse_wav(data)
        audio_format = AudioFormat.WAV_PCM16
    else:
        rate, channels, pcm = _parse_raw(audio, data)
        audio_format = AudioFormat.RAW_PCM16LE

    frame_size = channels * SUPPORTED_SAMPLE_WIDTH_BYTES
    if len(pcm) == 0 or len(pcm) % frame_size != 0:
        raise MalformedAudioError("audio length is inconsistent")
    frames = len(pcm) // frame_size
    duration = frames / rate
    if duration > MAX_AUDIO_DURATION_SECONDS:
        raise AudioDurationError("audio exceeds the duration limit")

    metadata = AudioMetadata(
        audio_format=audio_format,
        sample_rate=rate,
        channels=channels,
        sample_width_bytes=SUPPORTED_SAMPLE_WIDTH_BYTES,
        frame_count=frames,
        duration_seconds=duration,
        byte_size=len(data),
        digest_sha256=hashlib.sha256(data).hexdigest(),
    )
    return ValidatedAudio(metadata=metadata, pcm=pcm)


def _parse_raw(audio: AudioInput, data: bytes) -> tuple[int, int, bytes]:
    rate, channels = audio.sample_rate, audio.channels
    if rate is None or channels is None:
        raise MalformedAudioError("raw PCM requires a sample rate and channel count")
    if not MIN_SAMPLE_RATE <= rate <= MAX_SAMPLE_RATE:
        raise UnsupportedAudioError("sample rate is not supported")
    if not 1 <= channels <= MAX_CHANNELS:
        raise UnsupportedAudioError("channel count is not supported")
    if _looks_foreign(data):
        # Declared as raw PCM but the bytes are a container/codec stream.
        raise UnsupportedAudioError("audio is not raw PCM")
    return rate, channels, data


def _parse_wav(data: bytes) -> tuple[int, int, bytes]:
    if data[:4] != b"RIFF":
        if _looks_foreign(data):
            raise UnsupportedAudioError("audio is not a PCM WAV")
        raise MalformedAudioError("audio is not a WAV container")
    if len(data) < 12:
        raise MalformedAudioError("WAV header is truncated")
    if data[8:12] != b"WAVE":
        raise UnsupportedAudioError("RIFF container is not WAVE")
    if _u32(data, 4) + 8 != len(data):
        # Covers truncation, trailing bytes, and a lying size field.
        raise MalformedAudioError("declared WAV size does not match the data")

    position = 12
    chunks = 0
    fmt: tuple[int, int] | None = None  # (sample_rate, channels)
    payload: tuple[int, int] | None = None  # (offset, size)
    while position < len(data):
        chunks += 1
        if chunks > MAX_WAV_CHUNKS:
            raise MalformedAudioError("WAV has too many chunks")
        if position + 8 > len(data):
            raise MalformedAudioError("WAV chunk header is truncated")
        chunk_id = data[position : position + 4]
        size = _u32(data, position + 4)
        body = position + 8
        if size > len(data) - body:
            raise MalformedAudioError("WAV chunk is truncated")
        if chunk_id == b"fmt ":
            if fmt is not None:
                raise MalformedAudioError("WAV has duplicate fmt chunks")
            fmt = _parse_fmt(data, body, size)
        elif chunk_id == b"data":
            if fmt is None:
                raise MalformedAudioError("WAV data precedes fmt")
            if payload is not None:
                raise MalformedAudioError("WAV has duplicate data chunks")
            payload = (body, size)
        position = body + size + (size & 1)  # chunks are word-aligned
    if fmt is None or payload is None:
        raise MalformedAudioError("WAV is missing fmt or data")
    rate, channels = fmt
    offset, size = payload
    return rate, channels, data[offset : offset + size]


def _parse_fmt(data: bytes, body: int, size: int) -> tuple[int, int]:
    if size not in _FMT_SIZES:
        raise UnsupportedAudioError("WAV fmt chunk is not plain PCM")
    tag, channels, rate, byte_rate, block_align, bits = struct.unpack_from(
        "<HHIIHH", data, body
    )
    if size == 18 and struct.unpack_from("<H", data, body + 16)[0] != 0:
        raise MalformedAudioError("WAV fmt extension is inconsistent")
    if tag != _PCM_TAG:
        raise UnsupportedAudioError("only uncompressed PCM is supported")
    if bits != SUPPORTED_SAMPLE_WIDTH_BYTES * 8:
        raise UnsupportedAudioError("only 16-bit samples are supported")
    if channels == 0:
        raise MalformedAudioError("WAV declares zero channels")
    if channels > MAX_CHANNELS:
        raise UnsupportedAudioError("channel count is not supported")
    if not MIN_SAMPLE_RATE <= rate <= MAX_SAMPLE_RATE:
        raise UnsupportedAudioError("sample rate is not supported")
    if block_align != channels * SUPPORTED_SAMPLE_WIDTH_BYTES or byte_rate != (
        rate * block_align
    ):
        raise MalformedAudioError("WAV fmt fields are inconsistent")
    return rate, channels


__all__ = ["validate_audio"]
