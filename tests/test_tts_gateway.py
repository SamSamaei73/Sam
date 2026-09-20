"""Lifecycle tests for sam.tts.gateway.TTSGateway against the REAL
PermissionEngine. The Fish path runs over httpx.MockTransport only."""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from sam.permissions.models import PermissionAction, PermissionResource
from sam.tts.audit import FailingTTSAuditSink, InMemoryTTSAuditSink
from sam.tts.gateway import TTSGateway
from sam.tts.models import (
    PermissionOutcomeSummary,
    ProviderSynthesisResult,
    TTSAuditEvent,
    TTSErrorCategory,
    TTSStatus,
)
from sam.tts.provider import FakeSpeechSynthesisProvider
from tests.tts_support import (
    BOB,
    FAKE_KEY,
    MP3,
    NOW,
    SECRET_TEXT,
    Recorder,
    TTSHarness,
    fish_harness,
    profile,
)

S = TTSStatus
C = TTSErrorCategory


def _events(h: TTSHarness) -> list[TTSAuditEvent]:
    assert isinstance(h.audit, InMemoryTTSAuditSink)
    return list(h.audit.list_events())


def _fake(h: TTSHarness) -> FakeSpeechSynthesisProvider:
    assert isinstance(h.provider, FakeSpeechSynthesisProvider)
    return h.provider


# ===================================================================== happy


class TestHappyPath:
    def test_synthesis_returns_validated_audio(self) -> None:
        h = TTSHarness()
        result = h.gateway.synthesize(h.request())
        assert result.status is S.SUCCEEDED and result.audio is not None
        assert result.permission_outcome is PermissionOutcomeSummary.ALLOW
        assert result.provider_call_attempted is True
        assert _fake(h).call_count == 1

    def test_output_model_carries_trusted_provenance(self) -> None:
        h = TTSHarness()
        result = h.gateway.synthesize(h.request())
        audio = result.audio
        assert audio is not None
        assert audio.provider_id == "fake-tts"
        assert audio.trusted_profile_id == "sam_default"
        assert audio.format.value == "mp3"
        assert audio.synthesis_id == result.synthesis_id
        assert audio.created_at.tzinfo is not None

    def test_audio_digest_and_length_match_the_returned_bytes(self) -> None:
        h = TTSHarness()
        audio = h.gateway.synthesize(h.request()).audio
        assert audio is not None
        assert audio.byte_length == len(audio.audio_bytes)
        assert audio.sha256 == hashlib.sha256(audio.audio_bytes).hexdigest()

    def test_the_digest_is_computed_by_sam_not_taken_from_the_provider(self) -> None:
        h = TTSHarness(
            provider=FakeSpeechSynthesisProvider(
                lambda r: ProviderSynthesisResult(
                    audio_bytes=MP3, content_type="audio/mpeg; sha256=" + "0" * 64
                )
            )
        )
        audio = h.gateway.synthesize(h.request()).audio
        assert audio is not None and audio.sha256 == hashlib.sha256(MP3).hexdigest()

    def test_result_never_grants_authorization(self) -> None:
        h = TTSHarness()
        assert h.gateway.synthesize(h.request()).grants_authorization is False

    def test_the_provider_receives_the_trusted_profile_values_and_sams_timeout(
        self,
    ) -> None:
        h = TTSHarness(timeout=3.5)
        h.gateway.synthesize(h.request())
        p = _fake(h)
        assert p.seen_voices == ["abcdef0123456789abcdef0123456789"]
        assert p.seen_models == ["s2.1-pro-free"]
        assert p.seen_timeouts == [3.5]

    def test_the_fish_path_works_end_to_end_over_the_mock_transport(self) -> None:
        h, rec, _ = fish_harness()
        result = h.gateway.synthesize(h.request("Fish please."))
        assert result.status is S.SUCCEEDED and len(rec.requests) == 1
        assert result.audio is not None and result.audio.provider_id == "fish-audio"

    def test_different_profiles_use_different_trusted_voices(self) -> None:
        h, rec, _ = fish_harness()
        h.gateway.synthesize(h.request(profile_id="sam_default"))
        h.gateway.synthesize(h.request(profile_id="other_voice"))
        import json

        voices = [json.loads(r.content)["reference_id"] for r in rec.requests]
        assert voices[0] != voices[1]


