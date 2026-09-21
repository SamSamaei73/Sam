"""The /desktop/v1 bridge: gatekeeping, principal binding, and each surface."""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sam.agent.errors import AgentError
from sam.desktop import api as desktop_api
from sam.desktop.models import ChatRequest
from sam.tts.provider import FakeSpeechSynthesisProvider
from sam.voice.transcription import FakeTranscriptionProvider
from tests.desktop_support import HEADERS, TOKEN, Bridge, StubAgent, b64, wav_b64


def route_set(bridge: Bridge, needle: str = "/desktop/") -> set[tuple[str, str]]:
    paths = bridge.app.openapi()["paths"]
    return {(m.upper(), p) for p, ops in paths.items() if needle in p for m in ops}


SECRET = "sk-ant-abcdefghijklmnopqrst1234"


# ------------------------------------------------------------ gatekeeping


def test_unconfigured_token_fails_closed() -> None:
    bridge = Bridge(token=None)
    r = bridge.client.get("/desktop/v1/status", headers=HEADERS)
    assert r.status_code == 503


def test_missing_and_wrong_token_rejected() -> None:
    bridge = Bridge()
    assert bridge.client.get("/desktop/v1/status").status_code == 401
    bad = {"x-sam-desktop-token": "x" * 40}
    assert bridge.client.get("/desktop/v1/status", headers=bad).status_code == 401
    assert bridge.get("/status").status_code == 200


def test_origin_header_rejected() -> None:
    bridge = Bridge()
    r = bridge.client.get(
        "/desktop/v1/status", headers={**HEADERS, "origin": "https://evil.example"}
    )
    assert r.status_code == 403


def test_non_loopback_host_and_peer_rejected() -> None:
    bridge = Bridge()
    r = bridge.client.get(
        "/desktop/v1/status", headers={**HEADERS, "host": "evil.example"}
    )
    assert r.status_code == 403
    remote = TestClient(bridge.app, base_url="http://127.0.0.1", client=("10.1.2.3", 1))
    assert remote.get("/desktop/v1/status", headers=HEADERS).status_code == 403


def test_oversized_declared_body_rejected() -> None:
    bridge = Bridge()
    r = bridge.client.post(
        "/desktop/v1/chat",
        content=b"{}",
        headers={
            **HEADERS,
            "content-length": "999999999",
            "content-type": "application/json",
        },
    )
    assert r.status_code in (400, 413)


def test_error_bodies_do_not_leak_the_token() -> None:
    bridge = Bridge()
    r = bridge.client.get(
        "/desktop/v1/status", headers={"x-sam-desktop-token": "y" * 40}
    )
    assert TOKEN not in r.text


# -------------------------------------------------- principal / extra-forbid


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/chat", {"message": "hi", "principal": "root"}),
        ("/chat", {"message": "hi", "scope": "*"}),
        ("/knowledge/query", {"query": "x", "risk": "low"}),
        (
            "/knowledge/ingest",
            {
                "name": "a.txt",
                "resource_type": "txt",
                "content_base64": "aGk=",
                "allow": True,
            },
        ),
        ("/knowledge/remove", {"resource_id": "r", "principal": "x"}),
        ("/memory/search", {"text": "x", "principal": "x"}),
        ("/permissions/revoke", {"grant_id": "g", "scope": "*"}),
        (
            "/confirmations/decide",
            {"confirmation_id": "c", "approved": True, "principal": "x"},
        ),
        ("/voice/utterance", {"audio_base64": "AA==", "principal": "x"}),
        (
            "/tts/speak",
            {"text": "hi", "voice_profile": "sam_default", "endpoint": "http://x"},
        ),
    ],
)
def test_client_supplied_authority_fields_are_rejected(
    path: str, body: dict[str, Any]
) -> None:
    bridge = Bridge(stt=FakeTranscriptionProvider(), tts=FakeSpeechSynthesisProvider())
    assert bridge.post(path, body).status_code == 422


