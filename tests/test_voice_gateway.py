"""Lifecycle tests for sam.voice.gateway.VoiceGateway against the REAL
PermissionEngine and the fake providers. No test bypasses authorization."""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Any

import pytest

from sam.permissions.models import PermissionAction
from sam.voice.audit import FailingVoiceAuditSink, InMemoryVoiceAuditSink
from sam.voice.gateway import VoiceGateway
from sam.voice.identity import FakeVoiceIdentityProvider
from sam.voice.models import (
    MAX_VOICE_SESSION_UTTERANCES,
    AudioFormat,
    AudioInput,
    PermissionOutcomeSummary,
    VoiceAuditEvent,
    VoiceErrorCategory,
    VoiceForwardingDecision,
    VoiceIdentityStatus,
    VoiceOperation,
    VoiceProcessingStatus,
)
from sam.voice.transcription import FakeTranscriptionProvider
from tests.voice_support import (
    ALICE,
    BOB,
    INJECTION,
    NOW,
    SPOKEN_SECRET,
    VoiceHarness,
    make_wav,
    raw_input,
    wav_input,
)

S = VoiceProcessingStatus
C = VoiceErrorCategory


def _events(h: VoiceHarness) -> list[VoiceAuditEvent]:
    assert isinstance(h.audit, InMemoryVoiceAuditSink)
    return list(h.audit.list_events())


# ===================================================================== happy


class TestHappyPath:
    def test_full_lifecycle(self) -> None:
        h = VoiceHarness()
        sid = h.start()
        result = h.gateway.process(h.request(sid))
        assert result.status is S.SUCCEEDED
        assert result.transcript == "hello world"
        assert h.end(sid).status is S.SUCCEEDED

    def test_result_carries_provenance(self) -> None:
        h = VoiceHarness()
        sid = h.start()
        result = h.gateway.process(h.request(sid, utterance_id="utt-1"))
        assert (result.session_id, result.utterance_id) == (sid, "utt-1")
        assert result.provider_id == "fake-stt"
        assert result.principal == ALICE
        assert result.processed_at.tzinfo is not None
        assert result.audio is not None
        assert result.audio.audio_format is AudioFormat.WAV_PCM16
        assert result.audio.sample_rate == 16_000 and result.audio.channels == 1
        assert result.audio.digest_sha256
        assert result.transcription_attempted is True
        assert result.transcript_truncated is False
        assert result.permission_outcome is PermissionOutcomeSummary.ALLOW

    def test_identity_is_not_fabricated_when_not_checked(self) -> None:
        h = VoiceHarness()
        sid = h.start()
        result = h.gateway.process(h.request(sid))
        assert result.identity_checked is False
        assert result.identity_status is VoiceIdentityStatus.NOT_CHECKED
        assert result.identity_reason_code is None

    def test_result_never_grants_authorization_and_transcript_is_untrusted(
        self,
    ) -> None:
        h = VoiceHarness()
        sid = h.start()
        result = h.gateway.process(h.request(sid))
        assert result.grants_authorization is False
        assert result.transcript_untrusted_input is True

    def test_secret_like_transcript_is_withheld_not_returned(self) -> None:
        h = VoiceHarness(transcription=FakeTranscriptionProvider(SPOKEN_SECRET))
        sid = h.start()
        result = h.gateway.process(h.request(sid))
        # withheld entirely: no transcript is returned, none can be forwarded
        assert result.status is S.FAILED and result.error_category is C.SECRET_DETECTED
        assert result.transcript is None
        assert result.transcript_secret_like is True
        assert result.forwarding is VoiceForwardingDecision.WITHHELD_SECRET_DETECTED
        assert "sk-ant" not in result.model_dump_json()

    def test_ordinary_transcript_is_not_flagged(self) -> None:
        h = VoiceHarness()
        sid = h.start()
        assert h.gateway.process(h.request(sid)).transcript_secret_like is False

    def test_raw_pcm_and_stereo_are_supported(self) -> None:
        h = VoiceHarness()
        sid = h.start()
        assert (
            h.gateway.process(h.request(sid, audio=raw_input(800, seed=1))).status
            is S.SUCCEEDED
        )
        stereo = wav_input(channels=2, frames=400, seed=2)
        assert h.gateway.process(h.request(sid, audio=stereo)).status is S.SUCCEEDED

    def test_default_utterance_ids_are_unique(self) -> None:
        h = VoiceHarness()
        sid = h.start()
        a = h.gateway.process(h.request(sid, seed=1))
        b = h.gateway.process(h.request(sid, seed=2))
        assert a.utterance_id != b.utterance_id


