"""The Gemini Free Tier adapter, driven only by httpx.MockTransport.

No live network and no real key. Verifies the exact outbound request, endpoint
and model pinning, redirect/environment behavior, error containment, bounds,
and the one-request / no-retry rule.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from sam.agent.models import Message, MessageRole
from sam.models.credentials import ProviderCredential
from sam.models.errors import ProviderFailure
from sam.models.models import (
    Availability,
    BillingMode,
    FailureCategory,
    GeminiFreeAttestation,
    ModelRequest,
    PrivacyClass,
    ProviderId,
)
from sam.models.providers import gemini
from sam.models.providers.gemini import (
    GEMINI_ALLOWED_MODELS,
    GeminiFreeProvider,
    build_body,
    build_client,
    endpoint_for,
)
from sam.models.registry import GEMINI_EFFICIENT_MODEL, GEMINI_GENERAL_MODEL
from tests.models_support import (
    FAKE_GEMINI_KEY,
    PRIVATE_TEXT,
    audit_json,
    make_rig,
    request,
)

MODEL = GEMINI_GENERAL_MODEL
PERSIAN = "سلام سام، این API با FastAPI کار می‌کند؟"


class Recorder:
    def __init__(self, response: Any = None) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        r = self._response
        if r is None:
            return httpx.Response(200, json=ok_body("Paris."))
        response: httpx.Response = r(req) if callable(r) else r
        return response


def ok_body(text: str = "hello") -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 7,
            "candidatesTokenCount": 3,
            "totalTokenCount": 10,
        },
    }


ATTESTED = GeminiFreeAttestation.OWNER_ATTESTED_UNBILLED


def provider(
    handler: Callable[..., httpx.Response] | Recorder,
    attestation: GeminiFreeAttestation = ATTESTED,
) -> GeminiFreeProvider:
    return GeminiFreeProvider(
        ProviderCredential(FAKE_GEMINI_KEY),
        transport=httpx.MockTransport(handler),
        attestation=lambda: attestation,
    )


def call(
    p: GeminiFreeProvider, req: ModelRequest | None = None, model: str = MODEL
) -> Any:
    return p.complete(req or request(), model_id=model, timeout_seconds=5)


def expect(
    p: GeminiFreeProvider, category: FailureCategory, code: str | None = None
) -> ProviderFailure:
    with pytest.raises(ProviderFailure) as caught:
        call(p)
    assert caught.value.category is category, caught.value.code
    if code is not None:
        assert caught.value.code == code
    return caught.value


# ------------------------------------------------------- request shape


def test_request_matches_the_documented_generate_content_schema() -> None:
    rec = Recorder()
    req = ModelRequest(
        request_id="r1",
        messages=(
            Message(role=MessageRole.SYSTEM, content="Answer in French."),
            Message(role=MessageRole.USER, content=PERSIAN),
            Message(role=MessageRole.ASSISTANT, content="prior answer"),
            Message(role=MessageRole.USER, content="and now?"),
        ),
        max_output_tokens=256,
    )
    out = call(provider(rec), req)
    (sent,) = rec.requests
    assert sent.method == "POST"
    assert (
        str(sent.url)
        == f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    )
    assert sent.url.query == b"" and FAKE_GEMINI_KEY not in str(sent.url)
    assert sent.headers["x-goog-api-key"] == FAKE_GEMINI_KEY
    assert (
        "authorization" not in sent.headers
        and sent.headers["content-type"] == "application/json"
    )
    body = json.loads(sent.content)
    assert body == {
        "contents": [
            {"role": "user", "parts": [{"text": PERSIAN}]},
            {"role": "model", "parts": [{"text": "prior answer"}]},
            {"role": "user", "parts": [{"text": "and now?"}]},
        ],
        "generationConfig": {"maxOutputTokens": 256},
        "systemInstruction": {"parts": [{"text": "Answer in French."}]},
    }
    assert FAKE_GEMINI_KEY not in sent.content.decode()
    assert PERSIAN.encode() in sent.content  # Persian is sent as UTF-8, not escaped
    assert out.text == "Paris." and out.model_id == MODEL
    assert out.usage is not None and (
        out.usage.input_tokens,
        out.usage.output_tokens,
    ) == (7, 3)


def test_sam_sets_no_safety_settings_and_no_tools() -> None:
    body = build_body(request())
    assert set(body) <= {"contents", "generationConfig", "systemInstruction"}
    assert (
        "safetySettings" not in body
        and "tools" not in body
        and "toolConfig" not in body
    )


# -------------------------------------- pinning: endpoint, model, client


def test_only_allowlisted_models_can_be_requested_and_no_call_precedes_the_check() -> (
    None
):
    assert GEMINI_ALLOWED_MODELS == {GEMINI_GENERAL_MODEL, GEMINI_EFFICIENT_MODEL}
    assert endpoint_for(GEMINI_EFFICIENT_MODEL).endswith(
        f"{GEMINI_EFFICIENT_MODEL}:generateContent"
    )
    rec = Recorder()
    p = provider(rec)
    for bad in (
        "gemini-1.5-pro",
        "gemini-3.8-flash/../../evil",
        "gemini-3.8-flash?key=x",
        "",
        "models/x",
        "gemini-3.8-flash-preview-experimental",
    ):
        with pytest.raises(ProviderFailure) as caught:
            call(p, model=bad)
        assert caught.value.category is FailureCategory.POLICY_BLOCKED
        with pytest.raises(ProviderFailure):
            endpoint_for(bad)
    assert rec.requests == []  # refused before the key is resolved or anything is sent


def test_there_is_no_endpoint_parameter_and_the_client_is_hardened() -> None:
    assert set(inspect.signature(GeminiFreeProvider.__init__).parameters) == {
        "self",
        "credential",
        "transport",
        "attestation",
    }
    client = build_client(None, 5.0)
    assert client.follow_redirects is False and client.trust_env is False
    assert gemini.GEMINI_HOST == "generativelanguage.googleapis.com"


def test_proxy_and_ssl_environment_cannot_redirect_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.invalid:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent")
    rec = Recorder()
    call(provider(rec))
    assert len(rec.requests) == 1 and build_client(None, 5.0).trust_env is False


def test_a_redirect_is_refused_and_never_followed() -> None:
    rec = Recorder(httpx.Response(302, headers={"location": "https://evil.invalid/x"}))
    expect(provider(rec), FailureCategory.UNAVAILABLE, "redirect_refused")
    assert len(rec.requests) == 1


# ------------------------------------------ one request, no retry, safe errors


@pytest.mark.parametrize(
    ("status", "category", "code"),
    [
        (429, FailureCategory.RATE_LIMIT, "free_quota_exhausted"),
        (401, FailureCategory.AUTH_FAILURE, "not_authorized"),
        (403, FailureCategory.AUTH_FAILURE, "not_authorized"),
        (400, FailureCategory.UNAVAILABLE, "http_400"),
        (404, FailureCategory.UNAVAILABLE, "http_404"),
        (500, FailureCategory.UNAVAILABLE, "http_500"),
        (503, FailureCategory.UNAVAILABLE, "http_503"),
    ],
)
def test_error_statuses_map_to_safe_categories_with_exactly_one_call(
    status: int, category: FailureCategory, code: str
) -> None:
    body = f"echo {FAKE_GEMINI_KEY} {PRIVATE_TEXT}".encode()
    rec = Recorder(httpx.Response(status, content=body))
    failure = expect(provider(rec), category, code)
    assert len(rec.requests) == 1  # no retry, ever
    assert FAKE_GEMINI_KEY not in str(failure) and PRIVATE_TEXT not in str(failure)


def test_quota_exhaustion_never_enables_billing_or_reaches_another_provider() -> None:
    rig = make_rig(claude_enabled=False)
    from sam.models.audit import InMemoryRoutingAuditSink
    from sam.models.policies import OwnerPreferences, PersonalContentPolicy
    from sam.models.registry import build_registry
    from sam.models.router import ModelRouter

    rec = Recorder(httpx.Response(429))
    audit = InMemoryRoutingAuditSink()
    router = ModelRouter(
        registry=build_registry(gemini_configured=True, claude_enabled=False),
        providers={
            ProviderId.GEMINI_FREE: provider(rec),
            ProviderId.OPENAI_API: rig.openai,
            ProviderId.GROK_API: rig.grok,
        },
        preferences=lambda: OwnerPreferences(gemini_attestation=ATTESTED),
        content_policy=PersonalContentPolicy,
        audit=audit,
    )
    result = router.execute(request())
    assert (
        result.status is FailureCategory.RATE_LIMIT
        and result.diagnostic_code == "free_quota_exhausted"
    )
    assert len(rec.requests) == 1 and rig.paid_calls() == 0
    assert audit.events()[0].cost_class is BillingMode.FREE_TIER


def test_timeout_is_typed_and_not_retried() -> None:
    def slow(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    rec = Recorder(slow)
    expect(provider(rec), FailureCategory.TIMEOUT, "timeout")
    assert len(rec.requests) == 1


def test_transport_failures_are_generic_and_never_contain_the_key() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {FAKE_GEMINI_KEY}", request=req)

    failure = expect(
        provider(Recorder(boom)), FailureCategory.UNAVAILABLE, "request_failed"
    )
    assert FAKE_GEMINI_KEY not in str(failure) and failure.__cause__ is None


# ---------------------------------------------------- hostile responses


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, json=[1, 2, 3]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"candidates": []}),
        httpx.Response(200, json={"candidates": ["x"]}),
        httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"inlineData": {}}]}, "finishReason": "STOP"}
                ]
            },
        ),
        httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "   "}]}}]}
        ),
        httpx.Response(200, content=b"x", headers={"content-encoding": "gzip"}),
        httpx.Response(200, headers={"content-length": str(50 * 1024 * 1024)}),
        httpx.Response(200, headers={"content-length": "abc"}),
    ],
    ids=[
        "html",
        "list",
        "empty-object",
        "no-candidates",
        "bad-candidate",
        "no-text",
        "blank-text",
        "gzip",
        "huge-length",
        "bad-length",
    ],
)
def test_malformed_or_hostile_responses_fail_closed(response: httpx.Response) -> None:
    expect(provider(Recorder(response)), FailureCategory.INVALID_RESPONSE)


def test_an_oversized_streamed_body_is_bounded() -> None:
    big = httpx.Response(200, content=b"x" * (gemini.MAX_RESPONSE_BYTES + 10))
    expect(provider(Recorder(big)), FailureCategory.INVALID_RESPONSE, "too_large")


def test_an_oversized_request_is_refused_before_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gemini, "MAX_REQUEST_BYTES", 100)
    rec = Recorder()
    with pytest.raises(ProviderFailure) as caught:
        call(provider(rec), request("ب" * 200))
    assert caught.value.code == "request_too_large" and rec.requests == []


def test_provider_safety_blocks_are_a_refusal_not_an_outage() -> None:
    blocked = Recorder(
        httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    )
    expect(provider(blocked), FailureCategory.REFUSAL, "provider_refusal")
    finish = Recorder(
        httpx.Response(
            200,
            json={"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]},
        )
    )
    expect(provider(finish), FailureCategory.REFUSAL, "provider_refusal")
    assert len(blocked.requests) == 1  # and never retried with looser settings


def test_thought_parts_are_not_returned_as_the_answer() -> None:
    body = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "internal", "thought": True},
                        {"text": "final answer"},
                    ]
                }
            }
        ]
    }
    assert (
        call(provider(Recorder(httpx.Response(200, json=body)))).text == "final answer"
    )


# ----------------------------------------- credential isolation and config


def test_credential_is_opaque_and_never_appears_in_repr_str_or_audit() -> None:
    credential = ProviderCredential(FAKE_GEMINI_KEY)
    p = GeminiFreeProvider(
        credential,
        transport=httpx.MockTransport(Recorder(httpx.Response(500))),
        attestation=lambda: ATTESTED,
    )
    for shown in (
        repr(credential),
        str(credential),
        f"{credential}",
        repr(p),
        repr(vars(credential) if hasattr(credential, "__dict__") else ""),
    ):
        assert FAKE_GEMINI_KEY not in shown
    import copy
    import pickle

    with pytest.raises(TypeError):
        pickle.dumps(credential)
    with pytest.raises(TypeError):
        copy.deepcopy(credential)
    with pytest.raises(AttributeError):
        credential.x = 1
    for bad in ("", "  ", "has space", "x" * 600, "new\nline"):
        with pytest.raises(ValueError):
            ProviderCredential(bad)


def test_unconfigured_provider_is_not_configured_and_makes_no_request() -> None:
    rec = Recorder()
    p = GeminiFreeProvider(None, transport=httpx.MockTransport(rec))
    assert p.availability() is Availability.NOT_CONFIGURED
    with pytest.raises(ProviderFailure) as caught:
        call(p)
    assert caught.value.category is FailureCategory.UNAVAILABLE and rec.requests == []


def test_router_uses_gemini_as_a_free_tier_fallback_with_a_clean_audit() -> None:
    from sam.models.providers.fake import FakeProvider

    rec = Recorder()
    rig = make_rig(
        claude=FakeProvider(
            ProviderId.CLAUDE_SUBSCRIPTION,
            failure=ProviderFailure(FailureCategory.UNAVAILABLE),
        ),
        gemini=provider(rec),
    )
    result = rig.router.execute(request("What is 2+2? " + PERSIAN))
    assert (
        result.provider_id is ProviderId.GEMINI_FREE
        and result.fallback_used
        and result.text == "Paris."
    )
    assert len(rec.requests) == 1
    dumped = audit_json(rig)
    assert (
        FAKE_GEMINI_KEY not in dumped
        and PERSIAN not in dumped
        and "Paris." not in dumped
    )
    assert "free_tier" in dumped
    # A PRIVATE request never reaches this HTTP transport by default, even
    # once Claude's short cooldown has passed and Claude fails again.
    rec.requests.clear()
    rig.clock.advance(seconds=31)
    private = rig.router.execute(request(PRIVATE_TEXT, privacy=PrivacyClass.PRIVATE))
    assert private.provider_id is ProviderId.CLAUDE_SUBSCRIPTION
    assert private.status is FailureCategory.UNAVAILABLE and rec.requests == []
    # And while Claude is cooling down, PRIVATE is blocked rather than sent.
    blocked = rig.router.execute(request(PRIVATE_TEXT, privacy=PrivacyClass.PRIVATE))
    assert blocked.status is FailureCategory.POLICY_BLOCKED and rec.requests == []


# ------------------------------- billing-state attestation (Phase 13 review)


def test_unknown_attestation_makes_zero_network_calls() -> None:
    rec = Recorder()
    p = provider(rec, GeminiFreeAttestation.UNKNOWN)
    with pytest.raises(ProviderFailure) as caught:
        call(p)
    assert caught.value.category is FailureCategory.POLICY_BLOCKED
    assert caught.value.code == "free_tier_unattested" and rec.requests == []


def test_a_provider_built_without_an_attestation_source_refuses_by_default() -> None:
    rec = Recorder()
    p = GeminiFreeProvider(
        ProviderCredential(FAKE_GEMINI_KEY), transport=httpx.MockTransport(rec)
    )
    with pytest.raises(ProviderFailure) as caught:
        call(p)
    assert caught.value.code == "free_tier_unattested" and rec.requests == []


def test_attestation_is_read_live_so_a_reset_to_unknown_stops_requests() -> None:
    rec = Recorder()
    state = {"value": ATTESTED}
    p = GeminiFreeProvider(
        ProviderCredential(FAKE_GEMINI_KEY),
        transport=httpx.MockTransport(rec),
        attestation=lambda: state["value"],
    )
    assert call(p).text == "Paris." and len(rec.requests) == 1
    state["value"] = GeminiFreeAttestation.UNKNOWN
    with pytest.raises(ProviderFailure):
        call(p)
    assert len(rec.requests) == 1


def test_the_router_never_calls_gemini_while_unattested() -> None:
    rec = Recorder()
    from sam.models.policies import OwnerPreferences
    from tests.models_support import Holder

    rig = make_rig(
        claude_enabled=False,
        gemini=provider(rec, GeminiFreeAttestation.UNKNOWN),
        holder=Holder(OwnerPreferences()),
    )
    result = rig.router.execute(request())
    assert result.provider_id is None and rec.requests == []


def test_the_gemini_module_never_calls_billing_or_extra_oauth_endpoints() -> None:
    source = inspect.getsource(gemini)
    for banned in ("cloudbilling", "billingAccounts", "oauth2", "serviceusage"):
        assert banned not in source