# =============================================================== permissions


class TestPermissionEngineIsTheAuthority:
    def test_no_grant_denies_and_makes_zero_provider_calls(self) -> None:
        h = TTSHarness(grant_all=False)
        result = h.gateway.synthesize(h.request())
        assert (
            result.status is S.DENIED and result.error_category is C.PERMISSION_DENIED
        )
        assert result.provider_call_attempted is False
        assert _fake(h).call_count == 0

    def test_deny_makes_zero_fish_requests_and_never_resolves_the_key(self) -> None:
        h, rec, creds = fish_harness(grant_all=False)
        assert h.gateway.synthesize(h.request()).status is S.DENIED
        assert rec.requests == [] and creds.resolve_count == 0

    def test_permission_engine_failure_fails_closed(self) -> None:
        h = TTSHarness()

        class Broken:
            def evaluate(self, *a: Any, **k: Any) -> object:
                raise RuntimeError("boom")

        gateway = TTSGateway(
            permission_engine=Broken(),  # type: ignore[arg-type]
            provider=h.provider,
            profiles=h.profiles,
            audit_sink=h.audit,
        )
        result = gateway.synthesize(h.request())
        assert result.status is S.FAILED and result.error_category is C.INTERNAL_ERROR
        assert _fake(h).call_count == 0

    def test_a_phase9_voice_grant_cannot_authorize_external_synthesis(self) -> None:
        h = TTSHarness(grant_all=False)
        for action in (
            PermissionAction.CREATE,
            PermissionAction.READ,
            PermissionAction.UPDATE,
        ):
            h.grant(
                "sam_default",
                resource=PermissionResource.VOICE,
                action=action,
                segments=("session",),
            )
        h.grant(
            "sam_default",
            resource=PermissionResource.VOICE,
            action=PermissionAction.SEND,
            segments=("fake-tts", "sam_default"),
        )
        result = h.gateway.synthesize(h.request())
        assert result.status is S.DENIED
        assert _fake(h).call_count == 0

    def test_a_grant_for_the_wrong_action_on_the_same_resource_does_not_authorize(
        self,
    ) -> None:
        h = TTSHarness(grant_all=False)
        h.grant("sam_default", action=PermissionAction.READ)
        assert h.gateway.synthesize(h.request()).status is S.DENIED

    def test_cross_profile_scope_isolation(self) -> None:
        h = TTSHarness(grant_all=False)
        h.grant("sam_default")
        assert (
            h.gateway.synthesize(h.request(profile_id="sam_default")).status
            is S.SUCCEEDED
        )
        other = h.gateway.synthesize(h.request(profile_id="other_voice"))
        assert other.status is S.DENIED

    def test_cross_provider_scope_isolation(self) -> None:
        h = TTSHarness(grant_all=False)
        h.grant("sam_default", provider_id="some-other-provider")
        assert h.gateway.synthesize(h.request()).status is S.DENIED

    def test_provider_wide_grant_covers_its_profiles_only(self) -> None:
        h = TTSHarness(grant_all=False)
        h.grant("", segments=("fake-tts",))
        assert (
            h.gateway.synthesize(h.request(profile_id="sam_default")).status
            is S.SUCCEEDED
        )
        assert (
            h.gateway.synthesize(h.request(profile_id="other_voice")).status
            is S.SUCCEEDED
        )

    def test_cross_principal_isolation(self) -> None:
        h = TTSHarness()  # grants are Alice's
        assert h.gateway.synthesize(h.request(principal=BOB)).status is S.DENIED
        assert _fake(h).call_count == 0

    def test_revoked_and_expired_grants_deny(self) -> None:
        h = TTSHarness(grant_all=False)
        gid = h.grant("sam_default")
        h.pstore.revoke_grant(gid, now=NOW)
        assert h.gateway.synthesize(h.request()).status is S.DENIED
        h2 = TTSHarness(grant_all=False)
        h2.grant("sam_default", expires_at=NOW - timedelta(days=1))
        assert h2.gateway.synthesize(h2.request()).status is S.DENIED

    def test_permission_request_uses_only_trusted_profile_data(self) -> None:
        h = TTSHarness()
        h.gateway.synthesize(h.request())
        event = h.permission_audit.list_events()[-1]
        assert event.resource is PermissionResource.SPEECH_SYNTHESIS
        assert event.action is PermissionAction.SEND
        assert event.scope_summary == "fake-tts/sam_default"
        assert event.risk.value == "medium"

    def test_a_denied_request_does_not_consume_the_request_id(self) -> None:
        h = TTSHarness(grant_all=False)
        assert h.gateway.synthesize(h.request(request_id="r1")).status is S.DENIED
        h.grant("sam_default")
        assert h.gateway.synthesize(h.request(request_id="r1")).status is S.SUCCEEDED


