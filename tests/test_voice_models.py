"""Voice domain models: limits, shapes, and the pinned authorization invariants."""

from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import ValidationError

from sam.voice import models as m
from sam.voice.audio import validate_audio
from sam.voice.models import (
    AudioFormat,
    AudioMetadata,
    EndSessionRequest,
    StartSessionRequest,
    TranscriptionRequest,
    TranscriptionResult,
    VoiceAuditEvent,
    VoiceErrorCategory,
    VoiceForwardingDecision,
    VoiceIdentityStatus,
    VoiceOperation,
    VoiceProcessingRequest,
    VoiceProcessingResult,
    VoiceProcessingStatus,
)
from tests.voice_support import ALICE, NOW, wav_input

S = VoiceProcessingStatus
DIGEST = "a" * 64


def _meta(**over: Any) -> AudioMetadata:
    fields: dict[str, Any] = dict(
        audio_format=AudioFormat.WAV_PCM16,
        sample_rate=16_000,
        channels=1,
        sample_width_bytes=2,
        frame_count=100,
        duration_seconds=0.1,
        byte_size=244,
        digest_sha256=DIGEST,
    )
    fields.update(over)
    return AudioMetadata(**fields)


def _result(**over: Any) -> VoiceProcessingResult:
    fields: dict[str, Any] = dict(
        session_id="s",
        utterance_id="u",
        principal=ALICE,
        status=S.SUCCEEDED,
        transcript="hi",
        provider_id="fake-stt",
        audio=_meta(),
        forwarding=VoiceForwardingDecision.ELIGIBLE,
        processed_at=NOW,
    )
    fields.update(over)
    return VoiceProcessingResult(**fields)


class TestLimits:
    def test_limits_are_central_positive_and_consistent(self) -> None:
        assert 0 < m.MIN_SAMPLE_RATE < m.MAX_SAMPLE_RATE
        assert m.MAX_CHANNELS >= 1 and m.SUPPORTED_SAMPLE_WIDTH_BYTES == 2
        assert m.MAX_AUDIO_BYTES > 0 and m.MAX_AUDIO_DURATION_SECONDS > 0
        assert m.MAX_TRANSCRIPT_LENGTH > 0 and m.MAX_VOICE_SESSION_UTTERANCES > 0
        assert (
            0
            < m.DEFAULT_VOICE_PROVIDER_TIMEOUT_SECONDS
            <= m.MAX_VOICE_PROVIDER_TIMEOUT_SECONDS
        )
        # the worst legal stream fits in the byte cap
        worst = m.MAX_SAMPLE_RATE * m.MAX_CHANNELS * 2 * m.MAX_AUDIO_DURATION_SECONDS
        assert worst <= m.MAX_AUDIO_BYTES

    def test_audio_metadata_bounds(self) -> None:
        for over in (
            {"sample_rate": m.MAX_SAMPLE_RATE + 1},
            {"sample_rate": m.MIN_SAMPLE_RATE - 1},
            {"channels": 0},
            {"channels": m.MAX_CHANNELS + 1},
            {"sample_width_bytes": 1},
            {"frame_count": 0},
            {"duration_seconds": 0},
            {"duration_seconds": m.MAX_AUDIO_DURATION_SECONDS + 1},
            {"byte_size": m.MAX_AUDIO_BYTES + 1},
            {"digest_sha256": "xyz"},
            {"digest_sha256": "A" * 64},
        ):
            with pytest.raises(ValidationError):
                _meta(**over)

    def test_a_transcription_request_carries_no_timeout(self) -> None:
        assert "timeout_seconds" not in TranscriptionRequest.model_fields
        with pytest.raises(ValidationError):
            TranscriptionRequest.model_validate(
                {
                    "session_id": "s",
                    "utterance_id": "u",
                    "audio": validate_audio(wav_input()),
                    "timeout_seconds": 9999,
                }
            )


class TestTranscriptionResult:
    def test_valid(self) -> None:
        r = TranscriptionResult(
            text="hi", provider_id="p", language="en", reported_confidence=0.5
        )
        assert r.text == "hi"

    @pytest.mark.parametrize(
        "over",
        [
            {"text": ""},
            {"text": "x" * (m.MAX_TRANSCRIPT_LENGTH + 1)},
            {"provider_id": "Bad Id"},
            {"provider_id": ""},
            {"language": "not a tag"},
            {"reported_confidence": 1.1},
            {"reported_confidence": -0.1},
            {"reported_confidence": math.nan},
        ],
    )
    def test_invalid(self, over: dict[str, Any]) -> None:
        fields: dict[str, Any] = dict(text="hi", provider_id="p")
        fields.update(over)
        with pytest.raises(ValidationError):
            TranscriptionResult(**fields)