# =============================================================== permissions


class TestPermissionEngineIsTheAuthority:
    def test_no_grant_denies_and_the_provider_is_never_called(self) -> None:
        h = VoiceHarness(grant_all=False)
        h.grant(PermissionAction.CREATE, "session")  # can start, cannot process
        sid = h.start()
        result = h.gateway.process(h.request(sid))
        assert result.status is S.DENIED
        assert result.error_category is C.PERMISSION_DENIED
        assert result.permission_outcome is PermissionOutcomeSummary.DENY
        assert result.transcription_attempted is False
        assert isinstance(h.stt, FakeTranscriptionProvider) and h.stt.call_count == 0

    def test_starting_a_session_needs_a_grant(self) -> None:
        h = VoiceHarness(grant_all=False)
        from sam.voice.models import StartSessionRequest

        result = h.gateway.start_session(StartSessionRequest(principal=ALICE))
        assert result.status is S.DENIED and result.session_id is None
        assert h.gateway._sessions.open_count() == 0

    def test_ending_a_session_needs_a_grant(self) -> None:
        h = VoiceHarness(grant_all=False)
        h.grant(PermissionAction.CREATE, "session")
        sid = h.start()
        assert h.end(sid).status is S.DENIED
        assert h.gateway._sessions.open_count() == 1

    def test_grant_scoped_to_a_different_session_does_not_authorize(self) -> None:
        h = VoiceHarness(grant_all=False)
        h.grant(PermissionAction.CREATE, "session")
        h.grant(PermissionAction.READ, "session", "some-other-session")
        sid = h.start()
        assert h.gateway.process(h.request(sid)).status is S.DENIED

    def test_session_scoped_grant_authorizes_that_session_only(self) -> None:
        h = VoiceHarness(grant_all=False)
        h.grant(PermissionAction.CREATE, "session")
        a, b = h.start(), h.start()
        h.grant(PermissionAction.READ, "session", a)
        assert h.gateway.process(h.request(a)).status is S.SUCCEEDED
        assert h.gateway.process(h.request(b, seed=5)).status is S.DENIED

    def test_wrong_action_grant_does_not_authorize(self) -> None:
        h = VoiceHarness(grant_all=False)
        h.grant(PermissionAction.CREATE, "session")
        h.grant(PermissionAction.UPDATE, "session")  # not READ
        sid = h.start()
        assert h.gateway.process(h.request(sid)).status is S.DENIED

    def test_other_principal_grant_does_not_authorize(self) -> None:
        h = VoiceHarness(grant_all=False)
        h.grant(PermissionAction.CREATE, "session")
        h.grant(PermissionAction.READ, "session", principal=BOB)
        sid = h.start()
        assert h.gateway.process(h.request(sid)).status is S.DENIED

    def test_revoked_grant_denies(self) -> None:
        h = VoiceHarness(grant_all=False)
        h.grant(PermissionAction.CREATE, "session")
        gid = h.grant(PermissionAction.READ, "session")
        sid = h.start()
        h.pstore.revoke_grant(gid, now=NOW)
        assert h.gateway.process(h.request(sid)).status is S.DENIED

    def test_expired_grant_denies(self) -> None:
        h = VoiceHarness(grant_all=False)
        h.grant(PermissionAction.CREATE, "session")
        h.grant(PermissionAction.READ, "session", expires_at=NOW - timedelta(days=1))
        sid = h.start()
        assert h.gateway.process(h.request(sid)).status is S.DENIED

    def test_permission_engine_failure_fails_closed(self) -> None:
        h = VoiceHarness()

        class Flaky:
            """Delegates to the real engine until told to fail."""

            def __init__(self) -> None:
                self.fail = False

            def evaluate(self, *a: Any, **k: Any) -> Any:
                if self.fail:
                    raise RuntimeError("boom")
                return h.pengine.evaluate(*a, **k)

        engine = Flaky()
        gateway = VoiceGateway(
            permission_engine=engine,  # type: ignore[arg-type]
            transcription_provider=h.stt,
            audit_sink=h.audit,
        )
        from sam.voice.models import StartSessionRequest

        sid = gateway.start_session(StartSessionRequest(principal=ALICE)).session_id
        assert sid is not None
        engine.fail = True
        result = gateway.process(h.request(sid))
        assert result.status is S.FAILED
        assert result.error_category is C.INTERNAL_ERROR
        assert isinstance(h.stt, FakeTranscriptionProvider) and h.stt.call_count == 0

    def test_denied_request_does_not_consume_the_utterance_id(self) -> None:
        h = VoiceHarness(grant_all=False)
        h.grant(PermissionAction.CREATE, "session")
        sid = h.start()
        assert h.gateway.process(h.request(sid, utterance_id="u1")).status is S.DENIED
        h.grant(PermissionAction.READ, "session")
        assert (
            h.gateway.process(h.request(sid, utterance_id="u1")).status is S.SUCCEEDED
        )

    def test_permission_request_uses_the_fixed_mapping(self) -> None:
        h = VoiceHarness()
        sid = h.start()
        h.gateway.process(h.request(sid))
        event = h.permission_audit.list_events()[-1]
        assert event.resource.value == "voice" and event.action is PermissionAction.READ
        assert event.scope_summary == f"session/{sid}"
        assert event.risk.value == "medium"


