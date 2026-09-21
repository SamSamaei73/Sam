"""Gemini request contract and safe error diagnostics, driven only by
httpx.MockTransport: no live network, no real key.

Verifies (1) the exact outbound GenerateContent request for BOTH allowlisted
models, and (2) that a non-2xx response yields only allowlisted, sanitized
diagnostic fields: never the key, the prompt, the raw body or ``error.details``.
Routing-visible behavior (category and code) is unchanged.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from sam.agent.models import Message, MessageRole
from sam.models.credentials import ProviderCredential
from sam.models.errors import ProviderFailure
from sam.models.models import (
    FailureCategory,
    GeminiFreeAttestation,
    ModelRequest,
    ProviderId,
)
from sam.models.providers import gemini_errors
from sam.models.providers.gemini import GeminiFreeProvider
from sam.models.providers.gemini_errors import (
    MAX_MESSAGE_CHARS,
    format_detail,
    safe_error_detail,
    sanitize_message,
)
from sam.models.registry import GEMINI_EFFICIENT_MODEL, GEMINI_GENERAL_MODEL
from tests.models_support import FAKE_GEMINI_KEY, audit_json, make_rig, request

PROMPT = "Reply with exactly one word: pong"
CANARY = "CANARY-DETAILS-PAYLOAD-must-never-appear"
ATTESTED = GeminiFreeAttestation.OWNER_ATTESTED_UNBILLED
MODELS = (GEMINI_GENERAL_MODEL, GEMINI_EFFICIENT_MODEL)


class Recorder:
    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self._response is not None:
            return self._response
        return httpx.Response(200, json=ok_body("pong"))


def ok_body(text: str) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 1},
    }


def make_provider(handler: Recorder) -> GeminiFreeProvider:
    return GeminiFreeProvider(
        ProviderCredential(FAKE_GEMINI_KEY),
        transport=httpx.MockTransport(handler),
        attestation=lambda: ATTESTED,
    )


def smoke_request() -> ModelRequest:
    """Exactly what the owner-run smoke sends."""

    return ModelRequest(
        request_id="smoke1",
        max_output_tokens=32,
        messages=(Message(role=MessageRole.USER, content=PROMPT),),
    )


def fail(response: httpx.Response) -> tuple[ProviderFailure, Recorder]:
    rec = Recorder(response)
    with pytest.raises(ProviderFailure) as caught:
        make_provider(rec).complete(
            smoke_request(), model_id=GEMINI_EFFICIENT_MODEL, timeout_seconds=5
        )
    return caught.value, rec


def rpc(
    code: int, status: str, message: str, details: list[Any] | None = None
) -> httpx.Response:
    error: dict[str, Any] = {"code": code, "message": message, "status": status}
    if details is not None:
        error["details"] = details
    return httpx.Response(code, json={"error": error})


# ------------------------------------------- 1. the exact request, both models


@pytest.mark.parametrize("model", MODELS)
def test_the_smoke_request_matches_the_generate_content_contract(model: str) -> None:
    rec = Recorder()
    out = make_provider(rec).complete(
        smoke_request(), model_id=model, timeout_seconds=5
    )
    (sent,) = rec.requests
    assert out.text == "pong" and out.model_id == model
    assert sent.method == "POST"
    assert str(sent.url) == (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    )
    # The key is in the header only: never the query string, URL or body.
    assert sent.url.query == b"" and FAKE_GEMINI_KEY not in str(sent.url)
    assert sent.headers["x-goog-api-key"] == FAKE_GEMINI_KEY
    assert sent.headers["content-type"] == "application/json"
    assert "authorization" not in sent.headers
    assert FAKE_GEMINI_KEY not in sent.content.decode()
    # Structurally the documented body; the only optional field is the documented
    # generationConfig.maxOutputTokens.
    assert json.loads(sent.content) == {
        "contents": [{"role": "user", "parts": [{"text": PROMPT}]}],
        "generationConfig": {"maxOutputTokens": 32},
    }


@pytest.mark.parametrize("model", MODELS)
def test_no_unsupported_field_is_ever_sent(model: str) -> None:
    rec = Recorder()
    make_provider(rec).complete(smoke_request(), model_id=model, timeout_seconds=5)
    body = json.loads(rec.requests[0].content)
    assert set(body) == {"contents", "generationConfig"}
    assert set(body["generationConfig"]) == {"maxOutputTokens"}
    for forbidden in (
        "safetySettings",
        "tools",
        "toolConfig",
        "cachedContent",
        "responseModalities",
        "speechConfig",
        "response_format",
        "interaction",
        "model",  # the model is in the URL only
    ):
        assert forbidden not in body and forbidden not in body["generationConfig"]


def test_an_arbitrary_model_id_is_refused_before_any_request() -> None:
    rec = Recorder()
    with pytest.raises(ProviderFailure) as caught:
        make_provider(rec).complete(
            smoke_request(), model_id="gemini-3.8-pro", timeout_seconds=5
        )
    assert caught.value.code == "model_not_allowed" and rec.requests == []


# ---------------------------------------------- 2. safe extraction, each shape


def test_invalid_argument_is_extracted_and_the_category_is_unchanged() -> None:
    failure, rec = fail(
        rpc(400, "INVALID_ARGUMENT", "Invalid value at generation_config")
    )
    assert (
        failure.category is FailureCategory.UNAVAILABLE and failure.code == "http_400"
    )
    assert len(rec.requests) == 1  # no retry
    assert failure.detail == {
        "http_status": 400,
        "error_code": 400,
        "error_status": "INVALID_ARGUMENT",
        "message": "Invalid value at generation_config",
        "shape": "google_rpc",
    }


def test_failed_precondition_is_extracted_without_any_billing_action() -> None:
    failure, rec = fail(
        rpc(
            400,
            "FAILED_PRECONDITION",
            "Free tier is not available in your country.",
        )
    )
    assert (
        failure.category is FailureCategory.UNAVAILABLE and failure.code == "http_400"
    )
    assert failure.detail is not None
    assert failure.detail["error_status"] == "FAILED_PRECONDITION"
    assert len(rec.requests) == 1  # nothing was enabled, nothing retried


def test_api_key_invalid_reason_is_extracted_from_error_info() -> None:
    failure, _ = fail(
        rpc(
            400,
            "INVALID_ARGUMENT",
            "API key not valid. Please pass a valid API key.",
            [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "API_KEY_INVALID",
                    "domain": "googleapis.com",
                    "metadata": {"service": "generativelanguage.googleapis.com"},
                }
            ],
        )
    )
    assert failure.detail is not None
    assert failure.detail["reason"] == "API_KEY_INVALID"
    assert failure.detail["error_status"] == "INVALID_ARGUMENT"


def test_permission_denied_and_not_found_keep_their_categories() -> None:
    denied, _ = fail(rpc(403, "PERMISSION_DENIED", "Permission denied"))
    assert (
        denied.category is FailureCategory.AUTH_FAILURE
        and denied.code == "not_authorized"
    )
    assert (
        denied.detail is not None
        and denied.detail["error_status"] == "PERMISSION_DENIED"
    )
    missing, _ = fail(rpc(404, "NOT_FOUND", "model is not found"))
    assert (
        missing.category is FailureCategory.UNAVAILABLE and missing.code == "http_404"
    )
    assert missing.detail is not None and missing.detail["error_status"] == "NOT_FOUND"
    quota, _ = fail(rpc(429, "RESOURCE_EXHAUSTED", "Quota exceeded"))
    assert quota.category is FailureCategory.RATE_LIMIT
    assert quota.code == "free_quota_exhausted"


def test_the_current_simple_error_reference_shape_is_understood() -> None:
    failure, _ = fail(
        httpx.Response(
            400, json={"error": {"code": "failed_precondition", "message": "billing"}}
        )
    )
    assert failure.detail == {
        "http_status": 400,
        "error_code": "FAILED_PRECONDITION",
        "error_status": "FAILED_PRECONDITION",
        "message": "billing",
        "shape": "simple",
    }


@pytest.mark.parametrize(
    ("response", "shape"),
    [
        (httpx.Response(400, content=b"{not json"), "not_json"),
        (httpx.Response(400, content=b""), "empty"),
        (httpx.Response(400, json=["error"]), "unrecognized"),
        (httpx.Response(400, json={"error": "a string"}), "unrecognized"),
        (httpx.Response(400, json={"unexpected": {"shape": 1}}), "unrecognized"),
        (httpx.Response(400, content=b"<html>Bad Request</html>"), "not_json"),
    ],
)
def test_malformed_or_unexpected_error_bodies_yield_only_the_status(
    response: httpx.Response, shape: str
) -> None:
    failure, rec = fail(response)
    assert (
        failure.category is FailureCategory.UNAVAILABLE and failure.code == "http_400"
    )
    assert failure.detail == {"http_status": 400, "shape": shape}
    assert len(rec.requests) == 1


def test_a_success_response_carries_no_error_detail() -> None:
    rec = Recorder()
    out = make_provider(rec).complete(
        smoke_request(), model_id=GEMINI_GENERAL_MODEL, timeout_seconds=5
    )
    assert out.text == "pong" and len(rec.requests) == 1


def test_an_unknown_status_and_an_unknown_reason_are_never_copied() -> None:
    failure, _ = fail(
        rpc(
            400,
            "SOMETHING_NEW_AND_SECRET",
            "m",
            [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": CANARY}],
        )
    )
    assert failure.detail is not None
    assert (
        failure.detail["error_status"] == "UNKNOWN" and "reason" not in failure.detail
    )
    assert CANARY not in json.dumps(dict(failure.detail))
    assert "SOMETHING_NEW_AND_SECRET" not in json.dumps(dict(failure.detail))


def test_encoded_error_bodies_are_not_read() -> None:
    # A lazy stream: if the adapter tried to read/decode it, the test would fail.
    response = httpx.Response(
        400,
        headers={"content-encoding": "gzip"},
        stream=httpx.ByteStream(b"\x1f\x8b garbage"),
    )
    failure, _ = fail(response)
    assert failure.detail == {"http_status": 400, "shape": "encoded"}


def test_an_oversized_error_body_is_bounded() -> None:
    failure, _ = fail(httpx.Response(400, content=b"x" * 5_000_000))
    assert failure.detail == {"http_status": 400, "shape": "not_json"}


# ------------------------------------------------ 3. nothing sensitive escapes


def test_key_prompt_details_and_raw_body_never_appear_in_any_diagnostic() -> None:
    hostile = rpc(
        400,
        "INVALID_ARGUMENT",
        f"bad request for {PROMPT!r} using key {FAKE_GEMINI_KEY} in "
        "projects/123456789012/locations/x at https://example.test/secret?token=abc "
        "token " + "AI" + "zaSy" + "TOKEN" * 5 + "123456",
        [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "API_KEY_INVALID",
                "metadata": {"consumer": "projects/123456789012", "x": CANARY},
            },
            {"@type": "type.googleapis.com/google.rpc.DebugInfo", "detail": CANARY},
        ],
    )
    failure, _ = fail(hostile)
    assert failure.detail is not None
    surfaces = [
        json.dumps(dict(failure.detail)),
        format_detail(dict(failure.detail)),
        str(failure),
        repr(failure),
        f"{failure.category} {failure.code}",
    ]
    for text in surfaces:
        for secret in (
            FAKE_GEMINI_KEY,
            PROMPT,
            CANARY,
            "123456789012",
            "example.test",
            "TOKENTOKENTOKEN",
        ):
            assert secret not in text, (secret, text)
    assert failure.detail["reason"] == "API_KEY_INVALID"


def test_the_router_never_records_the_detail_in_its_audit() -> None:
    body = rpc(400, "INVALID_ARGUMENT", f"echo {PROMPT} {FAKE_GEMINI_KEY} {CANARY}")
    rec = Recorder(body)
    rig = make_rig(gemini=make_provider(rec))
    result = rig.router.execute(
        request(
            "What is 2+2?",
        )
    )
    assert result.provider_id in (
        ProviderId.GEMINI_FREE,
        ProviderId.CLAUDE_SUBSCRIPTION,
    )
    dumped = audit_json(rig) + repr(result)
    for secret in (FAKE_GEMINI_KEY, CANARY, "INVALID_ARGUMENT", "echo"):
        assert secret not in dumped


def test_sanitize_message_removes_secrets_tokens_urls_ids_and_truncates() -> None:
    text = (
        "key SECRETVALUE1234 at https://x.test/a?b=c in projects/999999/x "
        "AbCdEfGhIjKlMnOpQrStUv id 12345678 " + "long " * 200
    )
    cleaned = sanitize_message(text, redact=["SECRETVALUE1234"])
    for gone in (
        "SECRETVALUE1234",
        "x.test",
        "999999",
        "AbCdEfGhIjKlMnOpQrStUv",
        "12345678",
    ):
        assert gone not in cleaned
    assert len(cleaned) <= MAX_MESSAGE_CHARS
    assert "\n" not in sanitize_message("a\nb\x00c\td")


def test_the_diagnostic_module_uses_only_allowlists() -> None:
    assert "API_KEY_INVALID" in gemini_errors.ALLOWED_REASONS
    assert "INVALID_ARGUMENT" in gemini_errors.ALLOWED_STATUS
    assert "FAILED_PRECONDITION" in gemini_errors.ALLOWED_STATUS
    assert safe_error_detail(500, None) == {"http_status": 500, "shape": "empty"}


def test_error_detail_does_not_change_routing_or_billing_behavior() -> None:
    """A 400 is still an ordinary unavailable failure: no retry, no billing, no
    other provider is contacted by this adapter."""

    failure, rec = fail(rpc(400, "FAILED_PRECONDITION", "prerequisite"))
    assert failure.category is FailureCategory.UNAVAILABLE
    assert len(rec.requests) == 1
    assert {r.url.host for r in rec.requests} == {"generativelanguage.googleapis.com"}