class TestRequests:
    def test_ids_must_be_safe(self) -> None:
        for bad in ("", "-x", "a b", "a/b", "x" * 101, "é", "../etc"):
            with pytest.raises(ValidationError):
                EndSessionRequest(principal=ALICE, session_id=bad)
            with pytest.raises(ValidationError):
                VoiceProcessingRequest(
                    principal=ALICE, session_id="s", utterance_id=bad, audio=wav_input()
                )

    @pytest.mark.parametrize(
        "field",
        [
            "permission",
            "scope",
            "risk",
            "provider",
            "confirmation_id",
            "authorized",
            "approved",
            "timeout_seconds",
            "provider_id",
        ],
    )
    def test_a_request_cannot_carry_authorization_or_provider_data(
        self, field: str
    ) -> None:
        with pytest.raises(ValidationError):
            VoiceProcessingRequest.model_validate(
                {
                    "principal": ALICE,
                    "session_id": "s",
                    "audio": wav_input(),
                    field: "allow",
                }
            )
        with pytest.raises(ValidationError):
            StartSessionRequest.model_validate({"principal": ALICE, field: "allow"})

    def test_request_fields_are_exactly_the_safe_set(self) -> None:
        assert set(VoiceProcessingRequest.model_fields) == {
            "principal",
            "reason",
            "session_id",
            "utterance_id",
            "audio",
            "identity_signal",
        }

    def test_default_utterance_ids_are_unique(self) -> None:
        a = VoiceProcessingRequest(principal=ALICE, session_id="s", audio=wav_input())
        b = VoiceProcessingRequest(principal=ALICE, session_id="s", audio=wav_input())
        assert a.utterance_id != b.utterance_id

    def test_reason_is_sanitized(self) -> None:
        req = StartSessionRequest(principal=ALICE, reason="  why\x00\x07  ")
        assert req.reason == "why"


class TestResultShape:
    def test_success_needs_transcript_provider_and_audio(self) -> None:
        assert _result().transcript == "hi"
        for over in ({"transcript": None}, {"provider_id": None}, {"audio": None}):
            with pytest.raises(ValidationError):
                _result(**over)

    def test_only_success_carries_a_transcript(self) -> None:
        with pytest.raises(ValidationError):
            _result(
                status=S.FAILED, error_category=VoiceErrorCategory.TRANSCRIPTION_ERROR
            )

    def test_failure_needs_a_category(self) -> None:
        with pytest.raises(ValidationError):
            _result(status=S.FAILED, transcript=None, provider_id=None, audio=None)

    def test_confirmation_required_needs_no_category(self) -> None:
        r = _result(
            status=S.CONFIRMATION_REQUIRED,
            transcript=None,
            provider_id=None,
            audio=None,
            forwarding=VoiceForwardingDecision.NOT_APPLICABLE,
        )
        assert r.error_category is None

    def test_forwarding_invariants(self) -> None:
        # a SUCCEEDED result must be eligible and not secret-flagged
        with pytest.raises(ValidationError):
            _result(forwarding=VoiceForwardingDecision.NOT_APPLICABLE)
        with pytest.raises(ValidationError):
            _result(transcript_secret_like=True)
        # a withheld result is a secret_detected failure with no transcript
        ok = _result(
            status=S.FAILED,
            transcript=None,
            provider_id="fake-stt",
            audio=None,
            error_category=VoiceErrorCategory.SECRET_DETECTED,
            transcript_secret_like=True,
            forwarding=VoiceForwardingDecision.WITHHELD_SECRET_DETECTED,
        )
        assert ok.transcript is None
        for over in (
            {"status": S.SUCCEEDED},
            {"error_category": VoiceErrorCategory.TRANSCRIPTION_ERROR},
            {"transcript_secret_like": False},
            {"transcript": "the secret"},
        ):
            fields: dict[str, Any] = dict(
                status=S.FAILED,
                transcript=None,
                provider_id="fake-stt",
                audio=None,
                error_category=VoiceErrorCategory.SECRET_DETECTED,
                transcript_secret_like=True,
                forwarding=VoiceForwardingDecision.WITHHELD_SECRET_DETECTED,
            )
            fields.update(over)
            with pytest.raises(ValidationError):
                _result(**fields)
        # only a withheld result may carry the secret flag
        with pytest.raises(ValidationError):
            _result(
                status=S.FAILED,
                transcript=None,
                provider_id=None,
                audio=None,
                error_category=VoiceErrorCategory.TRANSCRIPTION_ERROR,
                transcript_secret_like=True,
                forwarding=VoiceForwardingDecision.NOT_APPLICABLE,
            )

    def test_authorization_and_trust_flags_are_pinned(self) -> None:
        assert _result().grants_authorization is False
        assert _result().transcript_untrusted_input is True
        with pytest.raises(ValidationError):
            _result(grants_authorization=True)
        with pytest.raises(ValidationError):
            _result(transcript_untrusted_input=False)

    def test_identity_status_requires_a_check(self) -> None:
        with pytest.raises(ValidationError):
            _result(
                identity_status=VoiceIdentityStatus.VERIFIED, identity_checked=False
            )
        r = _result(identity_status=VoiceIdentityStatus.VERIFIED, identity_checked=True)
        assert r.identity_status is VoiceIdentityStatus.VERIFIED

    def test_naive_timestamps_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _result(processed_at=NOW.replace(tzinfo=None))
        with pytest.raises(ValidationError):
            VoiceAuditEvent(
                event_id="e",
                occurred_at=NOW.replace(tzinfo=None),
                operation=VoiceOperation.START_SESSION,
                principal=ALICE,
                status=S.SUCCEEDED,
            )

    def test_results_are_immutable(self) -> None:
        with pytest.raises(ValidationError):
            _result().status = S.FAILED
