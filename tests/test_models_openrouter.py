"""OpenRouter transport tests use synthetic keys and never call the network."""

from __future__ import annotations

import json
import traceback
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sam.agent.models import Message, MessageRole
from sam.core.config import Settings
from sam.models.credentials import ProviderCredential
from sam.models.errors import ProviderFailure
from sam.models.factory import build_model_router
from sam.models.models import (
    FALLBACK_CATEGORIES,
    MAX_RESULT_CHARS,
    Availability,
    FailureCategory,
    ModelRequest,
    ProviderId,
    ProviderOutput,
    ReasonCode,
)
from sam.models.openrouter_config import DEFAULT_OPENROUTER_MODEL
from sam.models.providers import openrouter
from sam.models.providers.openrouter import OpenRouterProvider, build_client

MODEL = DEFAULT_OPENROUTER_MODEL
KEY = "sk-or-v1-synthetic-test-key-never-valid"
PRIVATE = "private owner conversation"
PERSIAN = "سلام سام، جواب فارسی می‌خواهم."


def request(text: str = "Hello") -> ModelRequest:
    return ModelRequest(
        request_id="test_openrouter",
        messages=(Message(role=MessageRole.USER, content=text),),
    )


def ok_body(text: str = "Hello.", model: str = MODEL) -> dict[str, Any]:
    return {
        "id": "gen-synthetic",
        "model": model,
        "choices": [
            {
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }


class Recorder:
    def __init__(
        self,
        response: httpx.Response
        | Callable[[httpx.Request], httpx.Response]
        | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.response = response

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if callable(self.response):
            return self.response(req)
        return (
            self.response
            if self.response is not None
            else httpx.Response(200, json=ok_body())
        )


def provider(rec: Recorder, *, model: str = MODEL) -> OpenRouterProvider:
    return OpenRouterProvider(
        ProviderCredential(KEY), model_id=model, transport=httpx.MockTransport(rec)
    )


def call(
    p: OpenRouterProvider, req: ModelRequest | None = None, *, model: str = MODEL
) -> ProviderOutput:
    return p.complete(req or request(), model_id=model, timeout_seconds=5)


def failure(
    rec: Recorder, category: FailureCategory, code: str | None = None
) -> ProviderFailure:
    with pytest.raises(ProviderFailure) as caught:
        call(provider(rec), request(PRIVATE))
    assert caught.value.category is category
    if code is not None:
        assert caught.value.code == code
    assert len(rec.requests) == 1
    assert KEY not in str(caught.value)
    assert PRIVATE not in str(caught.value)
    assert caught.value.detail is None
    return caught.value


def test_request_uses_documented_text_schema_and_one_explicit_model() -> None:
    rec = Recorder()
    req = ModelRequest(
        request_id="history",
        messages=(
            Message(role=MessageRole.SYSTEM, content="Answer in Persian."),
            Message(role=MessageRole.USER, content=PERSIAN),
            Message(role=MessageRole.ASSISTANT, content="پاسخ قبلی"),
            Message(role=MessageRole.USER, content="Continue."),
        ),
        max_output_tokens=256,
    )
    out = call(provider(rec), req)
    (sent,) = rec.requests
    assert sent.method == "POST"
    assert str(sent.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert sent.url.query == b""
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert sent.headers["content-type"] == "application/json"
    assert sent.headers["accept-encoding"] == "identity"
    assert KEY not in str(sent.url) and KEY.encode() not in sent.content
    assert PERSIAN.encode() in sent.content
    assert json.loads(sent.content) == {
        "model": MODEL,
        "messages": [
            {"role": m.role.value, "content": m.content} for m in req.messages
        ],
        "max_tokens": 256,
        "stream": False,
        "provider": {"allow_fallbacks": False, "data_collection": "deny"},
    }
    assert out.model_id == MODEL and out.text == "Hello."
    assert out.usage is not None
    assert out.usage.input_tokens == 12 and out.usage.output_tokens == 3
    assert provider(rec).provider_id is ProviderId.OPENROUTER


def test_bare_openrouter_key_is_blocked_by_router_before_any_provider_call() -> None:
    rec = Recorder()
    router, _ = build_model_router(
        Settings(openrouter_api_key=SecretStr(KEY)),
        openrouter_transport=httpx.MockTransport(rec),
    )
    result = router.execute(request("sk-or-v1-" + "a" * 64))
    assert result.status is FailureCategory.POLICY_BLOCKED
    assert ReasonCode.SECRET_BLOCKED in result.route_reasons
    assert not rec.requests


def test_upstream_canonical_model_does_not_change_requested_route_identity() -> None:
    rec = Recorder(
        httpx.Response(200, json=ok_body(model="openai/gpt-4.1-mini-2025-04-14"))
    )
    result = call(provider(rec))
    assert result.model_id == MODEL
    assert json.loads(rec.requests[0].content)["model"] == MODEL


def test_another_explicit_configured_model_is_supported() -> None:
    model = "anthropic/claude-sonnet-4.6"
    rec = Recorder(httpx.Response(200, json=ok_body(model=model)))
    result = call(provider(rec, model=model), model=model)
    assert result.model_id == model
    assert json.loads(rec.requests[0].content)["model"] == model


def test_client_disables_environment_proxies_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "https://untrusted.invalid")
    monkeypatch.setenv("SSL_CERT_FILE", "/untrusted/invalid.pem")
    with build_client(httpx.MockTransport(Recorder()), 5) as client:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client.timeout == httpx.Timeout(5)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_rejected_without_following_or_retry(status: int) -> None:
    rec = Recorder(
        httpx.Response(status, headers={"location": "https://untrusted.invalid"})
    )
    failure(rec, FailureCategory.UNAVAILABLE, "redirect_refused")


@pytest.mark.parametrize("model", ["google/gemini-test", "", "https://evil.invalid/x"])
def test_request_cannot_override_configured_model(model: str) -> None:
    rec = Recorder()
    with pytest.raises(ProviderFailure) as caught:
        call(provider(rec), model=model)
    assert caught.value.category is FailureCategory.POLICY_BLOCKED
    assert caught.value.code == "model_not_allowed" and not rec.requests


@pytest.mark.parametrize(
    "model",
    [
        "",
        "gpt-4.1-mini",
        "openrouter/auto",
        "openrouter/free",
        "vendor/name?key=x",
        "@preset",
    ],
)
def test_constructor_requires_a_concrete_trusted_model(model: str) -> None:
    rec = Recorder()
    with pytest.raises(ValueError):
        provider(rec, model=model)
    assert not rec.requests


@pytest.mark.parametrize("seconds", [0, -1, 301, float("inf"), float("nan")])
def test_invalid_timeout_never_sends(seconds: float) -> None:
    rec = Recorder()
    with pytest.raises(ProviderFailure) as caught:
        provider(rec).complete(request(), model_id=MODEL, timeout_seconds=seconds)
    assert caught.value.code == "bad_timeout" and not rec.requests


def test_missing_key_makes_no_network_calls() -> None:
    rec = Recorder()
    p = OpenRouterProvider(None, model_id=MODEL, transport=httpx.MockTransport(rec))
    assert p.availability() is Availability.NOT_CONFIGURED
    assert provider(rec).availability() is Availability.AVAILABLE
    with pytest.raises(ProviderFailure) as caught:
        call(p)
    assert caught.value.code == "not_configured" and not rec.requests
    assert KEY not in repr(p) and KEY not in repr(provider(rec))


@pytest.mark.parametrize(
    ("status", "category", "code"),
    [
        (400, FailureCategory.POLICY_BLOCKED, "request_rejected"),
        (401, FailureCategory.AUTH_FAILURE, "not_authorized"),
        (402, FailureCategory.COST_BLOCKED, "credit_limit"),
        (403, FailureCategory.REFUSAL, "provider_refusal"),
        (404, FailureCategory.UNAVAILABLE, "http_404"),
        (408, FailureCategory.TIMEOUT, "timeout"),
        (429, FailureCategory.RATE_LIMIT, "rate_limit"),
        (500, FailureCategory.UNAVAILABLE, "http_500"),
        (502, FailureCategory.UNAVAILABLE, "http_502"),
        (503, FailureCategory.UNAVAILABLE, "http_503"),
        (504, FailureCategory.TIMEOUT, "timeout"),
    ],
)
def test_http_failures_are_typed_redacted_and_never_retried(
    status: int, category: FailureCategory, code: str
) -> None:
    rec = Recorder(httpx.Response(status, content=f"echo {KEY} {PRIVATE}"))
    failure(rec, category, code)


@pytest.mark.parametrize("location", ["top", "choice"])
@pytest.mark.parametrize(
    ("code", "error_type", "category"),
    [
        (401, "authentication", FailureCategory.AUTH_FAILURE),
        (402, "payment_required", FailureCategory.COST_BLOCKED),
        (403, "refusal", FailureCategory.REFUSAL),
        (403, "content_policy_violation", FailureCategory.REFUSAL),
        (429, "rate_limit_exceeded", FailureCategory.RATE_LIMIT),
        (502, "provider_unavailable", FailureCategory.UNAVAILABLE),
        (504, "timeout", FailureCategory.TIMEOUT),
    ],
)
def test_errors_inside_success_responses_never_return_partial_content(
    location: str, code: int, error_type: str, category: FailureCategory
) -> None:
    body = ok_body("partial content that must not escape")
    target = body if location == "top" else body["choices"][0]
    target["error"] = {
        "code": code,
        "message": f"echo {KEY} {PRIVATE}",
        "metadata": {"error_type": error_type, "raw": PRIVATE},
    }
    failure(Recorder(httpx.Response(200, json=body)), category)


def test_http_200_with_only_an_error_is_handled() -> None:
    failure(
        Recorder(httpx.Response(200, json={"error": {"code": 429}})),
        FailureCategory.RATE_LIMIT,
        "rate_limit",
    )


def test_budget_guardrail_is_cost_blocked_and_not_eligible_for_failover() -> None:
    rec = Recorder(
        httpx.Response(
            403, json={"error": {"metadata": {"limit_source": "openrouter_key_limit"}}}
        )
    )
    caught = failure(rec, FailureCategory.COST_BLOCKED, "credit_limit")
    assert caught.category not in FALLBACK_CATEGORIES


@pytest.mark.parametrize("status", [200, 400, 500])
def test_typed_policy_refusal_takes_precedence_over_ordinary_http_errors(
    status: int,
) -> None:
    body = {
        "error": {"code": 500, "metadata": {"error_type": "content_policy_violation"}}
    }
    caught = failure(
        Recorder(httpx.Response(status, json=body)), FailureCategory.REFUSAL
    )
    assert caught.category not in FALLBACK_CATEGORIES


@pytest.mark.parametrize("kind", ["content_filter", "refusal", "refusal_with_text"])
def test_successful_refusals_are_preserved_as_refusals(kind: str) -> None:
    body = ok_body()
    choice = body["choices"][0]
    if kind == "content_filter":
        choice["finish_reason"] = "content_filter"
    else:
        choice["message"]["refusal"] = f"refused {PRIVATE}"
        if kind == "refusal":
            choice["message"]["content"] = None
    caught = failure(Recorder(httpx.Response(200, json=body)), FailureCategory.REFUSAL)
    assert caught.category not in FALLBACK_CATEGORIES


def test_timeout_is_redacted_and_not_retried() -> None:
    def slow(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"echo {KEY} {PRIVATE}", request=req)

    caught = failure(Recorder(slow), FailureCategory.TIMEOUT, "timeout")
    rendered = "".join(traceback.format_exception(caught))
    assert KEY not in rendered and PRIVATE not in rendered


def test_transport_exception_cannot_expose_credentials() -> None:
    def broken(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"echo {KEY} {PRIVATE}", request=req)

    caught = failure(Recorder(broken), FailureCategory.UNAVAILABLE, "request_failed")
    rendered = "".join(traceback.format_exception(caught))
    assert KEY not in rendered and PRIVATE not in rendered
    assert caught.__cause__ is None


def test_total_deadline_is_checked_after_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        "sam.models.providers.openrouter.time.monotonic", lambda: clock[0]
    )

    def late(req: httpx.Request) -> httpx.Response:
        clock[0] = 6.0
        return httpx.Response(200, json=ok_body())

    failure(Recorder(late), FailureCategory.TIMEOUT, "timeout")


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"choices": []},
        {"choices": ["invalid"]},
        {"choices": [{"message": "invalid"}]},
        {"choices": [{"message": {"role": "user", "content": "text"}}]},
    ],
)
def test_malformed_response_shapes_are_rejected(body: object) -> None:
    failure(Recorder(httpx.Response(200, json=body)), FailureCategory.INVALID_RESPONSE)