def test_chat_request_forbids_extras() -> None:
    assert ChatRequest.model_config.get("extra") == "forbid"


# ------------------------------------------------------------------ status


def test_status_is_honest_about_capabilities() -> None:
    body = Bridge(agent_configured=False).get("/status").json()
    assert body["agent"] == "not_configured"
    assert body["voice_input"] == "not_configured"
    assert body["speech_output"] == "not_configured"
    assert body["tools"] == "foundation_ready"
    assert body["computer_control"] == "foundation_ready"
    assert body["speech_profiles"] == []
    configured = Bridge(
        tts=FakeSpeechSynthesisProvider(), stt=FakeTranscriptionProvider()
    )
    body = configured.get("/status").json()
    assert body["speech_output"] == "configured"
    assert body["voice_input"] == "configured"
    assert [p["profile_id"] for p in body["speech_profiles"]] == ["sam_default"]
    assert "abcdef0123456789" not in json.dumps(body)


# -------------------------------------------------------------------- chat


def test_chat_uses_agent_core_only() -> None:
    bridge = Bridge(agent=StubAgent("hello"))
    body = bridge.post("/chat", {"message": "hey"}).json()
    assert body["status"] == "ok"
    assert body["reply"] == "hello"
    assert bridge.agent.messages == ["hey"]


def test_chat_not_configured_never_calls_agent() -> None:
    bridge = Bridge(agent_configured=False)
    body = bridge.post("/chat", {"message": "hey"}).json()
    assert body["status"] == "not_configured"
    assert bridge.agent.messages == []


def test_chat_agent_error_is_sanitized_with_reference() -> None:
    err = AgentError("boom sk-ant-SECRETSECRETSECRET1234 traceback")
    err.error_code = "provider_timeout"
    bridge = Bridge(agent=StubAgent(raises=err))
    body = bridge.post("/chat", {"message": "hey"}).json()
    assert body["status"] == "failed"
    assert body["reference_id"]
    assert "SECRET" not in json.dumps(body)
    assert "boom" not in json.dumps(body)


def test_chat_unexpected_exception_is_sanitized() -> None:
    bridge = Bridge(agent=StubAgent(raises=RuntimeError("secret internal path /etc/x")))
    body = bridge.post("/chat", {"message": "hey"}).json()
    assert body["status"] == "failed"
    assert "/etc/x" not in json.dumps(body)


def test_chat_does_not_write_memory_or_knowledge() -> None:
    bridge = Bridge()
    bridge.post("/chat", {"message": "remember that I like tea"})
    assert bridge.get("/knowledge/resources").json()["resources"] == []
    mem = bridge.post("/memory/search", {"text": "tea"}).json()
    assert mem["items"] == [] and mem["working"] == []


# --------------------------------------------------------------- knowledge


def ingest(bridge: Bridge, text: str = "Sam likes green tea.\n\nTea is warm.") -> Any:
    return bridge.post(
        "/knowledge/ingest",
        {
            "name": "notes.txt",
            "resource_type": "txt",
            "content_base64": b64(text.encode()),
        },
    ).json()


def test_knowledge_ingest_list_query_provenance() -> None:
    bridge = Bridge()
    added = ingest(bridge)
    assert added["status"] == "ok", added
    assert added["resource"]["name"] == "notes.txt"
    listed = bridge.get("/knowledge/resources").json()
    assert [r["name"] for r in listed["resources"]] == ["notes.txt"]
    hits = bridge.post("/knowledge/query", {"query": "green tea"}).json()["hits"]
    assert hits and hits[0]["resource_name"] == "notes.txt"
    assert hits[0]["location"]["character_start"] is not None
    # Fields the backend did not provide stay absent, never invented.
    assert hits[0]["location"]["page_number"] is None


def test_knowledge_duplicate_is_reported_not_hidden() -> None:
    bridge = Bridge()
    ingest(bridge)
    again = ingest(bridge)
    assert again["reason_code"] == "duplicate_resource" or again["status"] != "failed"