# ============================================================== confirmation


class TestConfirmation:
    def _confirming(self) -> TTSHarness:
        h = TTSHarness(grant_all=False)
        h.grant("sam_default", always_confirm=True)
        return h

    def test_a_grant_may_add_confirmation_and_it_blocks_the_provider(self) -> None:
        h = self._confirming()
        pending = h.gateway.synthesize(h.request("hello one", request_id="r1"))
        assert pending.status is S.CONFIRMATION_REQUIRED and pending.confirmation_id
        assert _fake(h).call_count == 0
        h.approve(pending.confirmation_id)
        done = h.gateway.synthesize(
            h.request("hello one", request_id="r1"),
            confirmation_id=pending.confirmation_id,
        )
        assert done.status is S.SUCCEEDED and _fake(h).call_count == 1

    def test_confirmation_is_bound_to_the_exact_text(self) -> None:
        h = self._confirming()
        pending = h.gateway.synthesize(h.request("approved text", request_id="r1"))
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        swapped = h.gateway.synthesize(
            h.request("DIFFERENT text", request_id="r1"),
            confirmation_id=pending.confirmation_id,
        )
        assert swapped.status is S.DENIED and _fake(h).call_count == 0

    def test_confirmation_cannot_be_replayed_or_moved_across_profiles(self) -> None:
        h = TTSHarness(grant_all=False)
        h.grant("sam_default", always_confirm=True)
        h.grant("other_voice", always_confirm=True)
        pending = h.gateway.synthesize(h.request("hi", request_id="r1"))
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        moved = h.gateway.synthesize(
            h.request("hi", profile_id="other_voice", request_id="r2"),
            confirmation_id=pending.confirmation_id,
        )
        assert moved.status is S.DENIED
        ok = h.gateway.synthesize(
            h.request("hi", request_id="r1"), confirmation_id=pending.confirmation_id
        )
        assert ok.status is S.SUCCEEDED
        again = h.gateway.synthesize(
            h.request("hi", request_id="r3"), confirmation_id=pending.confirmation_id
        )
        assert again.status is S.DENIED

    def test_unapproved_and_rejected_confirmations_deny(self) -> None:
        h = self._confirming()
        pending = h.gateway.synthesize(h.request("hi", request_id="r1"))
        assert pending.confirmation_id is not None
        assert (
            h.gateway.synthesize(
                h.request("hi", request_id="r1"),
                confirmation_id=pending.confirmation_id,
            ).status
            is S.DENIED
        )
        h.approve(pending.confirmation_id, approved=False)
        assert (
            h.gateway.synthesize(
                h.request("hi", request_id="r1"),
                confirmation_id=pending.confirmation_id,
            ).status
            is S.DENIED
        )

    def test_confirmation_record_holds_a_digest_never_the_text(self) -> None:
        h = self._confirming()
        h.gateway.synthesize(h.request("very private sentence here"))
        dumped = " ".join(str(r) for r in h.confirmations._list_for_test())
        assert "very private sentence" not in dumped
        assert "synthesize #" in dumped