@pytest.mark.parametrize("content", [None, [], {}, "", "   ", 3])
def test_nontext_or_empty_content_is_rejected(content: object) -> None:
    body = ok_body()
    body["choices"][0]["message"]["content"] = content
    failure(Recorder(httpx.Response(200, json=body)), FailureCategory.INVALID_RESPONSE)


@pytest.mark.parametrize("body", [b"<html>error</html>", b"\xff\xfe", b"[" * 2000])
def test_invalid_json_and_deep_nesting_are_normalized(body: bytes) -> None:
    failure(
        Recorder(httpx.Response(200, content=body)),
        FailureCategory.INVALID_RESPONSE,
        "not_json",
    )


def test_json_escaped_lone_surrogate_cannot_escape_as_a_successful_answer() -> None:
    body = json.dumps(ok_body("Invalid text: \ud800")).encode("ascii")
    failure(
        Recorder(httpx.Response(200, content=body)),
        FailureCategory.INVALID_RESPONSE,
        "invalid_response_text",
    )


@pytest.mark.parametrize(
    "model", [None, 3, "", "x" * 200, "https://evil.invalid/x", "bad\nmodel"]
)
def test_malformed_upstream_model_ids_are_rejected(model: object) -> None:
    body = ok_body()
    body["model"] = model
    failure(
        Recorder(httpx.Response(200, json=body)),
        FailureCategory.INVALID_RESPONSE,
        "bad_model",
    )