def test_knowledge_ingest_bad_base64_and_type() -> None:
    bridge = Bridge()
    bad = bridge.post(
        "/knowledge/ingest",
        {"name": "a.txt", "resource_type": "txt", "content_base64": "!!!not base64"},
    ).json()
    assert bad["status"] == "rejected"
    assert (
        bridge.post(
            "/knowledge/ingest",
            {"name": "a.exe", "resource_type": "exe", "content_base64": "aGk="},
        ).status_code
        == 422
    )


def test_knowledge_secret_document_is_rejected() -> None:
    bridge = Bridge()
    body = ingest(bridge, f"config: {SECRET}")
    assert body["status"] in ("rejected", "failed")
    assert bridge.get("/knowledge/resources").json()["resources"] == []
    assert SECRET not in json.dumps(body)


def test_knowledge_remove_requires_confirmation_then_succeeds_once() -> None:
    bridge = Bridge()
    rid = ingest(bridge)["resource"]["resource_id"]
    first = bridge.post("/knowledge/remove", {"resource_id": rid}).json()
    assert first["status"] == "confirmation_required"
    challenge = first["challenge"]
    assert challenge["risk"] in ("high", "critical", "medium")
    cid = challenge["confirmation_id"]
    # Nothing removed before approval.
    assert len(bridge.get("/knowledge/resources").json()["resources"]) == 1
    # Approval alone does not remove anything; the engine re-checks on retry.
    assert (
        bridge.post(
            "/confirmations/decide", {"confirmation_id": cid, "approved": True}
        ).json()["status"]
        == "approved"
    )
    assert len(bridge.get("/knowledge/resources").json()["resources"]) == 1
    done = bridge.post(
        "/knowledge/remove", {"resource_id": rid, "confirmation_id": cid}
    ).json()
    assert done["status"] == "ok"
    assert bridge.get("/knowledge/resources").json()["resources"] == []
    # One-time use.
    ingest(bridge)
    rid2 = bridge.get("/knowledge/resources").json()["resources"][0]["resource_id"]
    replay = bridge.post(
        "/knowledge/remove", {"resource_id": rid2, "confirmation_id": cid}
    ).json()
    assert replay["status"] != "ok"
    assert len(bridge.get("/knowledge/resources").json()["resources"]) == 1


def test_denied_confirmation_does_not_remove() -> None:
    bridge = Bridge()
    rid = ingest(bridge)["resource"]["resource_id"]
    cid = bridge.post("/knowledge/remove", {"resource_id": rid}).json()["challenge"][
        "confirmation_id"
    ]
    assert (
        bridge.post(
            "/confirmations/decide", {"confirmation_id": cid, "approved": False}
        ).json()["status"]
        == "denied"
    )
    r = bridge.post(
        "/knowledge/remove", {"resource_id": rid, "confirmation_id": cid}
    ).json()
    assert r["status"] != "ok"
    assert len(bridge.get("/knowledge/resources").json()["resources"]) == 1


def test_confirmation_bound_to_its_own_operation() -> None:
    bridge = Bridge()
    a = ingest(bridge, "alpha doc content")["resource"]["resource_id"]
    b = ingest(bridge, "beta doc content different")["resource"]["resource_id"]
    cid = bridge.post("/knowledge/remove", {"resource_id": a}).json()["challenge"][
        "confirmation_id"
    ]
    bridge.post("/confirmations/decide", {"confirmation_id": cid, "approved": True})
    wrong = bridge.post(
        "/knowledge/remove", {"resource_id": b, "confirmation_id": cid}
    ).json()
    assert wrong["status"] != "ok"
    assert len(bridge.get("/knowledge/resources").json()["resources"]) == 2


