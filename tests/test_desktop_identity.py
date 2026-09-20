"""Owner voice identity, Guest Mode and language through /desktop/v1."""

from __future__ import annotations

import base64
import json
import re
from datetime import timedelta
from typing import Any

from sam.desktop.identity import DesktopVoiceIdentity
from sam.desktop.runtime import LOCAL_PRINCIPAL
from sam.permissions.models import (
    PermissionAction,
    PermissionRequest,
    PermissionResource,
    PermissionScope,
    RiskLevel,
)
from sam.tts.provider import FakeSpeechSynthesisProvider
from sam.voice.audio import validate_audio
from sam.voice.models import AudioFormat, AudioInput
from sam.voice.transcription import FakeTranscriptionProvider
from sam.voice_identity.providers import FakeSpeakerEmbeddingProvider
from sam.voice_identity.store import InMemoryVoiceProfileStore
from tests.desktop_support import Bridge, StubAgent
from tests.voice_identity_support import DIM, OTHER_VOICE, OWNER_VOICE, Clock, jitter
from tests.voice_support import make_wav


def _identity(b: Bridge) -> DesktopVoiceIdentity:
    assert b.runtime.identity is not None
    return b.runtime.identity


SECRET = "correct-horse-battery-staple-1"
FA = "سلام سام، این API با FastAPI کار می‌کند؟"


class IdentityBridge(Bridge):
    def __init__(
        self,
        *,
        text: str = "hello there",
        with_secret: bool = True,
        tts: FakeSpeechSynthesisProvider | None = None,
    ) -> None:
        self.embedder = FakeSpeakerEmbeddingProvider(dimension=DIM)
        self.store = InMemoryVoiceProfileStore()
        self.voice_stt = FakeTranscriptionProvider(text)
        self.clock = Clock()
        self._n = 0
        super().__init__(
            stt=self.voice_stt,
            tts=tts,
            embedder=self.embedder,
            profile_store=self.store,
            step_up=SECRET if with_secret else None,
            clock=self.clock,
        )

    def clip(self, voice: Any = OWNER_VOICE, frames: int = 40_000) -> str:
        self._n += 1
        wav = make_wav(frames=frames, seed=self._n)
        digest = validate_audio(
            AudioInput(content=wav, declared_format=AudioFormat.WAV_PCM16)
        ).metadata.digest_sha256
        self.embedder.register(digest, jitter(voice, self._n))
        return base64.b64encode(wav).decode()

    def enroll(self, samples: int = 4) -> None:
        begin = self.post("/voice/identity/enroll/begin", {"step_up": SECRET}).json()
        assert begin["status"] == "ok", begin
        for _ in range(samples):
            r = self.post(
                "/voice/identity/enroll/sample",
                {"session_id": begin["session_id"], "audio_base64": self.clip()},
            ).json()
            assert r["accepted"], r
        done = self.post(
            "/voice/identity/enroll/complete", {"session_id": begin["session_id"]}
        ).json()
        assert done["status"] == "ok", done

    def utter(self, voice: Any, language: str = "auto") -> dict[str, Any]:
        body: dict[str, Any] = self.post(
            "/voice/utterance", {"audio_base64": self.clip(voice), "language": language}
        ).json()
        return body

    def start_guest(self, minutes: int = 15) -> dict[str, Any]:
        ch = self.post("/voice/guest/challenge", {}).json()
        assert ch["status"] == "ok", ch
        digits = [c for c in ch["text_en"] if c.isdigit()]
        word = ch["text_en"].split()[-1]
        self.voice_stt._result = f"{' '.join(digits)} {word}"
        started: dict[str, Any] = self.post(
            "/voice/guest/start",
            {
                "challenge_id": ch["challenge_id"],
                "audio_base64": self.clip(OWNER_VOICE),
                "step_up": SECRET,
                "minutes": minutes,
            },
        ).json()
        return started


# -------------------------------------------------------------- availability


def test_identity_is_honestly_unavailable_when_not_configured() -> None:
    bridge = Bridge()
    body = bridge.get("/voice/identity").json()
    assert body["available"] is False and body["enrolled"] is None
    assert bridge.get("/status").json()["voice_identity"] == "not_configured"
    assert (
        bridge.post("/voice/guest/challenge", {}).json()["status"] == "not_configured"
    )


def test_identity_status_and_capability_when_configured() -> None:
    b = IdentityBridge()
    body = b.get("/voice/identity").json()
    assert body["available"] is True and body["enrolled"] is False
    assert body["mode"] == "owner_only" and body["guest"] == {
        "active": False,
        "seconds_remaining": 0,
    }
    assert b.get("/status").json()["voice_identity"] == "configured"


# ---------------------------------------------------------------- step-up


