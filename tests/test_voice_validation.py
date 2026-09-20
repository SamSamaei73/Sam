"""The transcription provider is untrusted: its result, its exceptions and
its timing are all validated/contained."""

from __future__ import annotations

import time
from typing import Any

import pytest

from sam.voice.audio import validate_audio
from sam.voice.errors import (
    InvalidTranscriptError,
    TranscriptionError,
    TranscriptionTimeoutError,
)
from sam.voice.models import (
    MAX_TRANSCRIPT_LENGTH,
    TranscriptionRequest,
    VoiceErrorCategory,
)
from sam.voice.transcription import (
    FakeTranscriptionProvider,
    require_valid_provider_id,
    transcribe,
)
from sam.voice.validation import validate_transcription_result
from tests.voice_support import INJECTION, SPOKEN_SECRET, wav_input

V = validate_transcription_result


def _request() -> TranscriptionRequest:
    return TranscriptionRequest(
        session_id="s", utterance_id="u", audio=validate_audio(wav_input())
    )


class TestResultValidation:
    def test_plain_string_result(self) -> None:
        r = V("hello world", "fake-stt")
        assert (r.text, r.provider_id, r.language, r.reported_confidence) == (
            "hello world",
            "fake-stt",
            None,
            None,
        )

    def test_mapping_result_with_metadata(self) -> None:
        r = V({"text": "hi", "language": "en-US", "confidence": 0.9}, "p")
        assert (r.language, r.reported_confidence) == ("en-US", 0.9)

    def test_only_surrounding_whitespace_is_stripped_text_is_otherwise_unchanged(
        self,
    ) -> None:
        assert V("  a  b\nc\t d  ", "p").text == "a  b\nc\t d"

    def test_unicode_is_preserved_not_normalized(self) -> None:
        text = "café ‮ rtl \U0001f600 é"
        assert V(text, "p").text == text

    def test_hostile_text_is_returned_verbatim_as_data(self) -> None:
        assert V(INJECTION, "p").text == INJECTION

    @pytest.mark.parametrize("text", ["", "   ", "\n\t "])
    def test_empty_transcript_rejected(self, text: str) -> None:
        with pytest.raises(InvalidTranscriptError) as info:
            V(text, "p")
        assert info.value.category is VoiceErrorCategory.EMPTY_TRANSCRIPT

    def test_oversized_transcript_rejected_not_truncated(self) -> None:
        with pytest.raises(InvalidTranscriptError) as info:
            V("a" * (MAX_TRANSCRIPT_LENGTH + 1), "p")
        assert info.value.category is VoiceErrorCategory.TRANSCRIPT_TOO_LARGE

    def test_size_boundary_is_inclusive(self) -> None:
        assert len(V("a" * MAX_TRANSCRIPT_LENGTH, "p").text) == MAX_TRANSCRIPT_LENGTH

    def test_huge_transcript_rejected_quickly(self) -> None:
        start = time.monotonic()
        with pytest.raises(InvalidTranscriptError):
            V("x" * 5_000_000, "p")
        assert time.monotonic() - start < 1.0

    @pytest.mark.parametrize("bad", ["a\x00b", "a\x07b", "a\x1bb", "\x7f"])
    def test_control_characters_rejected(self, bad: str) -> None:
        with pytest.raises(InvalidTranscriptError):
            V(bad, "p")

    @pytest.mark.parametrize(
        "raw", [None, 5, 1.5, b"bytes", ["a"], object(), ("a",), {1, 2}]
    )
    def test_unexpected_result_shapes_rejected(self, raw: Any) -> None:
        with pytest.raises(InvalidTranscriptError):
            V(raw, "p")

    @pytest.mark.parametrize("text", [None, 5, b"x", ["a"], {"a": 1}])
    def test_non_text_transcript_field_rejected(self, text: Any) -> None:
        with pytest.raises(InvalidTranscriptError):
            V({"text": text}, "p")

    def test_missing_text_rejected(self) -> None:
        with pytest.raises(InvalidTranscriptError):
            V({"language": "en"}, "p")

    @pytest.mark.parametrize(
        "extra",
        [{"risk": "low"}, {"scope": "x"}, {"permission": "allow"}, {"timeout": 9999}],
    )
    def test_unknown_result_fields_rejected(self, extra: dict[str, Any]) -> None:
        with pytest.raises(InvalidTranscriptError):
            V({"text": "hi", **extra}, "p")

    @pytest.mark.parametrize(
        "language", [5, ["en"], "x" * 40, "e n", "1234", "en_US!!", ""]
    )
    def test_malformed_language_rejected(self, language: Any) -> None:
        with pytest.raises(InvalidTranscriptError):
            V({"text": "hi", "language": language}, "p")

    @pytest.mark.parametrize(
        "confidence", [float("nan"), float("inf"), -0.1, 1.5, True, "0.9", [1]]
    )
    def test_fake_or_malformed_confidence_rejected(self, confidence: Any) -> None:
        with pytest.raises(InvalidTranscriptError):
            V({"text": "hi", "confidence": confidence}, "p")

    def test_confidence_boundaries(self) -> None:
        assert V({"text": "hi", "confidence": 0}, "p").reported_confidence == 0.0
        assert V({"text": "hi", "confidence": 1}, "p").reported_confidence == 1.0

    def test_errors_never_echo_content(self) -> None:
        for raw in (SPOKEN_SECRET + "\x00", {"text": SPOKEN_SECRET, "risk": "low"}):
            with pytest.raises(InvalidTranscriptError) as info:
                V(raw, "p")
            assert "sk-ant" not in str(info.value)

    def test_repr_of_result_hides_transcript(self) -> None:
        r = V(SPOKEN_SECRET, "p")
        assert SPOKEN_SECRET not in repr(r) and SPOKEN_SECRET not in str(r)
        assert r.text == SPOKEN_SECRET