def test_decide_unknown_and_already_decided() -> None:
    bridge = Bridge()
    assert (
        bridge.post(
            "/confirmations/decide", {"confirmation_id": "nope", "approved": True}
        ).status_code
        == 404
    )
    rid = ingest(bridge)["resource"]["resource_id"]
    cid = bridge.post("/knowledge/remove", {"resource_id": rid}).json()["challenge"][
        "confirmation_id"
    ]
    assert (
        bridge.post(
            "/confirmations/decide", {"confirmation_id": cid, "approved": True}
        ).status_code
        == 200
    )
    assert (
        bridge.post(
            "/confirmations/decide", {"confirmation_id": cid, "approved": False}
        ).status_code
        == 404
    )


# ------------------------------------------------------------------ memory


def test_memory_surface_is_read_only() -> None:
    bridge = Bridge()
    routes = route_set(bridge, "memory")
    assert routes == {("POST", "/desktop/v1/memory/search")}
    assert bridge.post("/memory/search", {"text": "x"}).json()["status"] == "ok"


# ------------------------------------------------------------------- tools


def test_tools_empty_registry_is_foundation_ready() -> None:
    body = Bridge().get("/tools").json()
    assert body == {"state": "foundation_ready", "servers": [], "tools": []}


def test_tools_view_has_no_mutating_routes() -> None:
    bridge = Bridge()
    assert {m for m, _ in route_set(bridge, "/tools")} == {"GET"}
    assert route_set(bridge, "/tools")
    assert not hasattr(bridge.runtime, "mcp_admin")
    assert not hasattr(bridge.runtime.mcp_registry, "register_tool")


# ------------------------------------------------------------- permissions


def test_permissions_list_and_revoke_only() -> None:
    bridge = Bridge()
    grants = bridge.get("/permissions").json()["grants"]
    assert grants and all(g["status"] == "active" for g in grants)
    assert not any(g["scope"] == "*" for g in grants)
    assert {g["resource"] for g in grants} == {"knowledge"}
    ident = next(
        g["grant_id"]
        for g in grants
        if g["action"] == "read" and g["scope"].endswith("retrieve")
    )
    assert (
        bridge.post("/permissions/revoke", {"grant_id": ident}).json()["status"] == "ok"
    )
    after = bridge.post("/knowledge/query", {"query": "x"}).json()
    assert after["status"] == "denied"
    routes = {p for _, p in route_set(bridge, "permissions")}
    assert routes == {"/desktop/v1/permissions", "/desktop/v1/permissions/revoke"}


def test_revoke_unknown_grant_rejected() -> None:
    assert (
        Bridge().post("/permissions/revoke", {"grant_id": "zzz"}).json()["status"]
        == "rejected"
    )


# ---------------------------------------------------------------- activity


def test_activity_is_content_free() -> None:
    bridge = Bridge()
    bridge.post("/chat", {"message": "my secret plan is purple elephants"})
    ingest(bridge, "purple elephants document")
    bridge.post("/knowledge/query", {"query": "purple elephants"})
    text = json.dumps(bridge.get("/activity").json())
    assert "purple" not in text and "elephants" not in text and "notes.txt" not in text
    assert bridge.get("/activity").json()["items"]


# ------------------------------------------------------------------- voice


def test_voice_not_configured() -> None:
    body = Bridge().post("/voice/utterance", {"audio_base64": wav_b64()}).json()
    assert body["status"] == "not_configured"


def test_voice_without_owner_identity_fails_closed_and_text_chat_still_works() -> None:
    """No identity runtime: voice is refused (no legacy owner voice session),
    nothing is transcribed or forwarded, and text chat is unaffected."""

    stt = FakeTranscriptionProvider("what is the weather")
    bridge = Bridge(stt=stt)
    body = bridge.post("/voice/utterance", {"audio_base64": wav_b64()}).json()
    assert body["status"] == "denied"
    assert body["reason_code"] == "identity_not_configured"
    assert body.get("transcript") is None and body.get("speaker") is None
    assert stt.call_count == 0 and bridge.agent.messages == []
    chat = bridge.post("/chat", {"message": "hello"})
    assert chat.status_code == 200 and bridge.agent.messages == ["hello"]