# ================================================================== profiles


class TestTrustedProfiles:
    def test_unknown_profile_rejected_before_anything_else(self) -> None:
        h = TTSHarness()
        result = h.gateway.synthesize(h.request(profile_id="nonexistent"))
        assert (
            result.status is S.REJECTED and result.error_category is C.UNKNOWN_PROFILE
        )
        assert result.permission_outcome is None and _fake(h).call_count == 0

    def test_disabled_profile_rejected_even_with_a_grant(self) -> None:
        h = TTSHarness()
        result = h.gateway.synthesize(h.request(profile_id="off_voice"))
        assert (
            result.status is S.REJECTED and result.error_category is C.PROFILE_DISABLED
        )
        assert _fake(h).call_count == 0

    def test_a_profile_bound_to_another_provider_cannot_reach_this_one(self) -> None:
        h = TTSHarness(
            extra_profiles=(profile("foreign", provider_id="different-provider"),)
        )
        h.grant("foreign", provider_id="different-provider")
        result = h.gateway.synthesize(h.request(profile_id="foreign"))
        assert result.status is S.REJECTED and _fake(h).call_count == 0


# ================================================================ text checks


class TestTextIsValidatedFirst:
    @pytest.mark.parametrize(
        "text,category",
        [
            ("", C.TEXT_INVALID),
            ("   \n\t ", C.TEXT_INVALID),
            ("a\x00b", C.TEXT_INVALID),
            ("a\x07b", C.TEXT_INVALID),
            ("x" * 5_001, C.TEXT_TOO_LARGE),
            ("é" * 4_999 + "\U0001f600" * 2, C.TEXT_TOO_LARGE),
            ("\ud800", C.TEXT_INVALID),
        ],
    )
    def test_invalid_text_never_reaches_permission_or_provider(
        self, text: str, category: TTSErrorCategory
    ) -> None:
        h = TTSHarness(grant_all=False)  # not even permitted
        result = h.gateway.synthesize(h.request(text))
        assert result.status is S.REJECTED and result.error_category is category
        assert result.permission_outcome is None
        assert _fake(h).call_count == 0 and h.permission_audit.list_events() == ()

    def test_oversized_utf8_is_rejected_even_under_the_character_cap(self) -> None:
        text = "\U0001f600" * 4_500  # 4,500 chars but 18,000 bytes < 20,000: allowed
        h = TTSHarness()
        assert h.gateway.synthesize(h.request(text)).status is S.SUCCEEDED
        too_many_bytes = "\U0001f600" * 5_000  # 5,000 chars, 20,000 bytes: at the limit
        assert h.gateway.synthesize(h.request(too_many_bytes)).status is S.SUCCEEDED
        over = "\U0001f600" * 5_000 + "x"
        assert h.gateway.synthesize(h.request(over)).status is S.REJECTED

    def test_boundary_length_is_allowed(self) -> None:
        h = TTSHarness()
        assert h.gateway.synthesize(h.request("a" * 5_000)).status is S.SUCCEEDED


# ========================================================= secret withholding