# ============================================================== confirmation


class TestConfirmation:
    def _confirming(self) -> tuple[VoiceHarness, str]:
        h = VoiceHarness(grant_all=False)
        h.grant(PermissionAction.CREATE, "session")
        h.grant(PermissionAction.READ, "session", always_confirm=True)
        return h, h.start()

    def test_grant_requiring_confirmation_blocks_until_approved(self) -> None:
        h, sid = self._confirming()
        pending = h.gateway.process(h.request(sid, utterance_id="u1"))
        assert pending.status is S.CONFIRMATION_REQUIRED
        assert pending.confirmation_id
        assert isinstance(h.stt, FakeTranscriptionProvider) and h.stt.call_count == 0
        h.approve(pending.confirmation_id)
        done = h.gateway.process(
            h.request(sid, utterance_id="u1"), confirmation_id=pending.confirmation_id
        )
        assert done.status is S.SUCCEEDED
        assert h.stt.call_count == 1

    def test_unapproved_confirmation_denied(self) -> None:
        h, sid = self._confirming()
        pending = h.gateway.process(h.request(sid, utterance_id="u1"))
        result = h.gateway.process(
            h.request(sid, utterance_id="u1"), confirmation_id=pending.confirmation_id
        )
        assert result.status is S.DENIED
        assert result.error_category is C.CONFIRMATION_INVALID

    def test_rejected_confirmation_denied(self) -> None:
        h, sid = self._confirming()
        pending = h.gateway.process(h.request(sid, utterance_id="u1"))
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id, approved=False)
        result = h.gateway.process(
            h.request(sid, utterance_id="u1"), confirmation_id=pending.confirmation_id
        )
        assert result.status is S.DENIED

    def test_confirmation_cannot_be_replayed(self) -> None:
        h, sid = self._confirming()
        pending = h.gateway.process(h.request(sid, utterance_id="u1"))
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        h.gateway.process(
            h.request(sid, utterance_id="u1"), confirmation_id=pending.confirmation_id
        )
        again = h.gateway.process(
            h.request(sid, utterance_id="u2"), confirmation_id=pending.confirmation_id
        )
        assert again.status is S.DENIED

    def test_confirmation_is_bound_to_the_exact_audio(self) -> None:
        h, sid = self._confirming()
        pending = h.gateway.process(h.request(sid, utterance_id="u1", seed=1))
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        swapped = h.gateway.process(
            h.request(sid, utterance_id="u1", seed=2),
            confirmation_id=pending.confirmation_id,
        )
        assert swapped.status is S.DENIED
        assert isinstance(h.stt, FakeTranscriptionProvider) and h.stt.call_count == 0

    def test_confirmation_is_bound_to_the_utterance_id(self) -> None:
        h, sid = self._confirming()
        pending = h.gateway.process(h.request(sid, utterance_id="u1", seed=1))
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        other = h.gateway.process(
            h.request(sid, utterance_id="u2", seed=1),
            confirmation_id=pending.confirmation_id,
        )
        assert other.status is S.DENIED

    def test_confirmation_is_bound_to_the_session(self) -> None:
        h, sid = self._confirming()
        other_sid = h.start()
        pending = h.gateway.process(h.request(sid, utterance_id="u1"))
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        result = h.gateway.process(
            h.request(other_sid, utterance_id="u1"),
            confirmation_id=pending.confirmation_id,
        )
        assert result.status is S.DENIED

    def test_unknown_confirmation_id_denied(self) -> None:
        h, sid = self._confirming()
        result = h.gateway.process(h.request(sid), confirmation_id="does-not-exist")
        assert result.status is S.DENIED