def test_voice_secret_transcript_is_withheld() -> None:
    stt = FakeTranscriptionProvider(f"my key is {SECRET}")
    bridge = Bridge(stt=stt)
    body = bridge.post("/voice/utterance", {"audio_base64": wav_b64()}).json()
    assert bridge.agent.messages == []
    assert SECRET not in json.dumps(body)
    assert body["status"] != "ok" or body.get("forwarded_to_agent") is False


def test_voice_malformed_audio_never_reaches_the_agent() -> None:
    bridge = Bridge(stt=FakeTranscriptionProvider())
    for audio in (b64(b"not a wav"), "###"):
        body = bridge.post("/voice/utterance", {"audio_base64": audio}).json()
        assert body["status"] == "denied"  # identity gate comes first
    assert bridge.agent.messages == []


# --------------------------------------------------------------------- tts


def test_tts_not_configured() -> None:
    body = (
        Bridge()
        .post("/tts/speak", {"text": "hi", "voice_profile": "sam_default"})
        .json()
    )
    assert body["status"] == "not_configured"


def speak_confirmed(bridge: Bridge, body: dict[str, Any]) -> Any:
    """Speech SEND always asks first; approve the challenge and retry."""

    first = bridge.post("/tts/speak", body).json()
    if first["status"] != "confirmation_required":
        return first
    cid = first["challenge"]["confirmation_id"]
    bridge.post("/confirmations/decide", {"confirmation_id": cid, "approved": True})
    return bridge.post("/tts/speak", {**body, "confirmation_id": cid}).json()


def test_tts_speaks_with_trusted_profile_only() -> None:
    tts = FakeSpeechSynthesisProvider()
    bridge = Bridge(tts=tts)
    body = speak_confirmed(bridge, {"text": "hello", "voice_profile": "sam_default"})
    assert body["status"] == "ok", body
    assert body["audio_base64"] and body["byte_length"] > 0
    assert tts.seen_voices == ["abcdef0123456789abcdef0123456789"]
    unknown = speak_confirmed(
        bridge, {"text": "hello", "voice_profile": "attacker_voice"}
    )
    assert unknown["status"] != "ok"
    assert tts.call_count == 1


def test_speech_send_is_never_silently_authorized() -> None:
    """SEND sends the user's text to an external provider: it must ask first,
    every time, and a deny or replay must never reach the provider."""

    tts = FakeSpeechSynthesisProvider()
    bridge = Bridge(tts=tts)
    body = {"text": "read this", "voice_profile": "sam_default"}
    first = bridge.post("/tts/speak", body).json()
    assert first["status"] == "confirmation_required"
    assert first["challenge"]["action"] == "send"
    assert first["challenge"]["resource"] == "speech_synthesis"
    assert tts.call_count == 0
    cid = first["challenge"]["confirmation_id"]
    bridge.post("/confirmations/decide", {"confirmation_id": cid, "approved": False})
    assert (
        bridge.post("/tts/speak", {**body, "confirmation_id": cid}).json()["status"]
        != "ok"
    )
    assert tts.call_count == 0
    second = bridge.post("/tts/speak", body).json()
    assert second["status"] == "confirmation_required"  # asks again, every time
    cid2 = second["challenge"]["confirmation_id"]
    bridge.post("/confirmations/decide", {"confirmation_id": cid2, "approved": True})
    ok = bridge.post("/tts/speak", {**body, "confirmation_id": cid2}).json()
    assert ok["status"] == "ok" and tts.call_count == 1
    replay = bridge.post("/tts/speak", {**body, "confirmation_id": cid2}).json()
    assert replay["status"] != "ok" and tts.call_count == 1


