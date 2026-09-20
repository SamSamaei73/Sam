"""Validation of generated audio. Provider output is untrusted external data.

Pure and standard-library only: nothing is decoded, played, converted, or
executed, and no ffmpeg/subprocess/system player is involved. Phase 10
requests MP3 and checks (1) non-empty, (2) the byte cap *as bytes arrive*
(see ``fish_audio``) and again here, (3) that the payload is not an
HTML/JSON/text error body posing as audio, and (4) a structural MP3
signature: a well-formed ID3v2 header followed by data, or an MPEG audio
frame-sync header. The SHA-256 digest is computed by Sam over the exact bytes
returned — never taken from the provider.
"""

from __future__ import annotations

import hashlib
import struct

from sam.tts.errors import TTSInvalidAudioError, TTSOutputTooLargeError
from sam.tts.models import MAX_TTS_AUDIO_BYTES, TTSAudioFormat


def _looks_like_text_body(data: bytes) -> bool:
    head = data[:64].lstrip(b"\xef\xbb\xbf \t\r\n")
    return head[:1] in (b"<", b"{", b"[") or head[:5].lower() == b"error"


def _valid_mp3_signature(data: bytes) -> bool:
    if data[:3] == b"ID3":
        if len(data) < 11 or data[3] == 0xFF or data[4] == 0xFF:
            return False
        size_bytes = data[6:10]
        if any(b & 0x80 for b in size_bytes):  # syncsafe integer
            return False
        # Audio (or at least some payload) must follow the ID3 header.
        return len(data) > 10
    if len(data) >= 4 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        version = (data[1] >> 3) & 0x03
        layer = (data[1] >> 1) & 0x03
        # Reserved version (01) is invalid, and MP3 is MPEG Layer III (01):
        # Layer I/II or the reserved layer (00) is not what was requested.
        return version != 0x01 and layer == 0x01
    return False


def _valid_wav(data: bytes) -> bool:
    """A canonical PCM WAV as produced by Sam: RIFF/WAVE, 16-bit mono PCM,
    a fmt chunk then a data chunk whose declared length matches the payload."""

    if len(data) <= 44 or data[:4] != b"RIFF" or data[8:16] != b"WAVEfmt ":
        return False
    audio_format, channels, _rate, _byte_rate, _align, bits = struct.unpack(
        "<HHIIHH", data[20:36]
    )
    if (audio_format, channels, bits) != (1, 1, 16) or data[36:40] != b"data":
        return False
    (declared,) = struct.unpack("<I", data[40:44])
    return bool(declared == len(data) - 44 and declared % 2 == 0)


def validate_audio_bytes(data: object, fmt: TTSAudioFormat) -> tuple[bytes, str]:
    """Return ``(audio_bytes, sha256_hex)`` or raise a typed error."""

    if not isinstance(data, bytes):
        raise TTSInvalidAudioError("provider audio is not bytes")
    if len(data) == 0:
        raise TTSInvalidAudioError("provider returned no audio")
    if len(data) > MAX_TTS_AUDIO_BYTES:
        raise TTSOutputTooLargeError("generated audio exceeds the size limit")
    if _looks_like_text_body(data):
        raise TTSInvalidAudioError("provider returned a text body, not audio")
    if fmt is TTSAudioFormat.MP3 and not _valid_mp3_signature(data):
        raise TTSInvalidAudioError("provider audio has no valid MP3 signature")
    if fmt is TTSAudioFormat.WAV and not _valid_wav(data):
        raise TTSInvalidAudioError("provider audio is not a valid PCM WAV")
    return data, hashlib.sha256(data).hexdigest()


__all__ = ["validate_audio_bytes"]
