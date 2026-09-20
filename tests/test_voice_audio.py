"""Structural audio validation: hostile and malformed input."""

from __future__ import annotations

import hashlib
import struct
import time
from typing import Any

import pytest
from pydantic import ValidationError

from sam.voice.audio import validate_audio
from sam.voice.errors import (
    AudioDurationError,
    AudioTooLargeError,
    MalformedAudioError,
    UnsupportedAudioError,
    VoiceError,
)
from sam.voice.models import (
    MAX_AUDIO_BYTES,
    MAX_AUDIO_DURATION_SECONDS,
    MAX_AUDIO_METADATA_LENGTH,
    MAX_CHANNELS,
    MAX_SAMPLE_RATE,
    MAX_WAV_CHUNKS,
    MIN_SAMPLE_RATE,
    AudioFormat,
    AudioInput,
    VoiceErrorCategory,
)
from tests.voice_support import make_wav, pcm_ramp, raw_input, wav_input


def wav(**kw: Any) -> AudioInput:
    return wav_input(**kw)


# ================================================================== accepted


class TestAccepted:
    def test_mono_wav(self) -> None:
        v = validate_audio(wav(frames=1600, rate=16_000))
        m = v.metadata
        assert (m.audio_format, m.sample_rate, m.channels) == (
            AudioFormat.WAV_PCM16,
            16_000,
            1,
        )
        assert m.frame_count == 1600
        assert m.duration_seconds == pytest.approx(0.1)
        assert len(v.pcm) == 3200

    def test_stereo_wav(self) -> None:
        v = validate_audio(wav(frames=800, channels=2, rate=8_000))
        assert v.metadata.channels == 2
        assert len(v.pcm) == 800 * 2 * 2

    @pytest.mark.parametrize("rate", [MIN_SAMPLE_RATE, 16_000, 44_100, MAX_SAMPLE_RATE])
    def test_supported_sample_rates(self, rate: int) -> None:
        assert (
            validate_audio(wav(frames=rate // 10, rate=rate)).metadata.sample_rate
            == rate
        )

    def test_digest_is_sha256_of_the_full_input(self) -> None:
        audio = wav(seed=3)
        assert (
            validate_audio(audio).metadata.digest_sha256
            == hashlib.sha256(audio.content).hexdigest()
        )

    def test_different_audio_has_different_digest(self) -> None:
        a = validate_audio(wav(seed=1)).metadata.digest_sha256
        b = validate_audio(wav(seed=2)).metadata.digest_sha256
        assert a != b

    def test_raw_pcm(self) -> None:
        v = validate_audio(raw_input(1600))
        assert v.metadata.audio_format is AudioFormat.RAW_PCM16LE
        assert v.metadata.byte_size == 3200

    def test_wav_with_extra_chunks_and_fmt18_is_accepted(self) -> None:
        audio = wav(extra_chunks=((b"LIST", b"abc"), (b"junk", b"x" * 10)), fmt_size=18)
        assert validate_audio(audio).metadata.frame_count == 1600

    def test_filename_extension_is_never_consulted(self) -> None:
        for label in ("evil.mp3", "x.exe", "audio.ogg", "noextension", "a/b/c.wav"):
            assert validate_audio(wav(label=label)).metadata.audio_format is (
                AudioFormat.WAV_PCM16
            )

    def test_duration_boundary_is_inclusive(self) -> None:
        frames = int(MAX_AUDIO_DURATION_SECONDS * 16_000)
        assert validate_audio(raw_input(frames)).metadata.duration_seconds == (
            MAX_AUDIO_DURATION_SECONDS
        )


# ============================================================= size/duration


class TestLimits:
    def test_oversized_audio_rejected_before_parsing(self) -> None:
        audio = AudioInput(
            content=b"\x00" * (MAX_AUDIO_BYTES + 1),
            declared_format=AudioFormat.WAV_PCM16,
        )
        with pytest.raises(AudioTooLargeError):
            validate_audio(audio)

    def test_size_boundary_passes_the_size_check_then_hits_duration(self) -> None:
        audio = AudioInput(
            content=b"\x00" * MAX_AUDIO_BYTES,
            declared_format=AudioFormat.RAW_PCM16LE,
            sample_rate=16_000,
            channels=1,
        )
        with pytest.raises(AudioDurationError):
            validate_audio(audio)

    def test_excessive_duration_rejected(self) -> None:
        frames = int(MAX_AUDIO_DURATION_SECONDS * 16_000) + 1
        with pytest.raises(AudioDurationError):
            validate_audio(raw_input(frames))
        with pytest.raises(AudioDurationError):
            validate_audio(wav(frames=frames))

    def test_zero_length_audio_rejected(self) -> None:
        for fmt in (AudioFormat.WAV_PCM16, AudioFormat.RAW_PCM16LE):
            with pytest.raises(MalformedAudioError):
                validate_audio(
                    AudioInput(
                        content=b"",
                        declared_format=fmt,
                        sample_rate=16_000 if fmt is AudioFormat.RAW_PCM16LE else None,
                        channels=1 if fmt is AudioFormat.RAW_PCM16LE else None,
                    )
                )

    def test_wav_with_no_samples_rejected(self) -> None:
        with pytest.raises(MalformedAudioError):
            validate_audio(wav(pcm=b""))

    def test_excessive_sample_rate_rejected(self) -> None:
        with pytest.raises(UnsupportedAudioError):
            validate_audio(wav(rate=MAX_SAMPLE_RATE + 1, frames=100))
        with pytest.raises(UnsupportedAudioError):
            validate_audio(raw_input(100, rate=MAX_SAMPLE_RATE + 1))

    def test_too_low_sample_rate_rejected(self) -> None:
        for rate in (0, 1, MIN_SAMPLE_RATE - 1):
            with pytest.raises(UnsupportedAudioError):
                validate_audio(wav(rate=rate, frames=100))

    def test_excessive_channel_count_rejected(self) -> None:
        with pytest.raises(UnsupportedAudioError):
            validate_audio(wav(channels=MAX_CHANNELS + 1, frames=100))
        with pytest.raises(UnsupportedAudioError):
            validate_audio(raw_input(100, channels=MAX_CHANNELS + 1))

    def test_zero_channels_rejected(self) -> None:
        with pytest.raises(MalformedAudioError):
            validate_audio(wav(channels=0, frames=1, pcm=b"\x00\x00"))

    def test_huge_channel_count_in_header_is_bounded(self) -> None:
        with pytest.raises(VoiceError):
            validate_audio(
                wav(channels=65_535, pcm=b"\x00" * 4, byte_rate=1, block_align=4)
            )


# ============================================================ malformed WAV


class TestMalformedWav:
    def test_not_a_wav_at_all(self) -> None:
        for junk in (b"hello world this is not audio", b"\x00" * 64, b"RIF"):
            with pytest.raises(MalformedAudioError):
                validate_audio(
                    AudioInput(content=junk, declared_format=AudioFormat.WAV_PCM16)
                )

    def test_truncated_header(self) -> None:
        with pytest.raises(MalformedAudioError):
            validate_audio(
                AudioInput(
                    content=b"RIFF\x04\x00\x00\x00WA",
                    declared_format=AudioFormat.WAV_PCM16,
                )
            )

    def test_truncated_audio_data(self) -> None:
        good = make_wav(frames=1600)
        for cut in (len(good) - 1, len(good) - 500, 60, 44):
            with pytest.raises(MalformedAudioError):
                validate_audio(
                    AudioInput(
                        content=good[:cut], declared_format=AudioFormat.WAV_PCM16
                    )
                )

    def test_declared_riff_size_too_large_or_too_small(self) -> None:
        good = make_wav(frames=100)
        for delta in (1, -1, 1000, -1000, 2**30):
            declared = (len(good) - 8 + delta) & 0xFFFFFFFF
            with pytest.raises(MalformedAudioError):
                validate_audio(wav_input(frames=100, riff_size=declared))

    def test_declared_data_size_mismatch(self) -> None:
        with pytest.raises(MalformedAudioError):
            validate_audio(wav_input(frames=100, data_size=10_000))
        with pytest.raises(MalformedAudioError):
            validate_audio(wav_input(frames=100, data_size=0xFFFFFFFF))

    def test_data_size_smaller_than_payload_leaves_unparseable_tail(self) -> None:
        with pytest.raises(MalformedAudioError):
            validate_audio(wav_input(frames=100, data_size=100))

    def test_trailing_garbage_rejected(self) -> None:
        good = make_wav(frames=100) + b"garbage"
        with pytest.raises(MalformedAudioError):
            validate_audio(
                AudioInput(content=good, declared_format=AudioFormat.WAV_PCM16)
            )

    def test_inconsistent_fmt_fields(self) -> None:
        with pytest.raises(MalformedAudioError):
            validate_audio(wav_input(frames=100, byte_rate=1))
        with pytest.raises(MalformedAudioError):
            validate_audio(wav_input(frames=100, block_align=3))

    def test_pcm_not_a_whole_number_of_frames(self) -> None:
        with pytest.raises(MalformedAudioError):
            validate_audio(wav_input(channels=2, pcm=b"\x00" * 6))

    def test_odd_byte_length_pcm(self) -> None:
        with pytest.raises(MalformedAudioError):
            validate_audio(wav_input(pcm=b"\x00" * 101))

    def test_missing_fmt_or_data(self) -> None:
        body = b"WAVE" + b"data" + struct.pack("<I", 4) + b"\x00" * 4
        no_fmt = b"RIFF" + struct.pack("<I", len(body)) + body
        with pytest.raises(MalformedAudioError):
            validate_audio(
                AudioInput(content=no_fmt, declared_format=AudioFormat.WAV_PCM16)
            )
        fmt = struct.pack("<HHIIHH", 1, 1, 16_000, 32_000, 2, 16)
        body2 = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
        no_data = b"RIFF" + struct.pack("<I", len(body2)) + body2
        with pytest.raises(MalformedAudioError):
            validate_audio(
                AudioInput(content=no_data, declared_format=AudioFormat.WAV_PCM16)
            )

    def test_duplicate_fmt_or_data_chunks(self) -> None:
        fmt = struct.pack("<HHIIHH", 1, 1, 16_000, 32_000, 2, 16)
        chunk_fmt = b"fmt " + struct.pack("<I", 16) + fmt
        chunk_data = b"data" + struct.pack("<I", 4) + b"\x00" * 4
        for chunks in (
            chunk_fmt + chunk_fmt + chunk_data,
            chunk_fmt + chunk_data + chunk_data,
        ):
            body = b"WAVE" + chunks
            content = b"RIFF" + struct.pack("<I", len(body)) + body
            with pytest.raises(MalformedAudioError):
                validate_audio(
                    AudioInput(content=content, declared_format=AudioFormat.WAV_PCM16)
                )

    def test_data_before_fmt(self) -> None:
        fmt = struct.pack("<HHIIHH", 1, 1, 16_000, 32_000, 2, 16)
        chunks = (
            b"data"
            + struct.pack("<I", 4)
            + b"\x00" * 4
            + b"fmt "
            + struct.pack("<I", 16)
            + fmt
        )
        body = b"WAVE" + chunks
        content = b"RIFF" + struct.pack("<I", len(body)) + body
        with pytest.raises(MalformedAudioError):
            validate_audio(
                AudioInput(content=content, declared_format=AudioFormat.WAV_PCM16)
            )

    def test_chunk_claiming_more_bytes_than_exist(self) -> None:
        fmt = struct.pack("<HHIIHH", 1, 1, 16_000, 32_000, 2, 16)
        chunks = (
            b"fmt "
            + struct.pack("<I", 16)
            + fmt
            + b"LIST"
            + struct.pack("<I", 0xFFFFFFF0)
        )
        body = b"WAVE" + chunks
        content = b"RIFF" + struct.pack("<I", len(body)) + body
        with pytest.raises(MalformedAudioError):
            validate_audio(
                AudioInput(content=content, declared_format=AudioFormat.WAV_PCM16)
            )

    def test_dangling_chunk_header(self) -> None:
        fmt = struct.pack("<HHIIHH", 1, 1, 16_000, 32_000, 2, 16)
        chunks = b"fmt " + struct.pack("<I", 16) + fmt + b"da"
        body = b"WAVE" + chunks
        content = b"RIFF" + struct.pack("<I", len(body)) + body
        with pytest.raises(MalformedAudioError):
            validate_audio(
                AudioInput(content=content, declared_format=AudioFormat.WAV_PCM16)
            )

    def test_too_many_chunks_is_bounded_and_fast(self) -> None:
        extras = tuple((b"junk", b"") for _ in range(MAX_WAV_CHUNKS + 50))
        start = time.monotonic()
        with pytest.raises(MalformedAudioError):
            validate_audio(wav_input(frames=100, extra_chunks=extras))
        assert time.monotonic() - start < 1.0

    def test_many_thousand_tiny_chunks_are_rejected_not_walked(self) -> None:
        extras = tuple((b"junk", b"") for _ in range(20_000))
        start = time.monotonic()
        with pytest.raises(MalformedAudioError):
            validate_audio(wav_input(frames=10, extra_chunks=extras))
        assert time.monotonic() - start < 1.0

    def test_unexpected_fmt_chunk_size_is_rejected(self) -> None:
        fmt = struct.pack("<HHIIHH", 1, 1, 16_000, 32_000, 2, 16) + b"\x00" * 4
        chunks = b"fmt " + struct.pack("<I", 20) + fmt
        chunks += b"data" + struct.pack("<I", 4) + b"\x00" * 4
        body = b"WAVE" + chunks
        content = b"RIFF" + struct.pack("<I", len(body)) + body
        with pytest.raises(UnsupportedAudioError):
            validate_audio(
                AudioInput(content=content, declared_format=AudioFormat.WAV_PCM16)
            )

    def test_riff_but_not_wave(self) -> None:
        content = b"RIFF" + struct.pack("<I", 8) + b"AVI " + b"\x00" * 4
        with pytest.raises(UnsupportedAudioError):
            validate_audio(
                AudioInput(content=content, declared_format=AudioFormat.WAV_PCM16)
            )


# ====================================================== unsupported encodings


class TestUnsupportedEncodings:
    @pytest.mark.parametrize("tag", [0, 2, 3, 6, 7, 0x11, 0x55, 0xFFFE])
    def test_non_pcm_formats_rejected(self, tag: int) -> None:
        with pytest.raises(UnsupportedAudioError):
            validate_audio(wav_input(frames=100, tag=tag))

    @pytest.mark.parametrize("bits", [8, 12, 24, 32, 64])
    def test_non_16_bit_samples_rejected(self, bits: int) -> None:
        with pytest.raises(UnsupportedAudioError):
            validate_audio(
                wav_input(frames=10, bits=bits, pcm=b"\x00" * (10 * (bits // 8)))
            )

    @pytest.mark.parametrize(
        "prefix",
        [
            b"ID3\x04\x00",
            b"OggS\x00\x02",
            b"fLaC\x00",
            b"\xff\xfb\x90\x00",
            b"\x00\x00\x00\x18ftypM4A ",
            b"FORM\x00\x00\x00\x00AIFF",
        ],
    )
    def test_compressed_or_foreign_containers_declared_as_wav(
        self, prefix: bytes
    ) -> None:
        with pytest.raises(UnsupportedAudioError):
            validate_audio(
                AudioInput(
                    content=prefix + b"\x00" * 200,
                    declared_format=AudioFormat.WAV_PCM16,
                )
            )

    @pytest.mark.parametrize(
        "prefix",
        [
            b"ID3\x04\x00",
            b"OggS",
            b"fLaC",
            b"\xff\xfb\x90\x00",
            b"RIFF\x00\x00\x00\x00WAVE",
        ],
    )
    def test_foreign_container_declared_as_raw_pcm(self, prefix: bytes) -> None:
        content = prefix + b"\x00" * (3200 - len(prefix))
        with pytest.raises(UnsupportedAudioError):
            validate_audio(
                AudioInput(
                    content=content,
                    declared_format=AudioFormat.RAW_PCM16LE,
                    sample_rate=16_000,
                    channels=1,
                )
            )

    def test_real_wav_declared_as_raw_is_rejected_not_reinterpreted(self) -> None:
        with pytest.raises(UnsupportedAudioError):
            validate_audio(
                AudioInput(
                    content=make_wav(),
                    declared_format=AudioFormat.RAW_PCM16LE,
                    sample_rate=16_000,
                    channels=1,
                )
            )

    def test_stream_parameters_are_not_accepted_for_a_wav(self) -> None:
        with pytest.raises(UnsupportedAudioError):
            validate_audio(
                AudioInput(
                    content=make_wav(),
                    declared_format=AudioFormat.WAV_PCM16,
                    sample_rate=44_100,
                )
            )

    def test_raw_pcm_requires_rate_and_channels(self) -> None:
        variants: tuple[dict[str, Any], ...] = (
            {},
            {"sample_rate": 16_000},
            {"channels": 1},
        )
        for kwargs in variants:
            with pytest.raises(MalformedAudioError):
                validate_audio(
                    AudioInput(
                        content=pcm_ramp(100),
                        declared_format=AudioFormat.RAW_PCM16LE,
                        **kwargs,
                    )
                )

    def test_raw_pcm_odd_length(self) -> None:
        with pytest.raises(MalformedAudioError):
            validate_audio(
                AudioInput(
                    content=b"\x00" * 101,
                    declared_format=AudioFormat.RAW_PCM16LE,
                    sample_rate=16_000,
                    channels=1,
                )
            )


# ================================================================ metadata


class TestMetadata:
    def test_null_byte_and_control_characters_in_label_rejected(self) -> None:
        for label in ("a\x00b", "a\x07b", "line\nbreak", "tab\there"):
            with pytest.raises(ValidationError):
                AudioInput(
                    content=b"x", declared_format=AudioFormat.WAV_PCM16, label=label
                )

    def test_oversized_label_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AudioInput(
                content=b"x",
                declared_format=AudioFormat.WAV_PCM16,
                label="x" * (MAX_AUDIO_METADATA_LENGTH + 1),
            )
        AudioInput(
            content=b"x",
            declared_format=AudioFormat.WAV_PCM16,
            label="x" * MAX_AUDIO_METADATA_LENGTH,
        )

    def test_unsupported_metadata_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AudioInput(  # type: ignore[call-arg]
                content=b"x", declared_format=AudioFormat.WAV_PCM16, codec="opus"
            )

    def test_unknown_declared_format_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AudioInput(content=b"x", declared_format="mp3")  # type: ignore[arg-type]

    def test_pathological_unicode_label_is_carried_inert(self) -> None:
        label = "é‮\U0001f600" * 20
        assert (
            AudioInput(
                content=b"x", declared_format=AudioFormat.WAV_PCM16, label=label
            ).label
            == label
        )


# ================================================================ privacy


class TestNoContentInReprsOrErrors:
    def test_audio_bytes_never_appear_in_repr(self) -> None:
        marker = b"SECRETPCM!" * 320
        audio = AudioInput(
            content=marker,
            declared_format=AudioFormat.RAW_PCM16LE,
            sample_rate=16_000,
            channels=1,
        )
        validated = validate_audio(audio)
        for text in (repr(audio), str(audio), repr(validated), str(validated)):
            assert "SECRETPCM" not in text
            assert "\\x" not in text

    def test_errors_never_contain_audio_content(self) -> None:
        marker = b"SECRETPCM!" * 10
        audio = AudioInput(content=marker, declared_format=AudioFormat.WAV_PCM16)
        with pytest.raises(VoiceError) as info:
            validate_audio(audio)
        assert "SECRETPCM" not in str(info.value)
        assert info.value.category in (
            VoiceErrorCategory.MALFORMED_AUDIO,
            VoiceErrorCategory.UNSUPPORTED_AUDIO,
        )

    def test_validated_metadata_holds_no_audio_content(self) -> None:
        validated = validate_audio(wav())
        assert "pcm" not in validated.metadata.model_dump()