def test_no_bootstrap_grant_silently_authorizes_a_consequential_action() -> None:
    full = Bridge(stt=FakeTranscriptionProvider(), tts=FakeSpeechSynthesisProvider())
    grants = full.get("/permissions").json()["grants"]
    assert {g["action"] for g in grants} <= {
        "read",
        "write",
        "create",
        "update",
        "delete",
        "send",
    }
    for g in grants:
        assert g["action"] not in {"publish", "execute", "approve"}
        if g["action"] in {"send", "delete"}:
            assert g["requires_confirmation"] is True, g


def test_tts_secret_text_is_refused() -> None:
    tts = FakeSpeechSynthesisProvider()
    bridge = Bridge(tts=tts)
    body = bridge.post(
        "/tts/speak", {"text": f"say {SECRET}", "voice_profile": "sam_default"}
    ).json()
    assert body["status"] != "ok"
    assert tts.call_count == 0
    assert SECRET not in json.dumps(body)


def test_tts_provider_error_is_sanitized() -> None:
    tts = FakeSpeechSynthesisProvider(raises=RuntimeError("upstream leak key=abc"))
    body = (
        Bridge(tts=tts)
        .post("/tts/speak", {"text": "hi", "voice_profile": "sam_default"})
        .json()
    )
    assert body["status"] != "ok"
    assert "leak" not in json.dumps(body)


# ---------------------------------------------------------- static hygiene


def test_router_has_only_the_documented_routes() -> None:
    bridge = Bridge()
    routes = sorted(route_set(bridge))
    assert routes == sorted(
        [
            ("GET", "/desktop/v1/status"),
            ("POST", "/desktop/v1/chat"),
            ("GET", "/desktop/v1/knowledge/resources"),
            ("POST", "/desktop/v1/knowledge/query"),
            ("POST", "/desktop/v1/knowledge/ingest"),
            ("POST", "/desktop/v1/knowledge/remove"),
            ("POST", "/desktop/v1/memory/search"),
            ("GET", "/desktop/v1/tools"),
            ("GET", "/desktop/v1/permissions"),
            ("POST", "/desktop/v1/permissions/revoke"),
            ("GET", "/desktop/v1/activity"),
            ("POST", "/desktop/v1/confirmations/decide"),
            ("POST", "/desktop/v1/voice/utterance"),
            ("POST", "/desktop/v1/tts/speak"),
            # Phase 12: owner voice identity and Guest Mode
            ("GET", "/desktop/v1/voice/identity"),
            ("POST", "/desktop/v1/voice/identity/enroll/begin"),
            ("POST", "/desktop/v1/voice/identity/enroll/sample"),
            ("POST", "/desktop/v1/voice/identity/enroll/complete"),
            ("POST", "/desktop/v1/voice/identity/enroll/cancel"),
            ("POST", "/desktop/v1/voice/identity/delete"),
            ("POST", "/desktop/v1/voice/guest/challenge"),
            ("POST", "/desktop/v1/voice/guest/start"),
            ("POST", "/desktop/v1/voice/guest/end"),
            # Phase 13: AI provider status and owner routing/privacy preferences
            ("GET", "/desktop/v1/models"),
            ("POST", "/desktop/v1/models/preferences"),
        ]
    )


def test_desktop_source_has_no_generic_capabilities() -> None:
    import pathlib

    root = pathlib.Path(desktop_api.__file__).parent
    text = "\n".join(p.read_text() for p in root.glob("*.py"))
    for banned in (
        "subprocess",
        "os.system",
        "shell=True",
        "eval(",
        "exec(",
        "requests.",
        "httpx",
        "urllib",
        "socket",
        "open(",
        "PermissionGrant(",
        "MCPRegistryAdmin()",
    ):
        if banned in text:
            # the runtime is the single trusted place allowed to build these
            assert banned in ("PermissionGrant(", "MCPRegistryAdmin()"), banned
    api_text = pathlib.Path(desktop_api.__file__).read_text()
    assert "PermissionGrant(" not in api_text
    assert "create_grant" not in api_text
    assert "allow_all" not in api_text
    assert inspect.getsource(desktop_api).count("@router.") == 14