# ================================================ audio validated before all


class TestValidationPrecedesEverything:
    @pytest.mark.parametrize(
        "audio,category",
        [
            (
                AudioInput(
                    content=b"\x00" * (8 * 1024 * 1024 + 1),
                    declared_format=AudioFormat.WAV_PCM16,
                ),
                C.AUDIO_TOO_LARGE,
            ),
            (
                AudioInput(content=b"", declared_format=AudioFormat.WAV_PCM16),
                C.MALFORMED_AUDIO,
            ),
            (
                AudioInput(content=b"not audio", declared_format=AudioFormat.WAV_PCM16),
                C.MALFORMED_AUDIO,
            ),
            (
                AudioInput(
                    content=b"ID3" + b"\x00" * 100,
                    declared_format=AudioFormat.WAV_PCM16,
                ),
                C.UNSUPPORTED_AUDIO,
            ),
            (
                AudioInput(
                    content=make_wav(rate=96_000, frames=100),
                    declared_format=AudioFormat.WAV_PCM16,
                ),
                C.UNSUPPORTED_AUDIO,
            ),
        ],
    )
    def test_invalid_audio_rejected_before_permission_and_provider(
        self, audio: AudioInput, category: VoiceErrorCategory
    ) -> None:
        h = VoiceHarness(
            grant_all=False
        )  # not even permitted: never reaches the engine
        h.grant(PermissionAction.CREATE, "session")
        sid = h.start()
        result = h.gateway.process(h.request(sid, audio=audio))
        assert result.status is S.REJECTED
        assert result.error_category is category
        assert result.permission_outcome is None
        assert result.transcription_attempted is False
        assert isinstance(h.stt, FakeTranscriptionProvider) and h.stt.call_count == 0

    def test_rejected_audio_does_not_consume_the_utterance_slot(self) -> None:
        h = VoiceHarness()
        sid = h.start()
        bad = AudioInput(content=b"junk", declared_format=AudioFormat.WAV_PCM16)
        assert (
            h.gateway.process(h.request(sid, audio=bad, utterance_id="u1")).status
            is S.REJECTED
        )
        assert (
            h.gateway.process(h.request(sid, utterance_id="u1")).status is S.SUCCEEDED
        )


# ==================================================================== sessions


class TestSessionsThroughTheGateway:
    def test_unknown_session_rejected(self) -> None:
        h = VoiceHarness()
        result = h.gateway.process(h.request("no-such-session"))
        assert result.status is S.REJECTED and result.error_category is C.SESSION_ERROR
        assert isinstance(h.stt, FakeTranscriptionProvider) and h.stt.call_count == 0

    def test_closed_session_rejected(self) -> None:
        h = VoiceHarness()
        sid = h.start()
        h.end(sid)
        assert h.gateway.process(h.request(sid)).status is S.REJECTED

    def test_cross_principal_isolation(self) -> None:
        h = VoiceHarness()
        h.grant_all(BOB)
        sid = h.start(ALICE)
        result = h.gateway.process(h.request(sid, principal=BOB))
        assert result.status is S.REJECTED and result.error_category is C.SESSION_ERROR
        assert isinstance(h.stt, FakeTranscriptionProvider) and h.stt.call_count == 0
        assert h.end(sid, BOB).status is S.REJECTED  # cannot end it either
        assert h.gateway.process(h.request(sid)).status is S.SUCCEEDED  # still Alice's

    def test_sessions_are_isolated_from_each_other(self) -> None:
        h = VoiceHarness(
            transcription=FakeTranscriptionProvider(
                lambda r: f"said {r.session_id[:6]}"
            )
        )
        a, b = h.start(), h.start()
        ra = h.gateway.process(h.request(a, utterance_id="same"))
        rb = h.gateway.process(h.request(b, utterance_id="same"))
        assert ra.status is rb.status is S.SUCCEEDED
        assert ra.transcript != rb.transcript
        assert (ra.session_id, rb.session_id) == (a, b)

    def test_duplicate_utterance_id_rejected_without_a_second_provider_call(
        self,
    ) -> None:
        h = VoiceHarness()
        sid = h.start()
        assert (
            h.gateway.process(h.request(sid, utterance_id="u1")).status is S.SUCCEEDED
        )
        dup = h.gateway.process(h.request(sid, utterance_id="u1", seed=9))
        assert dup.status is S.REJECTED and dup.error_category is C.DUPLICATE_UTTERANCE
        assert isinstance(h.stt, FakeTranscriptionProvider) and h.stt.call_count == 1

    def test_session_utterance_bound(self) -> None:
        h = VoiceHarness()
        sid = h.start()
        for i in range(MAX_VOICE_SESSION_UTTERANCES):
            assert (
                h.gateway.process(h.request(sid, utterance_id=f"u{i}", seed=i)).status
                is S.SUCCEEDED
            )
        over = h.gateway.process(h.request(sid, utterance_id="over", seed=999))
        assert over.status is S.REJECTED and over.error_category is C.SESSION_LIMIT
        assert isinstance(h.stt, FakeTranscriptionProvider)
        assert h.stt.call_count == MAX_VOICE_SESSION_UTTERANCES

    def test_the_gateway_owns_its_session_state(self) -> None:
        public = {n for n in dir(VoiceGateway) if not n.startswith("_")}
        assert public == {"process", "start_session", "end_session"}