@pytest.mark.parametrize("header", ["bad", "-1", "1000001"])
def test_response_length_bounds(header: str) -> None:
    rec = Recorder(httpx.Response(200, headers={"content-length": header}))
    failure(rec, FailureCategory.INVALID_RESPONSE)


class ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


def test_streamed_body_without_content_length_is_bounded_and_closed() -> None:
    stream = ChunkStream([b"x" * 600_000, b"x" * 600_000, b"never read"])
    failure(
        Recorder(httpx.Response(200, stream=stream)),
        FailureCategory.INVALID_RESPONSE,
        "too_large",
    )
    assert stream.read_count == 2 and stream.closed


def test_compressed_response_is_rejected_before_decoding() -> None:
    stream = ChunkStream([b"not compressed"])
    rec = Recorder(
        httpx.Response(200, stream=stream, headers={"content-encoding": "gzip"})
    )
    failure(rec, FailureCategory.INVALID_RESPONSE, "content_encoding")
    assert stream.read_count == 0 and stream.closed


def test_error_body_is_bounded_and_never_leaked() -> None:
    stream = ChunkStream([b"x" * (openrouter.MAX_ERROR_BODY_BYTES + 1), KEY.encode()])
    failure(Recorder(httpx.Response(403, stream=stream)), FailureCategory.REFUSAL)
    assert stream.read_count == 1 and stream.closed