# --------------------------------------------- CRITICAL step-up / audit

STEP_UP = "correct-horse-battery-staple"


def critical_challenge(bridge: Bridge) -> str:
    from sam.permissions.models import (
        PermissionAction,
        PermissionRequest,
        PermissionResource,
        PermissionScope,
        RiskLevel,
    )

    record = bridge.runtime.confirmations.request(
        PermissionRequest(
            principal=bridge.runtime.principal,
            action=PermissionAction.DELETE,
            resource=PermissionResource.KNOWLEDGE,
            scope=PermissionScope.from_path("default"),
            target="everything",
        ),
        risk=RiskLevel.CRITICAL,
        now=bridge.runtime.clock(),
    )
    return record.confirmation_id


def with_step_up(secret: str | None = STEP_UP) -> Bridge:
    from pydantic import SecretStr

    bridge = Bridge()
    if secret is not None:
        bridge.settings.desktop_step_up_secret = SecretStr(secret)
    return bridge


def test_critical_approval_is_refused_when_no_step_up_secret_is_configured() -> None:
    bridge = with_step_up(None)
    cid = critical_challenge(bridge)
    r = bridge.post("/confirmations/decide", {"confirmation_id": cid, "approved": True})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "step_up_unavailable"
    # A click (or an acknowledgement flag) can never approve it.
    r = bridge.post(
        "/confirmations/decide",
        {"confirmation_id": cid, "approved": True, "step_up": "anything-at-all-here"},
    )
    assert r.status_code == 403


def test_critical_approval_needs_the_right_step_up_secret() -> None:
    bridge = with_step_up()
    cid = critical_challenge(bridge)
    body = {"confirmation_id": cid, "approved": True}
    assert bridge.post("/confirmations/decide", body).status_code == 403
    bad = bridge.post(
        "/confirmations/decide", {**body, "step_up": "wrong-secret-value"}
    )
    assert bad.json()["detail"]["code"] == "step_up_failed"
    ok = bridge.post("/confirmations/decide", {**body, "step_up": STEP_UP})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"


def test_critical_step_up_locks_after_three_failures_and_denies() -> None:
    bridge = with_step_up()
    cid = critical_challenge(bridge)
    body = {"confirmation_id": cid, "approved": True}
    codes = [
        bridge.post(
            "/confirmations/decide", {**body, "step_up": f"wrong-secret-{i}"}
        ).json()["detail"]["code"]
        for i in range(3)
    ]
    assert codes == ["step_up_failed", "step_up_failed", "step_up_locked"]
    # Even the correct secret is now useless: the confirmation was denied.
    r = bridge.post("/confirmations/decide", {**body, "step_up": STEP_UP})
    assert r.status_code == 404
    record = bridge.runtime.confirmations.get(cid)
    assert record is not None and record.status.value == "denied"


def test_denying_a_critical_confirmation_needs_no_step_up() -> None:
    bridge = with_step_up()
    cid = critical_challenge(bridge)
    r = bridge.post(
        "/confirmations/decide", {"confirmation_id": cid, "approved": False}
    )
    assert r.status_code == 200 and r.json()["status"] == "denied"


def test_step_up_secret_never_appears_in_responses_or_activity() -> None:
    bridge = with_step_up()
    cid = critical_challenge(bridge)
    r = bridge.post(
        "/confirmations/decide",
        {"confirmation_id": cid, "approved": True, "step_up": "wrong-secret-value"},
    )
    text = r.text + json.dumps(bridge.get("/activity").json())
    assert "wrong-secret-value" not in text and STEP_UP not in text


def test_non_critical_confirmations_do_not_use_step_up() -> None:
    bridge = with_step_up()
    rid = ingest(bridge)["resource"]["resource_id"]
    cid = bridge.post("/knowledge/remove", {"resource_id": rid}).json()["challenge"][
        "confirmation_id"
    ]
    r = bridge.post("/confirmations/decide", {"confirmation_id": cid, "approved": True})
    assert r.status_code == 200