def test_enrollment_requires_the_step_up_secret() -> None:
    b = IdentityBridge(with_secret=False)
    r = b.post("/voice/identity/enroll/begin", {"step_up": "anything-goes-here"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "step_up_unavailable"
    b = IdentityBridge()
    wrong = b.post("/voice/identity/enroll/begin", {"step_up": "wrong-secret-value"})
    assert wrong.json()["detail"]["code"] == "step_up_failed"
    assert b.get("/voice/identity").json()["enrolled"] is False


def test_step_up_locks_then_unlocks_after_the_cooldown() -> None:
    b = IdentityBridge()
    codes = [
        b.post("/voice/identity/enroll/begin", {"step_up": f"wrong-secret-{i}"}).json()[
            "detail"
        ]["code"]
        for i in range(3)
    ]
    assert codes == ["step_up_failed", "step_up_failed", "step_up_locked"]
    assert (
        b.post("/voice/identity/enroll/begin", {"step_up": SECRET}).status_code == 403
    )
    b.runtime.clock = lambda: b.clock() + timedelta(minutes=6)
    assert (
        b.post("/voice/identity/enroll/begin", {"step_up": SECRET}).json()["status"]
        == "ok"
    )


# --------------------------------------------------------------- enrollment


def test_full_enrollment_over_the_bridge_then_status() -> None:
    b = IdentityBridge()
    b.enroll()
    body = b.get("/voice/identity").json()
    assert body["enrolled"] is True
    dump = json.dumps(body)
    assert "vector" not in dump and "embedding" not in dump and "template" not in dump
    # raw audio and templates never land in the activity feed
    text = json.dumps(b.get("/activity").json())
    assert "Owner voice enrolled" in text and "vector" not in text


def test_enrollment_quality_and_completion_errors_are_safe() -> None:
    b = IdentityBridge()
    begin = b.post("/voice/identity/enroll/begin", {"step_up": SECRET}).json()
    sid = begin["session_id"]
    short = b.post(
        "/voice/identity/enroll/sample",
        {"session_id": sid, "audio_base64": b.clip(frames=8_000)},
    ).json()
    assert short["status"] == "rejected" and short["reason_code"] == "audio_too_short"
    bad = b.post(
        "/voice/identity/enroll/sample", {"session_id": sid, "audio_base64": "###"}
    ).json()
    assert bad["reason_code"] == "audio_invalid"
    junk = b.post(
        "/voice/identity/enroll/sample",
        {"session_id": sid, "audio_base64": base64.b64encode(b"junk").decode()},
    ).json()
    assert junk["reason_code"] == "audio_invalid"
    early = b.post("/voice/identity/enroll/complete", {"session_id": sid}).json()
    assert early["reason_code"] == "too_few_samples"
    gone = b.post(
        "/voice/identity/enroll/sample",
        {"session_id": "nope", "audio_base64": b.clip()},
    ).json()
    assert gone["reason_code"] == "session_invalid"
    assert b.get("/voice/identity").json()["enrolled"] is False


def test_delete_needs_step_up_and_removes_the_profile_and_guest() -> None:
    b = IdentityBridge()
    b.enroll()
    assert (
        b.post("/voice/identity/delete", {"step_up": "wrong-secret-value"}).status_code
        == 403
    )
    assert b.get("/voice/identity").json()["enrolled"] is True
    assert b.start_guest()["status"] == "ok"
    # An active Guest Mode blocks owner-bound admin routes at the backend.
    blocked = b.post("/voice/identity/delete", {"step_up": SECRET})
    assert (
        blocked.status_code == 403
        and b.get("/voice/identity").json()["enrolled"] is True
    )
    assert b.post("/voice/guest/end", {}).json()["status"] == "ok"
    assert (
        b.post("/voice/identity/delete", {"step_up": SECRET}).json()["status"] == "ok"
    )
    body = b.get("/voice/identity").json()
    assert body["enrolled"] is False and body["guest"]["active"] is False


# ------------------------------------------------------- owner-only gating


def test_non_owner_is_blocked_before_any_transcription_or_agent_call() -> None:
    b = IdentityBridge()
    b.enroll()
    calls_before = b.voice_stt.call_count
    r = b.utter(OTHER_VOICE)
    assert r["status"] == "denied" and r["reason_code"] == "owner_verification_required"
    assert r["message"] == "Owner verification required."
    assert r["transcript"] is None and r["reply"] is None
    assert b.voice_stt.call_count == calls_before and b.agent.messages == []
    text = json.dumps(r)
    assert "local-user" not in text and "Memory" not in text


def test_owner_passes_and_status_reports_safe_verification_state() -> None:
    b = IdentityBridge(text="what is the weather")
    b.enroll()
    r = b.utter(OWNER_VOICE)
    assert (
        r["status"] == "ok"
        and r["speaker"] == "owner"
        and r["speaker_result"] == "owner_verified"
    )
    assert b.agent.messages == ["what is the weather"]
    assert b.get("/voice/identity").json()["last_verification"] == "verified"
    b.utter(OTHER_VOICE)
    assert b.get("/voice/identity").json()["last_verification"] == "not_verified"


def test_no_profile_speaker_is_not_owner_and_nothing_reaches_the_agent() -> None:
    b = IdentityBridge(text="hello")
    for voice in (OTHER_VOICE, OWNER_VOICE):
        r = b.utter(voice)
        assert r["status"] == "denied"
        assert r["reason_code"] == "owner_verification_required"
        assert r["speaker_result"] == "not_enrolled"
        assert r.get("speaker") is None
    assert b.agent.messages == []  # owner-only conversation does not unlock
    assert b.get("/voice/identity").json()["mode"] == "owner_only"


def test_no_profile_guest_mode_cannot_be_enabled() -> None:
    b = IdentityBridge()
    assert b.post("/voice/guest/challenge", {}).json()["reason_code"] == "not_enrolled"
    # Even bypassing the route gate: a challenge issued straight from the
    # coordinator, correct step-up, correct spoken words -> still no proof.
    identity = _identity(b)
    challenge = identity.coordinator.issue_challenge(identity.session_id)
    digits = " ".join(challenge.digits)
    b.voice_stt._result = f"{digits} {challenge.display('en').split()[-1]}"
    r = b.post(
        "/voice/guest/start",
        {
            "challenge_id": challenge.challenge_id,
            "audio_base64": b.clip(OWNER_VOICE),
            "step_up": SECRET,
            "minutes": 15,
        },
    ).json()
    assert r["status"] == "denied"
    assert b.get("/voice/identity").json()["guest"]["active"] is False
    assert identity.guests.current() is None


def test_identity_runtime_missing_blocks_voice_and_guest_and_ignores_overrides() -> (
    None
):
    stt = FakeTranscriptionProvider("hello there")
    b = Bridge(stt=stt)  # identity NOT configured
    assert b.runtime.identity is None
    audio = base64.b64encode(make_wav(frames=40_000, seed=1)).decode()
    body = b.post("/voice/utterance", {"audio_base64": audio}).json()
    assert (
        body["status"] == "denied" and body["reason_code"] == "identity_not_configured"
    )
    for extra in (
        {"speaker": "owner"},
        {"principal": "local-user"},
        {"identity": "off"},
    ):
        r = b.post("/voice/utterance", {"audio_base64": audio, **extra})
        assert r.status_code == 422  # the frontend cannot override the state
    assert (
        b.post(
            "/voice/guest/start",
            {
                "challenge_id": "x",
                "audio_base64": audio,
                "step_up": SECRET,
                "minutes": 5,
            },
        ).json()["status"]
        == "not_configured"
    )
    assert stt.call_count == 0 and b.agent.messages == []
    assert b.post("/chat", {"message": "hi"}).status_code == 200  # text unaffected


def test_no_profile_never_falls_back_to_the_legacy_owner_voice_session() -> None:
    b = IdentityBridge(text="what is the weather")
    r = b.utter(OWNER_VOICE)
    assert r["status"] == "denied" and r["reason_code"] == "owner_verification_required"
    assert b.agent.messages == []
    assert b.post("/chat", {"message": "hi"}).status_code == 200  # text still works
    assert b.agent.messages == ["hi"]


def test_no_profile_enrollment_still_begins_through_step_up_only() -> None:
    b = IdentityBridge()
    assert b.get("/voice/identity").json()["enrolled"] is False
    wrong = b.post("/voice/identity/enroll/begin", {"step_up": "wrong-secret-value-1"})
    assert wrong.status_code == 403
    ok = b.post("/voice/identity/enroll/begin", {"step_up": SECRET}).json()
    assert ok["status"] == "ok" and ok["session_id"]
    # A voice match is not a substitute for the step-up secret.
    assert b.post("/voice/identity/enroll/begin", {}).status_code == 422


def test_no_profile_frontend_cannot_force_owner_state() -> None:
    b = IdentityBridge()
    for extra in (
        {"speaker": "owner"},
        {"speaker_class": "owner"},
        {"principal": "local-user"},
        {"owner_verified": True},
        {"mode": "guest_mode"},
    ):
        r = b.post(
            "/voice/utterance",
            {"audio_base64": b.clip(OTHER_VOICE), "language": "auto", **extra},
        )
        assert r.status_code == 422
    assert b.agent.messages == []
    assert b.get("/voice/identity").json()["mode"] == "owner_only"


def test_unreadable_profile_store_fails_closed_never_as_not_enrolled() -> None:
    b = IdentityBridge()
    b.enroll()
    b.store.fail_load = True
    r = b.utter(OWNER_VOICE)
    assert r["status"] == "denied" and r["speaker_result"] == "verification_error"
    assert b.get("/voice/identity").json()["enrolled"] is None


def test_requests_cannot_assert_an_owner_identity() -> None:
    b = IdentityBridge()
    for body in (
        {"audio_base64": b.clip(), "principal": "local-user"},
        {"audio_base64": b.clip(), "owner": True},
        {"audio_base64": b.clip(), "speaker": "owner"},
        {"audio_base64": b.clip(), "speaker_result": "owner_verified"},
    ):
        assert b.post("/voice/utterance", body).status_code == 422
    for path in ("/voice/guest/start", "/voice/identity/enroll/begin"):
        assert (
            b.post(
                path,
                {
                    "step_up": SECRET,
                    "owner": True,
                    "challenge_id": "x",
                    "audio_base64": "AA==",
                },
            ).status_code
            == 422
        )


# ----------------------------------------------------------------- language


def test_persian_speech_gets_a_persian_rtl_answer() -> None:
    b = IdentityBridge(text=FA)
    b.enroll()
    r = b.utter(OWNER_VOICE)
    assert r["language"] == "fa" and r["direction"] == "rtl" and r["transcript"] == FA
    assert b.agent.languages[-1] == "fa"


def test_english_speech_and_explicit_preference() -> None:
    b = IdentityBridge(text="please review this file")
    b.enroll()
    assert b.utter(OWNER_VOICE)["language"] == "en"
    assert b.utter(OWNER_VOICE, language="fa")["language"] == "fa"
    assert b.agent.languages[-2:] == ["en", "fa"]


def test_chat_language_policy_and_unicode_round_trip() -> None:
    b = Bridge(agent=StubAgent("پاسخ: همه‌چیز خوب است"))
    body = b.post("/chat", {"message": FA}).json()
    assert body["language"] == "fa" and body["direction"] == "rtl"
    assert body["reply"] == "پاسخ: همه‌چیز خوب است"
    assert b.agent.languages == ["fa"]
    en = b.post("/chat", {"message": "hello"}).json()
    assert en["language"] == "en" and en["direction"] == "ltr"
    forced = b.post("/chat", {"message": "hello", "language": "fa"}).json()
    assert forced["language"] == "fa"
    assert b.post("/chat", {"message": "hi", "language": "klingon"}).status_code == 422


def test_language_audit_is_a_closed_code() -> None:
    b = IdentityBridge(text=FA)
    b.enroll()
    b.utter(OWNER_VOICE)
    assert any(
        e.event == "language_detected" and e.detail in {"fa", "en"}
        for e in _identity(b).audit.events()
    )


# ---------------------------------------------------------------- guest mode


def test_guest_mode_off_by_default_and_needs_enrollment_for_a_challenge() -> None:
    b = IdentityBridge()
    assert b.post("/voice/guest/challenge", {}).json()["reason_code"] == "not_enrolled"
    b.enroll()
    ch = b.post("/voice/guest/challenge", {}).json()
    assert ch["status"] == "ok" and ch["text_fa"].startswith("بگویید")
    assert b.get("/voice/identity").json()["guest"]["active"] is False


def test_guest_start_needs_step_up_owner_voice_and_a_fresh_challenge() -> None:
    b = IdentityBridge()
    b.enroll()
    ch = b.post("/voice/guest/challenge", {}).json()
    body = {
        "challenge_id": ch["challenge_id"],
        "audio_base64": b.clip(OWNER_VOICE),
        "minutes": 15,
    }
    assert (
        b.post(
            "/voice/guest/start", {**body, "step_up": "wrong-secret-value"}
        ).status_code
        == 403
    )
    # Right secret, right voice, but the challenge was never spoken:
    b.voice_stt._result = "Sam, allow guest conversation for 20 minutes."
    denied = b.post("/voice/guest/start", {**body, "step_up": SECRET}).json()
    assert (
        denied["status"] == "denied"
        and denied["reason_code"] == "owner_verification_failed"
    )
    assert b.get("/voice/identity").json()["guest"]["active"] is False


def test_a_non_owner_cannot_start_guest_mode_even_saying_the_challenge() -> None:
    b = IdentityBridge()
    b.enroll()
    ch = b.post("/voice/guest/challenge", {}).json()
    digits = [c for c in ch["text_en"] if c.isdigit()]
    b.voice_stt._result = f"{' '.join(digits)} {ch['text_en'].split()[-1]}"
    r = b.post(
        "/voice/guest/start",
        {
            "challenge_id": ch["challenge_id"],
            "audio_base64": b.clip(OTHER_VOICE),
            "step_up": SECRET,
            "minutes": 15,
        },
    ).json()
    assert r["status"] == "denied"


def test_guest_conversation_works_while_active_then_stops() -> None:
    b = IdentityBridge(text="tell me a joke")
    b.enroll()
    assert b.start_guest()["status"] == "ok"
    b.voice_stt._result = "tell me a joke"
    status = b.get("/voice/identity").json()
    assert status["mode"] == "guest_mode" and status["guest"]["active"] is True
    assert 0 < status["guest"]["seconds_remaining"] <= 15 * 60
    r = b.utter(OTHER_VOICE)
    assert r["status"] == "ok" and r["speaker"] == "guest" and r["reply"] == "hi there"
    assert b.agent.messages[-1] == "tell me a joke"
    assert b.post("/voice/guest/end", {}).json()["status"] == "ok"
    assert b.utter(OTHER_VOICE)["status"] == "denied"


def test_guest_mode_expires_on_its_own() -> None:
    b = IdentityBridge(text="hello")
    b.enroll()
    b.start_guest(minutes=5)
    b.clock.advance(minutes=6)
    b.runtime.clock = b.clock
    assert b.utter(OTHER_VOICE)["status"] == "denied"
    assert b.get("/voice/identity").json()["guest"]["active"] is False


def test_guest_mode_cannot_be_started_twice_or_extended() -> None:
    b = IdentityBridge()
    b.enroll()
    assert b.start_guest()["status"] == "ok"
    again = b.start_guest()
    assert again["status"] == "rejected" and again["reason_code"] == "guest_active"
    assert (
        b.post(
            "/voice/guest/start",
            {
                "challenge_id": "x",
                "audio_base64": "AA==",
                "step_up": SECRET,
                "minutes": 31,
            },
        ).status_code
        == 422
    )


def test_guest_gets_no_grants_and_owner_permissions_are_unchanged() -> None:
    b = IdentityBridge(text="hello")
    b.enroll()
    before = b.get("/permissions").json()
    b.start_guest()
    b.utter(OTHER_VOICE)
    assert b.get("/permissions").status_code == 403  # not readable as the owner now
    b.post("/voice/guest/end", {})
    after = b.get("/permissions").json()
    assert before == after  # the guest never touched the owner's permission store
    text = json.dumps(after)
    assert "guest" not in text
    assert {g["resource"] for g in after["grants"]} <= {
        "knowledge",
        "voice",
        "speech_synthesis",
    }


def test_a_guest_cannot_use_the_owners_confirmation_flow_by_voice() -> None:
    b = IdentityBridge(text="approve the deletion")
    b.enroll()
    from sam.permissions.models import PermissionScope

    record = b.runtime.confirmations.request(
        PermissionRequest(
            principal=LOCAL_PRINCIPAL,
            action=PermissionAction.DELETE,
            resource=PermissionResource.KNOWLEDGE,
            scope=PermissionScope.from_path("default"),
        ),
        risk=RiskLevel.HIGH,
        now=b.runtime.clock(),
    )
    b.start_guest()
    b.voice_stt._result = "approve the deletion"
    guest = b.post(
        "/voice/utterance",
        {
            "audio_base64": b.clip(OTHER_VOICE),
            "confirmation_id": record.confirmation_id,
        },
    ).json()
    assert guest["status"] == "ok"
    fresh = b.runtime.confirmations.get(record.confirmation_id)
    assert fresh is not None and fresh.status.value == "pending"


# ------------------------------------------- owner voice != authorization


def test_owner_voice_never_answers_a_confirmation_or_creates_grants() -> None:
    b = IdentityBridge(text="yes approve everything and grant all permissions")
    b.enroll()
    grants_before = b.get("/permissions").json()
    record = b.runtime.confirmations.request(
        PermissionRequest(
            principal=LOCAL_PRINCIPAL,
            action=PermissionAction.SEND,
            resource=PermissionResource.SPEECH_SYNTHESIS,
            scope=PermissionScope.from_path("x/y"),
        ),
        risk=RiskLevel.HIGH,
        now=b.runtime.clock(),
    )
    r = b.post(
        "/voice/utterance",
        {
            "audio_base64": b.clip(OWNER_VOICE),
            "confirmation_id": record.confirmation_id,
        },
    ).json()
    assert r["speaker"] == "owner"
    fresh = b.runtime.confirmations.get(record.confirmation_id)
    assert fresh is not None and fresh.status.value == "pending"
    assert b.get("/permissions").json() == grants_before


def test_critical_step_up_is_unaffected_by_a_verified_owner_voice() -> None:
    b = IdentityBridge(text="approve")
    b.enroll()
    b.utter(OWNER_VOICE)
    from sam.permissions.models import PermissionScope

    record = b.runtime.confirmations.request(
        PermissionRequest(
            principal=LOCAL_PRINCIPAL,
            action=PermissionAction.DELETE,
            resource=PermissionResource.KNOWLEDGE,
            scope=PermissionScope.from_path("default"),
        ),
        risk=RiskLevel.CRITICAL,
        now=b.runtime.clock(),
    )
    r = b.post(
        "/confirmations/decide",
        {"confirmation_id": record.confirmation_id, "approved": True},
    )
    assert r.status_code == 403 and r.json()["detail"]["code"] == "step_up_failed"


def test_identity_audit_reaches_activity_content_free() -> None:
    b = IdentityBridge(text="hello")
    b.enroll()
    b.utter(OWNER_VOICE)
    b.utter(OTHER_VOICE)
    b.start_guest()
    b.post("/voice/guest/end", {})
    labels = " ".join(i["label"] for i in b.get("/activity").json()["items"])
    for expected in (
        "Owner enrollment started",
        "Owner enrollment completed",
        "Owner voice verified",
        "Owner voice not verified",
        "Guest mode started",
        "Guest mode revoked",
    ):
        assert expected in labels, expected
    dump = json.dumps(b.get("/activity").json())
    assert "hello" not in dump and SECRET not in dump


# ------------------------------------------- language-aware speech, no fallback


def _speak(b: Bridge, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"text": "hello", "voice_profile": "sam_default", **extra}
    first: dict[str, Any] = b.post("/tts/speak", body).json()
    if first["status"] != "confirmation_required":
        return first
    cid = first["challenge"]["confirmation_id"]
    b.post("/confirmations/decide", {"confirmation_id": cid, "approved": True})
    done: dict[str, Any] = b.post("/tts/speak", {**body, "confirmation_id": cid}).json()
    return done


def test_persian_text_with_no_persian_voice_stays_text_only_and_calls_no_provider() -> (
    None
):
    from sam.tts.provider import FakeSpeechSynthesisProvider

    tts = FakeSpeechSynthesisProvider()
    b = Bridge(tts=tts)
    r = b.post(
        "/tts/speak", {"text": "سلام", "voice_profile": "sam_default", "language": "fa"}
    ).json()
    assert r["status"] == "rejected" and r["reason_code"] == "language_unsupported"
    assert tts.call_count == 0  # no provider call, no substitute voice
    assert b.get("/status").json()["persian_tts"] == "not_configured"
    assert b.get("/status").json()["speech_profiles"][0]["languages"] == ["en"]


def test_a_supported_language_still_speaks() -> None:
    from sam.tts.provider import FakeSpeechSynthesisProvider

    tts = FakeSpeechSynthesisProvider()
    b = Bridge(tts=tts)
    assert _speak(b, language="en")["status"] == "ok" and tts.call_count == 1


def test_a_trusted_persian_profile_is_advertised_and_used() -> None:
    from sam.tts.provider import FakeSpeechSynthesisProvider

    tts = FakeSpeechSynthesisProvider()
    b = Bridge(tts=tts)
    b.runtime.speech_profile_languages["sam_default"] = frozenset({"fa", "en"})
    body = b.get("/status").json()
    assert body["speech_profiles"][0]["languages"] == ["fa", "en"]
    assert body["persian_tts"] == "configured"
    assert _speak(b, text="سلام", language="fa")["status"] == "ok"


def test_the_language_field_cannot_select_a_provider_model_or_endpoint() -> None:
    b = Bridge(
        tts=__import__("sam.tts.provider", fromlist=["x"]).FakeSpeechSynthesisProvider()
    )
    assert (
        b.post(
            "/tts/speak",
            {"text": "hi", "voice_profile": "sam_default", "language": "klingon"},
        ).status_code
        == 422
    )
    for extra in (
        {"provider": "gemini"},
        {"model": "paid-model"},
        {"endpoint": "http://x"},
        {"fallback": True},
    ):
        assert (
            b.post(
                "/tts/speak", {"text": "hi", "voice_profile": "sam_default", **extra}
            ).status_code
            == 422
        )


def test_there_is_no_paid_or_cross_provider_fallback_in_the_desktop_code() -> None:
    import pathlib

    import sam.desktop as desktop_package

    assert desktop_package.__file__ is not None
    root = pathlib.Path(desktop_package.__file__).parent

    def strip(source: str) -> str:
        return re.sub(r"(?m)#.*$", "", re.sub(r'""".*?"""', "", source, flags=re.S))

    text = "\n".join(strip(p.read_text()) for p in root.glob("*.py")).lower()
    for banned in ("fallback", "billing", "paid", "upgrade_tier", "openai_tts"):
        assert banned not in text, banned
    # Gemini is an OPTIONAL, explicitly configured Persian voice: it may appear
    # only in the runtime's trusted composition, never in a route or request.
    mentions = {
        p.name for p in root.glob("*.py") if "gemini" in strip(p.read_text()).lower()
    }
    assert mentions <= {"runtime.py"}, mentions


def test_the_guests_own_permission_store_holds_only_voice_session_grants() -> None:
    b = IdentityBridge(text="hello")
    b.enroll()
    b.start_guest()
    b.utter(OTHER_VOICE)
    voice = _identity(b).guest_voice()
    assert voice is not None
    grants = list(voice.permission_store.list_grants(voice.principal))
    assert {(g.resource.value, g.action.value, g.scope.as_text()) for g in grants} == {
        ("voice", "create", "session"),
        ("voice", "read", "session"),
        ("voice", "update", "session"),
    }
    for g in grants:
        assert g.expires_at is not None and g.metadata == {"origin": "guest_mode"}
    # ...and the guest pipeline disappears with the guest.
    b.post("/voice/guest/end", {})
    assert _identity(b).guest_voice() is None


# -------------------------------------------- Memory isolation (real Memory)


def _seed_owner_memory(b: Bridge) -> None:
    from sam.memory.models import (
        MemoryCandidate,
        MemoryConfidence,
        MemorySource,
        MemoryType,
    )

    rt = b.runtime
    rt.memory.remember_working(
        principal=LOCAL_PRINCIPAL, key="k", content="OWNER-PRIVATE-WORKING-NOTE"
    )
    outcome = rt.memory.remember(
        MemoryCandidate(
            principal=LOCAL_PRINCIPAL,
            memory_type=MemoryType.SEMANTIC,
            content="OWNER-PRIVATE-DURABLE-FACT lives in Tehran",
            source=MemorySource.USER_EXPLICIT,
            confidence=MemoryConfidence.EXPLICIT,
        )
    )
    assert outcome.memory is not None, outcome.decision


def test_a_guest_cannot_read_or_write_the_real_memory_layer() -> None:
    from sam.memory.models import RetrievalQuery

    b = IdentityBridge(text="what do you know about me")
    b.enroll()
    _seed_owner_memory(b)
    rt, identity = b.runtime, _identity(b)
    owner_before = [
        r.memory.memory_id
        for r in rt.memory.retrieve(
            RetrievalQuery(principal=LOCAL_PRINCIPAL, text="OWNER-PRIVATE", limit=50)
        ).items
    ]
    assert owner_before  # the seed is really there for the owner

    assert b.start_guest()["status"] == "ok"
    session = identity.guests.current()
    assert session is not None and session.principal != LOCAL_PRINCIPAL
    b.voice_stt._result = "GUEST-SPOKEN-PRIVATE-SENTENCE remember this forever"
    r = b.utter(OTHER_VOICE)
    assert r["status"] == "ok" and r["speaker"] == "guest"

    # 1. read: the guest principal sees none of the owner's memory
    guest_view = rt.memory.retrieve(
        RetrievalQuery(principal=session.principal, text="OWNER-PRIVATE", limit=50)
    )
    assert list(guest_view.items) == []
    assert list(rt.memory.list_working(principal=session.principal)) == []
    # 2. write: nothing the guest said was stored, for the guest OR the owner
    for principal in (session.principal, LOCAL_PRINCIPAL):
        found = rt.memory.retrieve(
            RetrievalQuery(principal=principal, text="GUEST-SPOKEN-PRIVATE", limit=50)
        )
        assert all("GUEST-SPOKEN" not in r2.memory.content for r2 in found.items)
        assert all(
            "GUEST-SPOKEN" not in w.content
            for w in rt.memory.list_working(principal=principal)
        )
    # 3. the owner's memory is exactly as it was
    owner_after = [
        r2.memory.memory_id
        for r2 in rt.memory.retrieve(
            RetrievalQuery(principal=LOCAL_PRINCIPAL, text="OWNER-PRIVATE", limit=50)
        ).items
    ]
    assert owner_after == owner_before
    # 4. the bridge exposes no memory write route at all, and its read route is
    #    bound to the OWNER principal (a guest has no bridge access anyway)
    assert b.post("/memory/write", {"content": "x"}).status_code in (404, 405)
    assert b.post("/memory/search", {"text": "GUEST-SPOKEN"}).status_code == 403
    b.post("/voice/guest/end", {})  # only the owner, after ending, may read
    found_route = b.post("/memory/search", {"text": "GUEST-SPOKEN"}).json()
    assert all("GUEST-SPOKEN" not in i["content"] for i in found_route["items"])
    assert all("GUEST-SPOKEN" not in i["content"] for i in found_route["working"])
    # 5. the guest's own store holds only voice-session grants (no memory)
    assert b.start_guest()["status"] == "ok"
    voice = identity.guest_voice()
    assert voice is not None
    assert {
        g.resource.value for g in voice.permission_store.list_grants(voice.principal)
    } == {"voice"}
    # 6. ending the guest erases its session-local context
    b.post("/voice/guest/end", {})
    assert identity.guests.context_size() == 0
    assert list(rt.memory.list_working(principal=session.principal)) == []


# ------------------------- guest -> owner-principal laundering (route level)

GUEST_BLOCKED = {"detail": {"code": "guest_mode_active"}}
SPEAK = {"text": "hello", "voice_profile": "sam_default"}


def test_guest_to_tts_speak_is_denied_with_zero_provider_calls_and_no_consumption() -> (
    None
):
    tts = FakeSpeechSynthesisProvider()
    b = IdentityBridge(tts=tts)
    b.enroll()
    # The OWNER asks and approves a speech confirmation BEFORE Guest Mode.
    first = b.post("/tts/speak", SPEAK).json()
    assert first["status"] == "confirmation_required"
    cid = first["challenge"]["confirmation_id"]
    assert (
        b.post(
            "/confirmations/decide", {"confirmation_id": cid, "approved": True}
        ).status_code
        == 200
    )
    assert b.start_guest()["status"] == "ok"

    # Guest context: the backend's default owner principal must NOT be usable.
    for body in (SPEAK, {**SPEAK, "confirmation_id": cid}):
        r = b.post("/tts/speak", body)
        assert r.status_code == 403 and r.json() == GUEST_BLOCKED
    assert tts.call_count == 0  # zero provider calls
    record = b.runtime.confirmations.get(cid)
    assert record is not None and record.status.value == "approved"  # not consumed
    # A guest also cannot approve/deny confirmations to launder one.
    assert (
        b.post(
            "/confirmations/decide", {"confirmation_id": cid, "approved": False}
        ).json()
        == GUEST_BLOCKED
    )

    # Once the owner ends Guest Mode, the SAME approval is still usable once.
    assert b.post("/voice/guest/end", {}).json()["status"] == "ok"
    done = b.post("/tts/speak", {**SPEAK, "confirmation_id": cid}).json()
    assert done["status"] == "ok" and tts.call_count == 1


OWNER_BOUND = [
    ("post", "/chat", {"message": "hi"}),
    ("get", "/knowledge/resources", None),
    ("post", "/knowledge/query", {"query": "x"}),
    ("post", "/knowledge/ingest", {"title": "t", "text": "x"}),
    ("post", "/knowledge/remove", {"resource_id": "x"}),
    ("post", "/memory/search", {"text": "x"}),
    ("get", "/tools", None),
    ("get", "/permissions", None),
    ("post", "/permissions/revoke", {"grant_id": "x"}),
    ("get", "/activity", None),
    ("post", "/confirmations/decide", {"confirmation_id": "x", "approved": True}),
    ("post", "/tts/speak", SPEAK),
    ("post", "/voice/identity/enroll/begin", {"step_up": SECRET}),
    (
        "post",
        "/voice/identity/enroll/sample",
        {"session_id": "x", "audio_base64": "AAAA"},
    ),
    ("post", "/voice/identity/enroll/complete", {"session_id": "x"}),
    ("post", "/voice/identity/enroll/cancel", {"session_id": "x"}),
    ("post", "/voice/identity/delete", {"step_up": SECRET}),
]


def test_every_owner_bound_route_is_refused_while_guest_mode_is_active() -> None:
    tts = FakeSpeechSynthesisProvider()
    b = IdentityBridge(tts=tts)
    b.enroll()
    grants_before = b.get("/permissions").json()
    assert b.start_guest()["status"] == "ok"
    for method, path, body in OWNER_BOUND:
        r = b.get(path) if method == "get" else b.post(path, body)
        assert r.status_code == 403, path
        assert r.json() == GUEST_BLOCKED, path  # refused before any body is processed
    assert b.agent.messages == [] and tts.call_count == 0
    # Read-only status, the guest's own conversation, and ENDING guest mode stay open.
    assert b.get("/status").status_code == 200
    assert b.get("/voice/identity").json()["guest"]["active"] is True
    assert b.post("/voice/guest/end", {}).json()["status"] == "ok"
    for _method, path, body in OWNER_BOUND[:1]:  # the owner is back in control
        assert b.post(path, body).status_code == 200
    assert b.get("/permissions").json() == grants_before  # nothing changed meanwhile


def test_the_gate_is_inactive_without_a_guest_and_without_identity() -> None:
    plain = Bridge()  # identity not configured: nothing to gate on
    assert plain.get("/tools").status_code == 200
    b = IdentityBridge()
    b.enroll()
    assert b.get("/tools").status_code == 200 and b.get("/activity").status_code == 200