class TestSecretTextIsWithheld:
    def test_secret_text_is_rejected_with_zero_provider_calls(self) -> None:
        h = TTSHarness()
        result = h.gateway.synthesize(h.request(SECRET_TEXT))
        assert (
            result.status is S.REJECTED and result.error_category is C.SECRET_DETECTED
        )
        assert result.audio is None and result.provider_call_attempted is False
        assert _fake(h).call_count == 0

    @pytest.mark.parametrize(
        "secret",
        [
            SECRET_TEXT,
            "the key is AKIAABCDEFGHIJKLMNOP okay",
            "token ghp_" + "a" * 30,
            "header Bearer abcdefghijklmnopqrstuv",
            "-----BEGIN RSA PRIVATE KEY----- MIIB",
            "my password is hunter2plus",
        ],
    )
    def test_secret_text_causes_zero_fish_requests_and_no_credential_lookup(
        self, secret: str
    ) -> None:
        h, rec, creds = fish_harness()
        result = h.gateway.synthesize(h.request(secret))
        assert result.error_category is C.SECRET_DETECTED
        assert rec.requests == []  # zero network calls
        assert creds.resolve_count == 0  # key never even resolved

    def test_partially_secret_text_is_withheld_whole_not_redacted(self) -> None:
        h, rec, _ = fish_harness()
        h.gateway.synthesize(
            h.request("Read my notes. My password is hunter2plus. Thanks.")
        )
        assert rec.requests == []

    def test_no_permission_record_is_created_for_secret_text(self) -> None:
        h = TTSHarness()
        h.gateway.synthesize(h.request(SECRET_TEXT))
        assert h.permission_audit.list_events() == ()
        assert h.confirmations._list_for_test() == ()

    def test_the_secret_is_absent_from_every_output(self) -> None:
        h = TTSHarness()
        result = h.gateway.synthesize(h.request(SECRET_TEXT))
        blob = repr(result) + result.model_dump_json()
        blob += "\n".join(e.model_dump_json() for e in _events(h))
        assert "sk-ant" not in blob

    def test_ordinary_text_is_not_withheld(self) -> None:
        h = TTSHarness()
        for text in (
            "open the notes",
            "my password reset email arrived",
            "sk is a prefix",
        ):
            assert h.gateway.synthesize(h.request(text)).status is S.SUCCEEDED, text


# ================================================================== provider