# ================================================================== provider


class TestProviderTrustBoundary:
    def test_provider_timeout_is_normalized_and_returns_promptly(self) -> None:
        h = VoiceHarness(
            transcription=FakeTranscriptionProvider(delay_seconds=5.0), timeout=0.05
        )
        sid = h.start()
        started = time.monotonic()
        result = h.gateway.process(h.request(sid))
        assert time.monotonic() - started < 1.0
        assert (
            result.status is S.FAILED
            and result.error_category is C.TRANSCRIPTION_TIMEOUT
        )
        assert result.transcription_attempted is True and result.transcript is None

    def test_a_late_result_from_a_non_cooperative_provider_is_discarded(self) -> None:
        provider = FakeTranscriptionProvider(
            "late words", delay_seconds=0.15, honor_timeout=False
        )
        h = VoiceHarness(transcription=provider, timeout=0.05)
        sid = h.start()
        result = h.gateway.process(h.request(sid))
        assert (
            result.status is S.FAILED
            and result.error_category is C.TRANSCRIPTION_TIMEOUT
        )
        assert (
            result.transcript is None and "late words" not in result.model_dump_json()
        )

    def test_provider_exception_is_contained_and_not_leaked(self) -> None:
        h = VoiceHarness(
            transcription=FakeTranscriptionProvider(raises=RuntimeError(SPOKEN_SECRET))
        )
        sid = h.start()
        result = h.gateway.process(h.request(sid))
        assert (
            result.status is S.FAILED and result.error_category is C.TRANSCRIPTION_ERROR
        )
        assert "sk-ant" not in result.model_dump_json()

    @pytest.mark.parametrize(
        "raw,category",
        [
            ("", C.EMPTY_TRANSCRIPT),
            ("   ", C.EMPTY_TRANSCRIPT),
            ("x" * 10_001, C.TRANSCRIPT_TOO_LARGE),
            (b"bytes", C.INVALID_TRANSCRIPT),
            (None, C.INVALID_TRANSCRIPT),
            ({"text": "hi", "risk": "low"}, C.INVALID_TRANSCRIPT),
            ({"text": "hi", "confidence": 9}, C.INVALID_TRANSCRIPT),
            ("a\x00b", C.INVALID_TRANSCRIPT),
        ],
    )
    def test_bad_provider_results_fail_closed(
        self, raw: Any, category: VoiceErrorCategory
    ) -> None:
        h = VoiceHarness(transcription=FakeTranscriptionProvider(result=raw))
        sid = h.start()
        result = h.gateway.process(h.request(sid))
        assert result.status is S.FAILED and result.error_category is category
        assert result.transcript is None

    def test_exactly_one_transcription_call_per_request(self) -> None:
        h = VoiceHarness()
        sid = h.start()
        h.gateway.process(h.request(sid))
        assert isinstance(h.stt, FakeTranscriptionProvider) and h.stt.call_count == 1

    @pytest.mark.parametrize("raw", [RuntimeError("x"), TimeoutError("t")])
    def test_no_hidden_retries_on_failure(self, raw: BaseException) -> None:
        h = VoiceHarness(transcription=FakeTranscriptionProvider(raises=raw))
        sid = h.start()
        h.gateway.process(h.request(sid))
        assert isinstance(h.stt, FakeTranscriptionProvider) and h.stt.call_count == 1

    def test_failed_utterance_slot_stays_consumed_no_silent_retry(self) -> None:
        h = VoiceHarness(
            transcription=FakeTranscriptionProvider(raises=RuntimeError("x"))
        )
        sid = h.start()
        h.gateway.process(h.request(sid, utterance_id="u1"))
        again = h.gateway.process(h.request(sid, utterance_id="u1"))
        assert again.error_category is C.DUPLICATE_UTTERANCE
        assert h.stt.call_count == 1  # type: ignore[attr-defined]

    def test_provider_cannot_choose_scope_risk_or_permission(self) -> None:
        h = VoiceHarness(
            transcription=FakeTranscriptionProvider(
                result={
                    "text": "hi",
                    "scope": "voice",
                    "risk": "low",
                    "permission": "allow",
                }
            )
        )
        sid = h.start()
        assert (
            h.gateway.process(h.request(sid)).status is S.FAILED
        )  # unknown keys rejected
        # And what the engine saw was fixed by policy, not by the provider.
        event = h.permission_audit.list_events()[-1]
        assert event.scope_summary == f"session/{sid}" and event.risk.value == "medium"

    def test_provider_result_cannot_mutate_session_policy(self) -> None:
        h = VoiceHarness(
            transcription=FakeTranscriptionProvider({"text": "hi", "session": "x"})
        )
        sid = h.start()
        h.gateway.process(h.request(sid, utterance_id="u1"))
        info = h.gateway._sessions.info(sid, ALICE)
        assert info.utterance_count == 1  # only the reservation the gateway made

    def test_hostile_transcript_is_returned_verbatim_as_text(self) -> None:
        h = VoiceHarness(transcription=FakeTranscriptionProvider(INJECTION))
        sid = h.start()
        result = h.gateway.process(h.request(sid))
        assert result.status is S.SUCCEEDED and result.transcript == INJECTION

    def test_provider_id_in_result_is_the_configured_one(self) -> None:
        h = VoiceHarness(transcription=FakeTranscriptionProvider(provider_id="my-stt"))
        sid = h.start()
        assert h.gateway.process(h.request(sid)).provider_id == "my-stt"

    def test_gateway_rejects_a_bad_provider_id_or_timeout(self) -> None:
        h = VoiceHarness()

        class Bad:
            provider_id = "Bad Id"

            def transcribe(self, request: Any, *, timeout_seconds: float) -> object:
                return "x"

        with pytest.raises(ValueError):
            VoiceGateway(permission_engine=h.pengine, transcription_provider=Bad())
        for timeout in (0, -1, 31, float("inf")):
            with pytest.raises(ValueError):
                VoiceGateway(
                    permission_engine=h.pengine,
                    transcription_provider=h.stt,
                    provider_timeout_seconds=timeout,
                )

    def test_provider_gets_the_gateway_owned_timeout(self) -> None:
        h = VoiceHarness(timeout=1.25)
        sid = h.start()
        h.gateway.process(h.request(sid))
        assert h.stt.seen_timeouts == [1.25]  # type: ignore[attr-defined]

    def test_no_background_work_exists_or_remains_after_a_result(self) -> None:
        before = threading.active_count()
        h = VoiceHarness()
        sid = h.start()
        h.gateway.process(h.request(sid))
        time.sleep(0.05)
        assert threading.active_count() == before
        calls = h.stt.call_count  # type: ignore[attr-defined]
        time.sleep(0.1)
        assert h.stt.call_count == calls  # type: ignore[attr-defined]


