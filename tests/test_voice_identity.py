"""Voice identity: a bound, signed, one-time authentication *signal*."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

import sam.voice.identity as identity_module
from sam.voice.audio import validate_audio
from sam.voice.errors import VoiceIdentityError
from sam.voice.identity import FakeVoiceIdentityProvider, VoiceIdentityService
from sam.voice.models import (
    MAX_IDENTITY_SIGNAL_AGE_SECONDS,
    VoiceIdentitySignal,
    VoiceIdentityStatus,
)
from tests.voice_support import NOW, wav_input

AUDIO = validate_audio(wav_input(seed=1))
OTHER_AUDIO = validate_audio(wav_input(seed=2))
DIGEST = AUDIO.metadata.digest_sha256


class Clock:
    def __init__(self) -> None:
        self.now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _service(provider: Any = None, clock: Clock | None = None) -> VoiceIdentityService:
    return VoiceIdentityService(
        provider or FakeVoiceIdentityProvider(), clock=clock or Clock()
    )


def _verify(
    svc: VoiceIdentityService, signal: VoiceIdentitySignal, **over: str
) -> None:
    ctx = dict(session_id="s1", utterance_id="u1", audio_digest=DIGEST)
    ctx.update(over)
    svc.verify_and_consume(signal, **ctx)


class TestSignal:
    def test_signal_is_bound_to_its_context(self) -> None:
        svc = _service()
        sig = svc.assess("s1", "u1", AUDIO)
        assert (sig.session_id, sig.utterance_id, sig.audio_digest) == (
            "s1",
            "u1",
            DIGEST,
        )
        assert sig.provider_id == "fake-speaker"
        assert sig.status is VoiceIdentityStatus.VERIFIED
        assert len(sig.signature) == 64

    def test_signature_is_not_in_repr(self) -> None:
        sig = _service().assess("s1", "u1", AUDIO)
        assert sig.signature not in repr(sig) and sig.signature not in str(sig)

    def test_signal_has_no_biometric_or_authorization_field(self) -> None:
        assert set(VoiceIdentitySignal.model_fields) == {
            "signal_id",
            "status",
            "session_id",
            "utterance_id",
            "audio_digest",
            "provider_id",
            "issued_at",
            "reported_confidence",
            "signature",
        }

    def test_each_signal_has_a_fresh_id(self) -> None:
        svc = _service()
        assert (
            svc.assess("s", "u", AUDIO).signal_id
            != svc.assess("s", "u", AUDIO).signal_id
        )


class TestVerification:
    def test_accepted_for_exact_context_once(self) -> None:
        svc = _service()
        _verify(svc, svc.assess("s1", "u1", AUDIO))

    def test_replay_rejected(self) -> None:
        svc = _service()
        sig = svc.assess("s1", "u1", AUDIO)
        _verify(svc, sig)
        with pytest.raises(VoiceIdentityError):
            _verify(svc, sig)

    def test_signal_from_session_a_rejected_in_session_b(self) -> None:
        svc = _service()
        sig = svc.assess("session-a", "u1", AUDIO)
        with pytest.raises(VoiceIdentityError):
            _verify(svc, sig, session_id="session-b")

    def test_signal_for_utterance_a_rejected_for_utterance_b(self) -> None:
        svc = _service()
        sig = svc.assess("s1", "utt-a", AUDIO)
        with pytest.raises(VoiceIdentityError):
            _verify(svc, sig, utterance_id="utt-b")

    def test_signal_for_other_audio_rejected(self) -> None:
        svc = _service()
        sig = svc.assess("s1", "u1", AUDIO)
        with pytest.raises(VoiceIdentityError):
            _verify(svc, sig, audio_digest=OTHER_AUDIO.metadata.digest_sha256)

    def test_mismatched_context_does_not_consume_the_signal(self) -> None:
        svc = _service()
        sig = svc.assess("s1", "u1", AUDIO)
        with pytest.raises(VoiceIdentityError):
            _verify(svc, sig, session_id="other")
        _verify(svc, sig)  # the rightful holder can still use it, once

    def test_expired_signal_rejected(self) -> None:
        clock = Clock()
        svc = _service(clock=clock)
        sig = svc.assess("s1", "u1", AUDIO)
        clock.advance(MAX_IDENTITY_SIGNAL_AGE_SECONDS + 1)
        with pytest.raises(VoiceIdentityError):
            _verify(svc, sig)

    def test_age_boundary_is_inclusive(self) -> None:
        clock = Clock()
        svc = _service(clock=clock)
        sig = svc.assess("s1", "u1", AUDIO)
        clock.advance(MAX_IDENTITY_SIGNAL_AGE_SECONDS)
        _verify(svc, sig)

    def test_future_dated_signal_rejected(self) -> None:
        clock = Clock()
        svc = _service(clock=clock)
        sig = svc.assess("s1", "u1", AUDIO)
        clock.advance(-60)
        with pytest.raises(VoiceIdentityError):
            _verify(svc, sig)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("status", VoiceIdentityStatus.NOT_VERIFIED),
            ("reported_confidence", 0.1),
            ("session_id", "s-forged"),
            ("utterance_id", "u-forged"),
            ("audio_digest", "0" * 64),
            ("provider_id", "other-provider"),
            ("signal_id", "forged-id"),
        ],
    )
    def test_tampered_signal_rejected(self, field: str, value: Any) -> None:
        svc = _service()
        sig = svc.assess("s1", "u1", AUDIO)
        forged = sig.model_copy(update={field: value})
        with pytest.raises(VoiceIdentityError):
            _verify(
                svc,
                forged,
                **(
                    {field: value}
                    if field in {"session_id", "utterance_id", "audio_digest"}
                    else {}
                ),
            )

    def test_upgrading_a_signal_to_verified_is_impossible(self) -> None:
        svc = _service(FakeVoiceIdentityProvider(VoiceIdentityStatus.NOT_VERIFIED, 0.0))
        sig = svc.assess("s1", "u1", AUDIO)
        forged = sig.model_copy(
            update={"status": VoiceIdentityStatus.VERIFIED, "reported_confidence": 1.0}
        )
        with pytest.raises(VoiceIdentityError):
            _verify(svc, forged)

    def test_signal_from_another_service_key_rejected(self) -> None:
        sig = _service().assess("s1", "u1", AUDIO)
        with pytest.raises(VoiceIdentityError):
            _verify(_service(), sig)

    def test_hand_built_signal_without_the_key_rejected(self) -> None:
        svc = _service()
        forged = VoiceIdentitySignal(
            signal_id="x",
            status=VoiceIdentityStatus.VERIFIED,
            session_id="s1",
            utterance_id="u1",
            audio_digest=DIGEST,
            provider_id="fake-speaker",
            issued_at=NOW,
            reported_confidence=1.0,
            signature="a" * 64,
        )
        with pytest.raises(VoiceIdentityError):
            _verify(svc, forged)

    def test_replay_ledger_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(identity_module, "MAX_TRACKED_IDENTITY_SIGNALS", 5)
        svc = _service()
        for _ in range(20):
            _verify(svc, svc.assess("s1", "u1", AUDIO))
        assert len(svc._consumed) <= 5


class TestProvider:
    def test_verified_high_confidence_is_just_a_signal(self) -> None:
        svc = _service(FakeVoiceIdentityProvider(VoiceIdentityStatus.VERIFIED, 1.0))
        sig = svc.assess("s", "u", AUDIO)
        assert (
            sig.status is VoiceIdentityStatus.VERIFIED
            and sig.reported_confidence == 1.0
        )

    @pytest.mark.parametrize(
        "status", [VoiceIdentityStatus.NOT_VERIFIED, VoiceIdentityStatus.UNKNOWN]
    )
    def test_other_statuses(self, status: VoiceIdentityStatus) -> None:
        assert (
            _service(FakeVoiceIdentityProvider(status, None))
            .assess("s", "u", AUDIO)
            .status
            is status
        )

    def test_string_status_is_normalized(self) -> None:
        assert (
            _service(FakeVoiceIdentityProvider("verified", 0.5))
            .assess("s", "u", AUDIO)
            .status
            is VoiceIdentityStatus.VERIFIED
        )

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "verified",
            5,
            [],
            {"status": "verified", "extra": 1},
            {"status": "not_checked"},
            {"status": "authorized"},
            {"status": "verified", "confidence": 1.5},
            {"status": "verified", "confidence": float("nan")},
            {"status": "verified", "confidence": True},
            {"status": "verified", "confidence": "high"},
            {"confidence": 1.0},
        ],
    )
    def test_malformed_provider_assessments_rejected(self, raw: Any) -> None:
        provider = FakeVoiceIdentityProvider(
            result=raw if raw is not None else object()
        )
        with pytest.raises(VoiceIdentityError):
            _service(provider).assess("s", "u", AUDIO)

    def test_provider_cannot_return_not_checked(self) -> None:
        with pytest.raises(VoiceIdentityError):
            _service(FakeVoiceIdentityProvider(VoiceIdentityStatus.NOT_CHECKED)).assess(
                "s", "u", AUDIO
            )

    def test_provider_exception_is_contained(self) -> None:
        provider = FakeVoiceIdentityProvider(raises=RuntimeError("template leak 123"))
        with pytest.raises(VoiceIdentityError) as info:
            _service(provider).assess("s", "u", AUDIO)
        assert "template" not in str(info.value) and info.value.__cause__ is None

    def test_cooperative_provider_timeout_is_contained(self) -> None:
        provider = FakeVoiceIdentityProvider(delay_seconds=5.0)
        svc = VoiceIdentityService(provider, timeout_seconds=0.05, clock=Clock())
        with pytest.raises(VoiceIdentityError):
            svc.assess("s", "u", AUDIO)

    def test_late_assessment_is_discarded(self) -> None:
        provider = FakeVoiceIdentityProvider(delay_seconds=0.15, honor_timeout=False)
        svc = VoiceIdentityService(provider, timeout_seconds=0.05, clock=Clock())
        with pytest.raises(VoiceIdentityError):
            svc.assess("s", "u", AUDIO)

    def test_provider_is_given_the_service_owned_timeout(self) -> None:
        seen: list[float] = []

        class Recording:
            provider_id = "rec-speaker"

            def assess(self, request: Any, *, timeout_seconds: float) -> object:
                seen.append(timeout_seconds)
                return {"status": "verified"}

        VoiceIdentityService(Recording(), timeout_seconds=1.5, clock=Clock()).assess(
            "s", "u", AUDIO
        )
        assert seen == [1.5]

    def test_timeout_must_be_within_the_bound(self) -> None:
        for bad in (0, -1, 31, float("inf")):
            with pytest.raises(ValueError):
                VoiceIdentityService(FakeVoiceIdentityProvider(), timeout_seconds=bad)

    def test_provider_id_is_validated(self) -> None:
        with pytest.raises(ValueError):
            FakeVoiceIdentityProvider(provider_id="Bad Id")

    def test_service_repr_hides_the_key(self) -> None:
        svc = VoiceIdentityService(FakeVoiceIdentityProvider(), key=b"K" * 32)
        assert "KKKK" not in repr(svc)
        assert "K" * 8 not in str(svc)