def test_output_character_limit_is_enforced() -> None:
    rec = Recorder(httpx.Response(200, json=ok_body("x" * (MAX_RESULT_CHARS + 1))))
    failure(rec, FailureCategory.INVALID_RESPONSE, "text_too_large")


def test_request_byte_limit_is_checked_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openrouter, "MAX_REQUEST_BYTES", 100)
    rec = Recorder()
    with pytest.raises(ProviderFailure) as caught:
        call(provider(rec), request(PERSIAN))
    assert caught.value.code == "request_too_large" and not rec.requests


def test_unpaired_tool_history_is_rejected_before_network() -> None:
    rec = Recorder()
    req = ModelRequest(
        request_id="tool_history",
        messages=(Message(role=MessageRole.TOOL, content="malicious tool result"),),
    )
    with pytest.raises(ProviderFailure) as caught:
        call(provider(rec), req)
    assert caught.value.code == "unsupported_role" and not rec.requests


@pytest.mark.parametrize("kind", ["tool_calls", "function_call", "finish_reason"])
def test_unsolicited_tool_requests_are_never_executed_or_returned_as_answers(
    kind: str,
) -> None:
    body = ok_body()
    choice = body["choices"][0]
    if kind == "finish_reason":
        choice["finish_reason"] = "tool_calls"
    else:
        choice["message"][kind] = [
            {"function": {"name": "shell", "arguments": "malicious"}}
        ]
    failure(
        Recorder(httpx.Response(200, json=body)),
        FailureCategory.INVALID_RESPONSE,
        "unexpected_tool_call",
    )


def test_reasoning_and_other_unrequested_fields_are_not_exposed() -> None:
    body = ok_body("visible answer")
    body["choices"][0]["message"]["reasoning"] = PRIVATE
    result = call(provider(Recorder(httpx.Response(200, json=body))))
    assert result.text == "visible answer" and PRIVATE not in result.model_dump_json()


def test_response_echoing_key_is_rejected_even_when_json_escaped() -> None:
    body = json.dumps(ok_body(f"echo {KEY}"))
    body = body.replace("sk-or", "\\u0073k-or")
    failure(
        Recorder(httpx.Response(200, content=body)),
        FailureCategory.INVALID_RESPONSE,
        "credential_echo",
    )


@pytest.mark.parametrize("value", [True, "4", -1, 10_000_001, None, []])
def test_malformed_usage_cannot_inject_unbounded_or_untrusted_counts(
    value: object,
) -> None:
    body = ok_body()
    body["usage"] = {"prompt_tokens": value, "completion_tokens": 3}
    result = call(provider(Recorder(httpx.Response(200, json=body))))
    assert result.usage is not None
    assert result.usage.input_tokens is None and result.usage.output_tokens == 3