# ==================================================================== identity


class TestIdentityThroughTheGateway:
    def _with_identity(self, provider: FakeVoiceIdentityProvider) -> VoiceHarness:
        return VoiceHarness(identity=provider)

    @pytest.mark.parametrize(
        "status",
        [
            VoiceIdentityStatus.VERIFIED,
            VoiceIdentityStatus.NOT_VERIFIED,
            VoiceIdentityStatus.UNKNOWN,
        ],
    )
    def test_provider_status_is_reported_as_a_signal(
        self, status: VoiceIdentityStatus
    ) -> None:
        h = self._with_identity(FakeVoiceIdentityProvider(status, 0.5))
        sid = h.start()
        result = h.gateway.process(h.request(sid))
        assert result.status is S.SUCCEEDED
        assert result.identity_checked is True and result.identity_status is status

    def test_identity_provider_exception_is_contained_and_degrades_to_unknown(
        self,
    ) -> None:
        h = self._with_identity(
            FakeVoiceIdentityProvider(raises=RuntimeError("template 42"))
        )
        sid = h.start()
        result = h.gateway.process(h.request(sid))
        assert result.status is S.SUCCEEDED
        assert result.identity_status is VoiceIdentityStatus.UNKNOWN
        assert result.identity_reason_code == "identity_error"
        assert "template" not in result.model_dump_json()

    def test_malformed_identity_assessment_degrades_to_unknown(self) -> None:
        h = self._with_identity(
            FakeVoiceIdentityProvider(result={"status": "authorized"})
        )
        sid = h.start()
        result = h.gateway.process(h.request(sid))
        assert result.identity_status is VoiceIdentityStatus.UNKNOWN

    def test_identity_is_not_assessed_if_transcription_fails(self) -> None:
        provider = FakeVoiceIdentityProvider()
        h = VoiceHarness(
            identity=provider,
            transcription=FakeTranscriptionProvider(raises=RuntimeError("x")),
        )
        sid = h.start()
        h.gateway.process(h.request(sid))
        assert provider.call_count == 0

    def test_identity_is_not_assessed_when_denied(self) -> None:
        provider = FakeVoiceIdentityProvider()
        h = VoiceHarness(identity=provider, grant_all=False)
        h.grant(PermissionAction.CREATE, "session")
        sid = h.start()
        assert h.gateway.process(h.request(sid)).status is S.DENIED
        assert provider.call_count == 0

    def test_presented_signal_for_this_utterance_is_accepted(self) -> None:
        provider = FakeVoiceIdentityProvider(VoiceIdentityStatus.NOT_VERIFIED, 0.2)
        h = self._with_identity(provider)
        sid = h.start()
        request = h.request(sid, utterance_id="u1", seed=4)
        from sam.voice.audio import validate_audio

        assert h.identity is not None
        signal = h.identity.assess(sid, "u1", validate_audio(request.audio))
        calls_before = provider.call_count
        result = h.gateway.process(
            h.request(sid, utterance_id="u1", seed=4, identity_signal=signal)
        )
        assert result.status is S.SUCCEEDED
        assert result.identity_status is VoiceIdentityStatus.NOT_VERIFIED
        assert (
            provider.call_count == calls_before
        )  # the presented signal was used, not re-assessed

    def _signal(self, h: VoiceHarness, sid: str, uid: str, seed: int) -> Any:
        from sam.voice.audio import validate_audio

        assert h.identity is not None
        return h.identity.assess(sid, uid, validate_audio(wav_input(seed=seed)))

    def test_signal_from_session_a_rejected_in_session_b(self) -> None:
        h = self._with_identity(FakeVoiceIdentityProvider())
        a, b = h.start(), h.start()
        signal = self._signal(h, a, "u1", 1)
        result = h.gateway.process(
            h.request(b, utterance_id="u1", seed=1, identity_signal=signal)
        )
        assert (
            result.status is S.REJECTED
            and result.error_category is C.IDENTITY_SIGNAL_INVALID
        )
        assert h.stt.call_count == 0  # type: ignore[attr-defined]

    def test_signal_for_utterance_a_rejected_for_utterance_b(self) -> None:
        h = self._with_identity(FakeVoiceIdentityProvider())
        sid = h.start()
        signal = self._signal(h, sid, "utt-a", 1)
        result = h.gateway.process(
            h.request(sid, utterance_id="utt-b", seed=1, identity_signal=signal)
        )
        assert (
            result.status is S.REJECTED
            and result.error_category is C.IDENTITY_SIGNAL_INVALID
        )

    def test_signal_for_different_audio_rejected(self) -> None:
        h = self._with_identity(FakeVoiceIdentityProvider())
        sid = h.start()
        signal = self._signal(h, sid, "u1", 1)
        result = h.gateway.process(
            h.request(sid, utterance_id="u1", seed=2, identity_signal=signal)
        )
        assert result.status is S.REJECTED

    def test_replayed_signal_rejected(self) -> None:
        h = self._with_identity(FakeVoiceIdentityProvider())
        sid = h.start()
        signal = self._signal(h, sid, "u1", 1)
        assert (
            h.gateway.process(
                h.request(sid, utterance_id="u1", seed=1, identity_signal=signal)
            ).status
            is S.SUCCEEDED
        )
        # a second utterance that reuses the consumed signal is refused
        replay = h.gateway.process(
            h.request(sid, utterance_id="u1", seed=1, identity_signal=signal)
        )
        assert replay.status is S.REJECTED

    def test_presented_signal_without_an_identity_service_rejected(self) -> None:
        withid = self._with_identity(FakeVoiceIdentityProvider())
        sid_a = withid.start()
        signal = self._signal(withid, sid_a, "u1", 1)
        plain = VoiceHarness()
        sid = plain.start()
        result = plain.gateway.process(
            plain.request(sid, utterance_id="u1", seed=1, identity_signal=signal)
        )
        assert (
            result.status is S.REJECTED
            and result.error_category is C.IDENTITY_SIGNAL_INVALID
        )

    def test_denied_request_does_not_consume_a_presented_signal(self) -> None:
        h = VoiceHarness(identity=FakeVoiceIdentityProvider(), grant_all=False)
        h.grant(PermissionAction.CREATE, "session")
        sid = h.start()
        signal = self._signal(h, sid, "u1", 1)
        assert (
            h.gateway.process(
                h.request(sid, utterance_id="u1", seed=1, identity_signal=signal)
            ).status
            is S.DENIED
        )
        h.grant(PermissionAction.READ, "session")
        assert (
            h.gateway.process(
                h.request(sid, utterance_id="u1", seed=1, identity_signal=signal)
            ).status
            is S.SUCCEEDED
        )

    def test_forged_signal_rejected(self) -> None:
        h = self._with_identity(
            FakeVoiceIdentityProvider(VoiceIdentityStatus.NOT_VERIFIED, 0.0)
        )
        sid = h.start()
        signal = self._signal(h, sid, "u1", 1)
        forged = signal.model_copy(update={"status": VoiceIdentityStatus.VERIFIED})
        result = h.gateway.process(
            h.request(sid, utterance_id="u1", seed=1, identity_signal=forged)
        )
        assert result.status is S.REJECTED