def test_bootstrap_grants_are_exactly_the_documented_set_and_audited() -> None:
    """Exhaustive review of what authority the desktop starts with."""

    bare = Bridge()
    got = {
        (g["resource"], g["action"], g["scope"])
        for g in bare.get("/permissions").json()["grants"]
    }
    assert got == {
        ("knowledge", "write", "default:ingest"),
        ("knowledge", "read", "default:list"),
        ("knowledge", "read", "default:retrieve"),
        ("knowledge", "delete", "default"),
    }
    full = Bridge(stt=FakeTranscriptionProvider(), tts=FakeSpeechSynthesisProvider())
    grants = full.get("/permissions").json()["grants"]
    got = {(g["resource"], g["action"], g["scope"]) for g in grants}
    assert got - {
        ("knowledge", "write", "default:ingest"),
        ("knowledge", "read", "default:list"),
        ("knowledge", "read", "default:retrieve"),
        ("knowledge", "delete", "default"),
    } == {
        ("voice", "create", "session"),
        ("voice", "read", "session"),
        ("voice", "update", "session"),
        ("speech_synthesis", "send", "fake-tts/sam_default"),
    }
    for g in grants:
        assert g["origin"] == "desktop_bootstrap"
        assert g["expires_at"] is None and g["status"] == "active"
        assert "*" not in g["scope"] and g["scope"] != ""
    # Nothing for MCP/computer/coding/memory/email/etc.
    assert {g["resource"] for g in grants} <= {"knowledge", "voice", "speech_synthesis"}
    # One content-free audit label per grant, visible in Activity.
    labels = [
        i["label"]
        for i in full.get("/activity").json()["items"]
        if i["label"].startswith("Bootstrap grant:")
    ]
    assert len(labels) == len(grants)
    assert any("knowledge delete default" in label for label in labels)


def test_step_up_secret_hygiene_logs_audit_errors_activity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    bridge = with_step_up()
    cid = critical_challenge(bridge)
    typed = "typed-wrong-secret-XYZ"
    seen: list[str] = []
    for _ in range(3):  # wrong x3 -> locked -> denied
        seen.append(
            bridge.post(
                "/confirmations/decide",
                {"confirmation_id": cid, "approved": True, "step_up": typed},
            ).text
        )
    cid2 = critical_challenge(bridge)
    seen.append(
        bridge.post(
            "/confirmations/decide",
            {"confirmation_id": cid2, "approved": True, "step_up": STEP_UP},
        ).text
    )
    haystacks = [
        *seen,
        json.dumps(bridge.get("/activity").json()),
        json.dumps(bridge.get("/permissions").json()),
        "\n".join(r.getMessage() for r in caplog.records),
        repr(bridge.settings),
        str(bridge.settings.model_dump()),
        "\n".join(
            e.model_dump_json() for e in bridge.runtime.permission_audit.list_events()
        ),
        "\n".join(
            r.model_dump_json() for r in bridge.runtime.confirmations._list_for_test()
        ),
    ]
    for text in haystacks:
        assert typed not in text and STEP_UP not in text
    from sam.desktop.models import ConfirmationDecisionRequest

    request = ConfirmationDecisionRequest(
        confirmation_id="c",
        approved=True,
        step_up=typed,  # type: ignore[arg-type]
    )
    assert typed not in repr(request) and typed not in str(request)


def test_step_up_secret_and_bridge_token_are_distinct_in_the_test_setup() -> None:
    bridge = with_step_up()
    assert bridge.settings.desktop_step_up_secret is not None
    assert bridge.settings.desktop_bridge_token is not None
    assert (
        bridge.settings.desktop_step_up_secret.get_secret_value()
        != bridge.settings.desktop_bridge_token.get_secret_value()
    )