class TestProviderTrustBoundary:
    @pytest.mark.parametrize(
        "provider,category",
        [
            (
                FakeSpeechSynthesisProvider(raises=RuntimeError("boom " + FAKE_KEY)),
                C.PROVIDER_ERROR,
            ),
            (FakeSpeechSynthesisProvider(delay_seconds=99.0), C.TIMEOUT),
            (
                FakeSpeechSynthesisProvider(result=b"not a result object"),
                C.INVALID_AUDIO,
            ),
            (FakeSpeechSynthesisProvider(result={"audio_bytes": MP3}), C.INVALID_AUDIO),
            (
                FakeSpeechSynthesisProvider(
                    result=ProviderSynthesisResult(audio_bytes=b"")
                ),
                C.INVALID_AUDIO,
            ),
            (
                FakeSpeechSynthesisProvider(
                    result=ProviderSynthesisResult(audio_bytes=b"<html>err</html>")
                ),
                C.INVALID_AUDIO,
            ),
            (
                FakeSpeechSynthesisProvider(
                    result=ProviderSynthesisResult(audio_bytes=b'{"error":"x"}')
                ),
                C.INVALID_AUDIO,
            ),
            (
                FakeSpeechSynthesisProvider(
                    result=ProviderSynthesisResult(audio_bytes=b"RIFF....WAVEfmt ")
                ),
                C.INVALID_AUDIO,
            ),
            (
                FakeSpeechSynthesisProvider(
                    result=ProviderSynthesisResult(
                        audio_bytes=b"\x00" * (10 * 1024 * 1024 + 1)
                    )
                ),
                C.OUTPUT_TOO_LARGE,
            ),
        ],
    )
    def test_bad_provider_behaviour_fails_closed_with_a_generic_category(
        self, provider: FakeSpeechSynthesisProvider, category: TTSErrorCategory
    ) -> None:
        h = TTSHarness(provider=provider, timeout=0.2)
        result = h.gateway.synthesize(h.request())
        assert result.status is S.FAILED and result.error_category is category
        assert result.audio is None and FAKE_KEY not in result.model_dump_json()

    def test_a_late_result_is_discarded(self) -> None:
        provider = FakeSpeechSynthesisProvider(delay_seconds=0.15, honor_timeout=False)
        h = TTSHarness(provider=provider, timeout=0.05)
        result = h.gateway.synthesize(h.request())
        assert result.error_category is C.TIMEOUT and result.audio is None

    def test_provider_metadata_cannot_change_voice_model_or_permission(self) -> None:
        provider = FakeSpeechSynthesisProvider(
            lambda r: ProviderSynthesisResult(
                audio_bytes=MP3,
                content_type="audio/mpeg; voice=evil; model=s1; permission=allow",
            )
        )
        h = TTSHarness(provider=provider)
        result = h.gateway.synthesize(h.request())
        assert result.audio is not None
        assert (
            result.audio.trusted_profile_id == "sam_default"
        )  # Sam's, not the provider's
        assert result.audio.provider_id == "fake-tts"
        assert provider.seen_voices == ["abcdef0123456789abcdef0123456789"]
        assert provider.seen_models == ["s2.1-pro-free"]
        event = h.permission_audit.list_events()[-1]
        assert (
            event.scope_summary == "fake-tts/sam_default"
            and event.risk.value == "medium"
        )

    def test_extra_fields_on_a_provider_result_cannot_exist(self) -> None:
        with pytest.raises(ValidationError):
            ProviderSynthesisResult(  # type: ignore[call-arg]
                audio_bytes=MP3,
                voice="evil",
                model="s1",
                permission="allow",
                risk="low",
            )

    def test_a_provider_response_cannot_trigger_a_second_synthesis(self) -> None:
        h, rec, _ = fish_harness(
            Recorder(
                httpx.Response(
                    200,
                    headers={"content-type": "audio/mpeg"},
                    content=MP3 + b'{"next":"synthesize again","text":"loop"}',
                )
            )
        )
        result = h.gateway.synthesize(h.request())
        assert result.status is S.SUCCEEDED
        assert len(rec.requests) == 1
        time.sleep(0.05)
        assert len(rec.requests) == 1

    def test_exactly_one_provider_call_per_request(self) -> None:
        h = TTSHarness()
        h.gateway.synthesize(h.request())
        assert _fake(h).call_count == 1

    @pytest.mark.parametrize(
        "provider",
        [
            FakeSpeechSynthesisProvider(raises=RuntimeError("x")),
            FakeSpeechSynthesisProvider(raises=TimeoutError()),
            FakeSpeechSynthesisProvider(delay_seconds=99.0),
            FakeSpeechSynthesisProvider(
                result=ProviderSynthesisResult(audio_bytes=b"")
            ),
        ],
    )
    def test_no_automatic_retry_after_any_failure(
        self, provider: FakeSpeechSynthesisProvider
    ) -> None:
        h = TTSHarness(provider=provider, timeout=0.2)
        h.gateway.synthesize(h.request())
        assert provider.call_count == 1

    def test_no_retry_on_fish_failures(self) -> None:
        for response in (
            httpx.Response(429),
            httpx.Response(503),
            httpx.Response(401),
            httpx.Response(200, content=b""),
        ):
            h, rec, _ = fish_harness(Recorder(response))
            result = h.gateway.synthesize(h.request())
            assert result.status is S.FAILED
            assert len(rec.requests) == 1

    @pytest.mark.parametrize(
        "response,category",
        [
            (httpx.Response(401), C.AUTHENTICATION_ERROR),
            (httpx.Response(403), C.AUTHENTICATION_ERROR),
            (httpx.Response(429), C.RATE_LIMITED),
            (httpx.Response(500), C.PROVIDER_ERROR),
            (httpx.Response(503), C.PROVIDER_ERROR),
            (httpx.Response(200, content=b""), C.INVALID_AUDIO),
            (
                httpx.Response(
                    200, headers={"content-type": "text/html"}, content=b"<html>"
                ),
                C.INVALID_AUDIO,
            ),
            (httpx.Response(200, json={"message": "LEAKYERRORBODY"}), C.INVALID_AUDIO),
            (
                httpx.Response(
                    200,
                    headers={"content-type": "audio/mpeg"},
                    content=b"not an mp3 at all",
                ),
                C.INVALID_AUDIO,
            ),
            (
                httpx.Response(302, headers={"location": "https://evil.example"}),
                C.PROVIDER_ERROR,
            ),
        ],
    )
    def test_fish_failures_map_to_generic_categories_without_leaking_bodies(
        self, response: httpx.Response, category: TTSErrorCategory
    ) -> None:
        h, rec, _ = fish_harness(Recorder(response))
        result = h.gateway.synthesize(h.request())
        assert result.error_category is category
        assert "LEAKYERRORBODY" not in repr(result) + result.model_dump_json()
        assert len(rec.requests) == 1

    def test_connection_failure_and_timeout_from_fish(self) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        h, rec, _ = fish_harness(Recorder(down))
        assert h.gateway.synthesize(h.request()).error_category is C.PROVIDER_ERROR
        h2, rec2, _ = fish_harness(Recorder(slow))
        assert h2.gateway.synthesize(h2.request()).error_category is C.TIMEOUT
        assert len(rec.requests) == 1 and len(rec2.requests) == 1

    def test_oversized_fish_audio_is_rejected(self) -> None:
        big = (
            b"ID3\x04\x00\x00\x00\x00\x00\x00"
            + b"\xff\xfb\x90\x00"
            + b"\x00" * (10 * 1024 * 1024)
        )
        h, _, _ = fish_harness(
            Recorder(
                httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=big)
            )
        )
        assert h.gateway.synthesize(h.request()).error_category is C.OUTPUT_TOO_LARGE


