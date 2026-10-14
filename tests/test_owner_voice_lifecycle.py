"""Enrollment -> real Keychain adapter -> fresh speaker check -> agent boundary.

Only the OS Keychain and embedding/STT providers are synthetic. No owner's
recording, profile, credential, network service or LLM is used by these tests.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from sam.voice_identity.errors import ProfileStoreError
from sam.voice_identity.store import (
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE,
    MacOSKeychainVoiceProfileStore,
)
from tests.test_desktop_identity import SECRET, IdentityBridge, _identity
from tests.test_voice_identity_store_security import FakeKeyring
from tests.voice_identity_support import OTHER_VOICE, OWNER_VOICE


def keychain_bridge(
    keyring: FakeKeyring | None = None,
) -> tuple[IdentityBridge, MacOSKeychainVoiceProfileStore, FakeKeyring]:
    keyring = keyring or FakeKeyring()
    store = MacOSKeychainVoiceProfileStore(keyring_module=keyring, platform="darwin")
    bridge = IdentityBridge(text="hello Sam")
    identity = _identity(bridge)
    identity.enrollment._store = store
    identity.verifier._store = store
    return bridge, store, keyring


def samples(bridge: IdentityBridge, *, re_enroll: bool = False) -> str:
    begun = bridge.post(
        "/voice/identity/enroll/begin",
        {"step_up": SECRET, "re_enroll": re_enroll},
    ).json()
    assert begun["status"] == "ok"
    session = str(begun["session_id"])
    for count in range(1, 4):
        result = bridge.post(
            "/voice/identity/enroll/sample",
            {"session_id": session, "audio_base64": bridge.clip()},
        ).json()
        assert result["accepted"] and result["sample_count"] == count
    return session


def finish(bridge: IdentityBridge, session: str) -> dict[str, Any]:
    result: dict[str, Any] = bridge.post(
        "/voice/identity/enroll/complete", {"session_id": session}
    ).json()
    return result


def test_three_samples_persist_then_next_clip_uses_profile_without_restart() -> None:
    b, store, keyring = keychain_bridge()
    identity = _identity(b)
    for _ in range(3):  # repeated pre-enrollment reads must never cache absence
        assert b.get("/voice/identity").json()["setup_state"] == "not_enrolled"
    session = samples(b)
    assert finish(b, session)["status"] == "ok"
    assert set(keyring.items) == {(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)}
    profile = store.load()
    assert profile is not None and profile.sample_count == 3
    assert profile.model_id == b.embedder.model_id
    assert profile.model_revision == b.embedder.model_revision
    assert profile.dimension == b.embedder.dimension
    status = b.get("/voice/identity").json()
    assert status["setup_state"] == "enrolled" and status["profile_compatible"]
    assert status["last_verification"] is None  # enrollment is not verification
    assert _identity(b) is identity
    assert b.utter(OWNER_VOICE)["speaker"] == "owner"
    assert b.agent.messages == ["hello Sam"]
    assert any(
        item["label"] == "Owner voice reached AgentCore"
        for item in b.get("/activity").json()["items"]
    )
    assert b.post("/voice/identity/enroll/begin", {"step_up": SECRET}).json()[
        "reason_code"
    ] == "already_enrolled"


def test_fresh_runtime_and_store_reload_enrolled_profile() -> None:
    b, _, keyring = keychain_bridge()
    assert finish(b, samples(b))["status"] == "ok"
    restarted, store, _ = keychain_bridge(keyring)
    assert restarted.runtime is not b.runtime
    assert store.load() is not None
    assert restarted.get("/voice/identity").json()["setup_state"] == "enrolled"
    assert restarted.utter(OWNER_VOICE)["speaker"] == "owner"


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("missing", "profile_missing_after_save"),
        ("read", "profile_load_failed"),
        ("different", "profile_readback_mismatch"),
        ("schema", "profile_schema_mismatch"),
        ("write", "store_error"),
    ],
)
def test_completion_requires_exact_immediate_readback(
    failure: str, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    b, store, keyring = keychain_bridge()
    session = samples(b)
    original_save = store.save

    def save(profile: Any) -> None:
        if failure == "write":
            raise ProfileStoreError("secure store unavailable")
        if failure == "missing":
            return
        original_save(profile)
        if failure == "read":
            keyring.fail.add("get")
        elif failure == "different":
            original_save(replace(profile, model_revision="different-revision"))
        elif failure == "schema":
            key = (KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
            data = json.loads(keyring.items[key])
            data["v"] = 999
            keyring.items[key] = json.dumps(data)

    monkeypatch.setattr(store, "save", save)
    result = finish(b, session)
    assert result["status"] != "ok" and result["reason_code"] == reason
    assert _identity(b).enrollment._session is None
    assert all(
        event.event != "owner_enrollment_completed"
        for event in _identity(b).audit.events()
    )
    assert b.agent.messages == []


def test_completion_discards_samples_and_closes_session() -> None:
    b, _, _ = keychain_bridge()
    session = samples(b)
    retained = _identity(b).enrollment._session
    assert retained is not None and len(retained.embeddings) == 3
    assert finish(b, session)["status"] == "ok"
    assert retained.embeddings == [] and retained.digests == set()
    assert _identity(b).enrollment._session is None
    assert finish(b, session)["reason_code"] == "session_invalid"


def test_reenrollment_invalidates_previous_decision_without_verifying_owner() -> None:
    b, _, _ = keychain_bridge()
    assert finish(b, samples(b))["status"] == "ok"
    assert b.utter(OTHER_VOICE)["reason_code"] == "speaker_not_recognized"
    old_session = _identity(b).session_id
    assert finish(b, samples(b, re_enroll=True))["status"] == "ok"
    status = b.get("/voice/identity").json()
    assert status["setup_state"] == "enrolled"
    assert status["last_verification"] is None
    assert status["last_verification_reason"] is None
    assert _identity(b).session_id != old_session
    assert b.agent.messages == []
    assert b.utter(OWNER_VOICE)["speaker"] == "owner"


def test_normal_owner_conversation_never_requests_proof_or_step_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b, _, _ = keychain_bridge()
    assert finish(b, samples(b))["status"] == "ok"

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("normal conversation requested stronger authentication")

    identity = _identity(b)
    monkeypatch.setattr(identity.coordinator, "prove_owner", forbidden)
    monkeypatch.setattr(identity.proofs, "mint", forbidden)
    monkeypatch.setattr(identity.step_up, "mint", forbidden)
    monkeypatch.setattr(b.runtime, "check_step_up", forbidden)
    result = b.utter(OWNER_VOICE)
    assert result["forwarded_to_agent"] and result["speaker"] == "owner"
    assert b.agent.messages == ["hello Sam"]


def test_missing_profile_reports_setup_and_never_reaches_agent() -> None:
    b, _, _ = keychain_bridge()
    assert b.utter(OWNER_VOICE)["reason_code"] == "voice_not_enrolled"
    assert b.get("/voice/identity").json()["reason_code"] == "not_enrolled"
    assert b.agent.messages == []


def test_keychain_read_error_is_distinct_and_fails_closed() -> None:
    b, _, keyring = keychain_bridge()
    assert finish(b, samples(b))["status"] == "ok"
    keyring.fail.add("get")
    result = b.utter(OWNER_VOICE)
    assert result["reason_code"] == "profile_load_failed"
    assert result["verification_reason"] == "profile_unreadable"
    assert result["message"] == "Owner voice profile couldn't be loaded."
    assert b.get("/voice/identity").json()["enrolled"] is None
    assert b.voice_stt.call_count == 0 and b.agent.messages == []


def test_wrong_speaker_stays_blocked_and_keeps_safe_reason() -> None:
    b, _, _ = keychain_bridge()
    assert finish(b, samples(b))["status"] == "ok"
    result = b.utter(OTHER_VOICE)
    assert result["reason_code"] == "speaker_not_recognized"
    assert result["verification_reason"] == "speaker_mismatch"
    assert result["speaker"] is None and result["status"] == "denied"
    assert b.voice_stt.call_count == 0 and b.agent.messages == []
    assert b.get("/voice/identity").json()["last_verification_reason"] == (
        "speaker_mismatch"
    )


@pytest.mark.parametrize("version", [2, True, "1", None])
def test_incompatible_profile_schema_is_reported_safely(version: object) -> None:
    b, _, keyring = keychain_bridge()
    assert finish(b, samples(b))["status"] == "ok"
    key = (KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    saved = json.loads(keyring.items[key])
    saved["v"] = version
    keyring.items[key] = json.dumps(saved)
    assert b.get("/voice/identity").json()["reason_code"] == "profile_schema_mismatch"
    assert b.utter(OWNER_VOICE)["reason_code"] == "profile_schema_mismatch"
    assert b.voice_stt.call_count == 0 and b.agent.messages == []


@pytest.mark.parametrize("field", ["model_id", "model_revision", "dimension"])
def test_model_and_dimension_mismatches_never_run_inference(field: str) -> None:
    b, _, _ = keychain_bridge()
    assert finish(b, samples(b))["status"] == "ok"
    calls = b.embedder.call_count
    setattr(b.embedder, field, 17 if field == "dimension" else "incompatible")
    result = b.utter(OWNER_VOICE)
    assert result["reason_code"] == "profile_model_mismatch"
    assert result["verification_reason"] == "model_mismatch"
    assert b.embedder.call_count == calls and b.agent.messages == []


def test_live_guest_cannot_reenroll_or_claim_owner() -> None:
    b, _, _ = keychain_bridge()
    assert finish(b, samples(b))["status"] == "ok"
    assert b.start_guest()["status"] == "ok"
    assert b.post(
        "/voice/identity/enroll/begin", {"step_up": SECRET, "re_enroll": True}
    ).status_code == 403
    b.voice_stt._result = "tell me a joke"
    assert b.utter(OTHER_VOICE)["speaker"] == "guest"
    assert b.post(
        "/voice/utterance", {"audio_base64": b.clip(OTHER_VOICE), "owner": True}
    ).status_code == 422


def test_voice_lifecycle_logs_and_responses_never_expose_biometrics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="sam.voice_identity.verification")
    b, _, keyring = keychain_bridge()
    assert finish(b, samples(b))["status"] == "ok"
    clip = b.clip(OTHER_VOICE)
    response = b.post("/voice/utterance", {"audio_base64": clip}).json()
    output = caplog.text + json.dumps(response) + json.dumps(b.get("/activity").json())
    assert "speaker_mismatch" in output
    assert clip not in output and next(iter(keyring.items.values())) not in output
    assert all(word not in output for word in ("vector", "similarity", "embedding"))
    assert "Owner verification required" not in output