class TestTranscribe:
    def test_success_is_validated(self) -> None:
        r = transcribe(
            FakeTranscriptionProvider("  hi  "), _request(), timeout_seconds=1.0
        )
        assert r.text == "hi" and r.provider_id == "fake-stt"

    def test_provider_exception_is_contained(self) -> None:
        provider = FakeTranscriptionProvider(raises=RuntimeError(SPOKEN_SECRET))
        with pytest.raises(TranscriptionError) as info:
            transcribe(provider, _request(), timeout_seconds=1.0)
        assert "sk-ant" not in str(info.value) and info.value.__cause__ is None
        assert str(info.value) == "transcription failed"

    def test_provider_reported_timeout_is_a_timeout_error(self) -> None:
        provider = FakeTranscriptionProvider(delay_seconds=5.0)  # cooperative
        started = time.monotonic()
        with pytest.raises(TranscriptionTimeoutError):
            transcribe(provider, _request(), timeout_seconds=0.05)
        assert time.monotonic() - started < 0.5  # it never actually waited 5s

    def test_builtin_timeout_error_from_a_provider_is_normalized(self) -> None:
        provider = FakeTranscriptionProvider(raises=TimeoutError("upstream secret"))
        with pytest.raises(TranscriptionTimeoutError) as info:
            transcribe(provider, _request(), timeout_seconds=1.0)
        assert "upstream" not in str(info.value)

    def test_a_late_result_is_discarded(self) -> None:
        provider = FakeTranscriptionProvider(
            "late text", delay_seconds=0.15, honor_timeout=False
        )
        with pytest.raises(TranscriptionTimeoutError) as info:
            transcribe(provider, _request(), timeout_seconds=0.05)
        assert "late text" not in str(info.value)

    def test_malformed_provider_result_rejected(self) -> None:
        with pytest.raises(InvalidTranscriptError):
            transcribe(
                FakeTranscriptionProvider(result=b"bytes"),
                _request(),
                timeout_seconds=1.0,
            )

    def test_provider_cannot_choose_the_timeout(self) -> None:
        provider = FakeTranscriptionProvider({"text": "hi", "timeout": 9999})
        with pytest.raises(InvalidTranscriptError):
            transcribe(provider, _request(), timeout_seconds=0.5)
        assert provider.seen_timeouts == [0.5]

    def test_exactly_one_call_and_no_retry_on_failure(self) -> None:
        provider = FakeTranscriptionProvider(raises=RuntimeError("x"))
        with pytest.raises(TranscriptionError):
            transcribe(provider, _request(), timeout_seconds=1.0)
        assert provider.call_count == 1

    def test_fake_records_digest_not_audio(self) -> None:
        provider = FakeTranscriptionProvider()
        request = _request()
        transcribe(provider, request, timeout_seconds=1.0)
        assert provider.seen_digests == [request.audio.metadata.digest_sha256]
        assert request.audio.pcm not in [
            v for v in vars(provider).values() if isinstance(v, bytes)
        ]

    @pytest.mark.parametrize("bad", ["", "Bad", "has space", "x" * 65, 5, None, "1abc"])
    def test_provider_id_validation(self, bad: Any) -> None:
        with pytest.raises(ValueError):
            require_valid_provider_id(bad)