# ===================================================== duplicate / concurrency


class TestCostControls:
    def test_reusing_a_request_id_is_rejected_without_a_second_call(self) -> None:
        h = TTSHarness()
        assert h.gateway.synthesize(h.request(request_id="dup")).status is S.SUCCEEDED
        again = h.gateway.synthesize(h.request(request_id="dup"))
        assert (
            again.status is S.REJECTED and again.error_category is C.DUPLICATE_REQUEST
        )
        assert _fake(h).call_count == 1

    def test_a_failed_attempt_keeps_the_request_id_no_silent_retry(self) -> None:
        h = TTSHarness(provider=FakeSpeechSynthesisProvider(raises=RuntimeError("x")))
        h.gateway.synthesize(h.request(request_id="r"))
        again = h.gateway.synthesize(h.request(request_id="r"))
        assert again.error_category is C.DUPLICATE_REQUEST
        assert _fake(h).call_count == 1

    def test_request_ids_are_scoped_per_principal(self) -> None:
        h = TTSHarness()
        h.grant("sam_default", principal=BOB)
        assert h.gateway.synthesize(h.request(request_id="x")).status is S.SUCCEEDED
        assert (
            h.gateway.synthesize(h.request(request_id="x", principal=BOB)).status
            is S.SUCCEEDED
        )

    def test_concurrent_identical_requests_call_the_provider_at_most_once(self) -> None:
        release = threading.Event()

        def slowish(r: Any) -> Any:
            release.wait(0.2)
            return ProviderSynthesisResult(audio_bytes=MP3)

        h = TTSHarness(provider=FakeSpeechSynthesisProvider(slowish))
        results: list[Any] = []

        def run() -> None:
            results.append(h.gateway.synthesize(h.request(request_id="race")))

        threads = [threading.Thread(target=run) for _ in range(6)]
        for t in threads:
            t.start()
        release.set()
        for t in threads:
            t.join(5)
        assert _fake(h).call_count == 1
        assert sum(r.status is S.SUCCEEDED for r in results) == 1

    def test_the_gateway_rejects_unbounded_timeouts(self) -> None:
        h = TTSHarness()
        for bad in (0, -1, 61, float("inf"), float("nan")):
            with pytest.raises(ValueError):
                TTSGateway(
                    permission_engine=h.pengine,
                    provider=h.provider,
                    profiles=h.profiles,
                    provider_timeout_seconds=bad,
                )

    def test_the_gateway_public_surface_is_synthesize_only(self) -> None:
        assert {n for n in dir(TTSGateway) if not n.startswith("_")} == {"synthesize"}