# ===================================================================== audit


class TestAudit:
    def test_one_event_per_call_on_every_path(self) -> None:
        h = VoiceHarness()
        sid = h.start()  # 1
        h.gateway.process(h.request(sid))  # 2 success
        h.gateway.process(h.request("nope"))  # 3 session error
        h.gateway.process(
            h.request(
                sid,
                audio=AudioInput(content=b"x", declared_format=AudioFormat.WAV_PCM16),
            )
        )  # 4 invalid audio
        h.end(sid)  # 5
        assert len(_events(h)) == 5

    def test_event_carries_safe_metadata(self) -> None:
        h = VoiceHarness(identity=FakeVoiceIdentityProvider())
        sid = h.start()
        h.gateway.process(h.request(sid, utterance_id="u1"))
        event = _events(h)[-1]
        assert event.operation is VoiceOperation.PROCESS_UTTERANCE
        assert (event.session_id, event.utterance_id) == (sid, "u1")
        assert event.status is S.SUCCEEDED
        assert event.audio_format is AudioFormat.WAV_PCM16
        assert event.audio_bytes and event.audio_duration_seconds
        assert event.provider_id == "fake-stt"
        assert event.identity_status is VoiceIdentityStatus.VERIFIED
        assert event.transcription_attempted is True
        assert event.authorization_outcome is PermissionOutcomeSummary.ALLOW
        assert event.risk is not None and event.duration_ms >= 0

    def test_identity_status_is_only_recorded_when_checked(self) -> None:
        h = VoiceHarness()
        sid = h.start()
        h.gateway.process(h.request(sid))
        assert _events(h)[-1].identity_status is None

    def test_denied_event_records_the_outcome_and_no_provider(self) -> None:
        h = VoiceHarness(grant_all=False)
        h.grant(PermissionAction.CREATE, "session")
        sid = h.start()
        h.gateway.process(h.request(sid))
        event = _events(h)[-1]
        assert event.status is S.DENIED
        assert event.authorization_outcome is PermissionOutcomeSummary.DENY
        assert event.transcription_attempted is False and event.provider_id is None

    def test_failing_audit_sink_never_changes_the_result(self) -> None:
        good, bad = VoiceHarness(), VoiceHarness(audit_sink=FailingVoiceAuditSink())
        assert good.gateway.process(good.request(good.start())).status is S.SUCCEEDED
        assert bad.gateway.process(bad.request(bad.start())).status is S.SUCCEEDED

    def test_failing_audit_sink_cannot_turn_a_denial_into_an_allow(self) -> None:
        h = VoiceHarness(audit_sink=FailingVoiceAuditSink(), grant_all=False)
        h.grant(PermissionAction.CREATE, "session")
        sid = h.start()
        assert h.gateway.process(h.request(sid)).status is S.DENIED

    def test_gateway_without_an_audit_sink_works(self) -> None:
        h = VoiceHarness()
        gateway = VoiceGateway(
            permission_engine=h.pengine, transcription_provider=h.stt
        )
        from sam.voice.models import StartSessionRequest

        sid = gateway.start_session(StartSessionRequest(principal=ALICE)).session_id
        assert sid is not None