# ==================================================================== audit


class TestAudit:
    def test_one_event_per_request_on_every_path(self) -> None:
        h = TTSHarness()
        h.gateway.synthesize(h.request())  # success
        h.gateway.synthesize(h.request(profile_id="nope"))  # unknown profile
        h.gateway.synthesize(h.request(""))  # invalid text
        h.gateway.synthesize(h.request(SECRET_TEXT))  # secret
        h.gateway.synthesize(h.request(principal=BOB))  # denied
        assert len(_events(h)) == 5

    def test_event_carries_the_safe_metadata(self) -> None:
        h = TTSHarness()
        result = h.gateway.synthesize(h.request("twelve chars", request_id="rq"))
        [event] = _events(h)
        assert (event.request_id, event.synthesis_id) == ("rq", result.synthesis_id)
        assert (
            event.provider_id == "fake-tts"
            and event.trusted_profile_id == "sam_default"
        )
        assert event.permission_resource is PermissionResource.SPEECH_SYNTHESIS
        assert event.permission_action is PermissionAction.SEND
        assert event.scope == "fake-tts/sam_default"
        assert event.text_length == 12
        assert event.output_size == result.audio.byte_length  # type: ignore[union-attr]
        assert event.authorization_outcome is PermissionOutcomeSummary.ALLOW
        assert event.status is S.SUCCEEDED and event.provider_call_attempted is True
        assert event.duration_ms >= 0 and event.occurred_at.tzinfo is not None

    def test_audit_never_contains_text_audio_key_or_provider_error_bodies(self) -> None:
        h, rec, _ = fish_harness()
        h.gateway.synthesize(h.request("A uniquely private sentence."))
        h.gateway.synthesize(h.request(SECRET_TEXT))
        h2, _, _ = fish_harness(
            Recorder(httpx.Response(500, json={"message": "LEAKYERRORBODY"}))
        )
        h2.gateway.synthesize(h2.request("Another private sentence."))
        for harness in (h, h2):
            dumped = "\n".join(e.model_dump_json() for e in _events(harness))
            dumped += "\n".join(str(e) for e in harness.permission_audit.list_events())
            dumped += "\n".join(str(c) for c in harness.confirmations._list_for_test())
            for fragment in (
                "private sentence",
                "sk-ant",
                FAKE_KEY,
                "LEAKYERRORBODY",
                "Bearer",
            ):
                assert fragment not in dumped
            assert MP3.hex() not in dumped

    def test_audit_model_has_no_field_that_could_hold_content(self) -> None:
        forbidden = {
            "text",
            "audio",
            "audio_bytes",
            "api_key",
            "authorization",
            "body",
            "content",
            "prompt",
        }
        assert not forbidden & set(TTSAuditEvent.model_fields)

    def test_failing_audit_sink_never_changes_the_result(self) -> None:
        assert (
            TTSHarness(audit_sink=FailingTTSAuditSink())
            .gateway.synthesize(TTSHarness().request())
            .status
            is S.SUCCEEDED
        )

    def test_failing_audit_sink_cannot_turn_a_denial_into_an_allow(self) -> None:
        h = TTSHarness(audit_sink=FailingTTSAuditSink(), grant_all=False)
        assert h.gateway.synthesize(h.request()).status is S.DENIED
        assert _fake(h).call_count == 0

    def test_gateway_without_an_audit_sink_works(self) -> None:
        h = TTSHarness()
        gateway = TTSGateway(
            permission_engine=h.pengine, provider=h.provider, profiles=h.profiles
        )
        assert gateway.synthesize(h.request()).status is S.SUCCEEDED
