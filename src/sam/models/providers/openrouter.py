"""OpenRouter text completions: one bounded request to the configured model.

The endpoint and routing preferences are application constants; messages cannot
select another model, enable tools, or change data handling. An API key permits
paid usage and is revealed only for the Authorization header. OpenRouter's
upstream model may have a canonical/versioned name: ``ProviderOutput.model_id``
identifies the requested Sam route, not an attestation of that upstream name.

Protocol references (checked 2026-10-11):
https://openrouter.ai/docs/api_reference/overview
https://openrouter.ai/docs/api_reference/errors-and-debugging
https://openrouter.ai/docs/guides/routing/provider-selection
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

import httpx

from sam.agent.models import MessageRole
from sam.models.credentials import ProviderCredential
from sam.models.errors import ProviderFailure
from sam.models.models import (
    MAX_RESULT_CHARS,
    Availability,
    FailureCategory,
    ModelRequest,
    ProviderId,
    ProviderOutput,
    UsageMetadata,
)
from sam.models.openrouter_config import validate_openrouter_model_id

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MAX_REQUEST_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 1_000_000
MAX_ERROR_BODY_BYTES = 16_384
_REFUSAL_TYPES = frozenset({"refusal", "content_policy_violation"})
_BUDGET_SOURCES = frozenset(
    {
        "openrouter_guardrail_budget",
        "openrouter_workspace_budget",
        "openrouter_key_limit",
        "openrouter_in_flight_budget",
    }
)


def _valid_model_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        validate_openrouter_model_id(value)
    except ValueError:
        return False
    return True


def build_client(
    transport: httpx.BaseTransport | None, timeout_seconds: float
) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(timeout_seconds),
        trust_env=False,
        follow_redirects=False,
        http2=False,
    )


def build_body(request: ModelRequest, model_id: str) -> dict[str, Any]:
    if any(m.role is MessageRole.TOOL for m in request.messages):
        # The text-only contract has no tool_call_id to send a valid tool turn.
        raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "unsupported_role")
    if not any(m.role is MessageRole.USER for m in request.messages):
        raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "empty_request")
    return {
        "model": model_id,
        "messages": [
            {"role": m.role.value, "content": m.content} for m in request.messages
        ],
        "max_tokens": request.max_output_tokens,
        "stream": False,
        # OpenRouter otherwise retries upstream providers by default. Only Sam
        # may decide whether a different provider is eligible after a failure.
        "provider": {"allow_fallbacks": False, "data_collection": "deny"},
    }


class OpenRouterProvider:
    provider_id = ProviderId.OPENROUTER

    def __init__(
        self,
        credential: ProviderCredential | None,
        *,
        model_id: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        validate_openrouter_model_id(model_id)
        self._credential = credential
        self._model_id = model_id
        self._transport = transport

    def __repr__(self) -> str:
        return f"OpenRouterProvider(configured={self._credential is not None})"

    def availability(self) -> Availability:
        return (
            Availability.AVAILABLE
            if self._credential is not None
            else Availability.NOT_CONFIGURED
        )

    def complete(
        self, request: ModelRequest, *, model_id: str, timeout_seconds: float
    ) -> ProviderOutput:
        if self._credential is None:
            raise ProviderFailure(FailureCategory.UNAVAILABLE, "not_configured")
        if model_id != self._model_id:
            raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "model_not_allowed")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300:
            raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "bad_timeout")
        try:
            payload = json.dumps(
                build_body(request, self._model_id), ensure_ascii=False
            ).encode("utf-8")
        except UnicodeError:
            raise ProviderFailure(
                FailureCategory.POLICY_BLOCKED, "invalid_request_text"
            ) from None
        if len(payload) > MAX_REQUEST_BYTES:
            raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "request_too_large")
        headers = {
            "Authorization": f"Bearer {self._credential.reveal()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "sam-models/0.1",
            "X-OpenRouter-Title": "Sam",
        }
        deadline = time.monotonic() + timeout_seconds
        try:
            with build_client(self._transport, timeout_seconds) as client:
                with client.stream(
                    "POST", OPENROUTER_ENDPOINT, headers=headers, content=payload
                ) as response:
                    body = _read_response(response, deadline)
        except ProviderFailure:
            raise
        except httpx.TimeoutException:
            raise ProviderFailure(FailureCategory.TIMEOUT, "timeout") from None
        except Exception:
            # HTTP/protocol exceptions may contain credentials or request text.
            raise ProviderFailure(
                FailureCategory.UNAVAILABLE, "request_failed"
            ) from None
        output = _parse(body, self._model_id)
        if self._credential.reveal() in output.text:
            raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "credential_echo")
        return output


def _read_response(response: httpx.Response, deadline: float) -> bytes:
    status = response.status_code
    if 300 <= status < 400:
        raise ProviderFailure(FailureCategory.UNAVAILABLE, "redirect_refused")
    if status != 200:
        # An error body can contain an upstream refusal even under a different
        # HTTP status. Inspect only its bounded, typed fields; retain no text.
        error: object = None
        try:
            body = _read_bounded(response, deadline, MAX_ERROR_BODY_BYTES)
            document = json.loads(body)
            if isinstance(document, dict):
                error = document.get("error")
        except (ProviderFailure, ValueError, RecursionError, httpx.HTTPError):
            pass
        raise _error_failure(error, status)
    return _read_bounded(response, deadline, MAX_RESPONSE_BYTES)


def _read_bounded(response: httpx.Response, deadline: float, limit: int) -> bytes:
    if time.monotonic() > deadline:
        raise ProviderFailure(FailureCategory.TIMEOUT, "timeout")
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "content_encoding")
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise ProviderFailure(
                FailureCategory.INVALID_RESPONSE, "bad_length"
            ) from None
        if length < 0:
            raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "bad_length")
        if length > limit:
            raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "too_large")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > limit:
            raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "too_large")
        chunks.append(chunk)
        if time.monotonic() > deadline:
            raise ProviderFailure(FailureCategory.TIMEOUT, "timeout")
    if time.monotonic() > deadline:
        raise ProviderFailure(FailureCategory.TIMEOUT, "timeout")
    return b"".join(chunks)


def _error_failure(error: object, status: int = 200) -> ProviderFailure:
    metadata: dict[str, Any] = {}
    if isinstance(error, dict):
        if isinstance(error.get("metadata"), dict):
            metadata = error["metadata"]
        embedded_code = error.get("code")
        if status == 200 and type(embedded_code) is int:
            status = embedded_code
    error_type = metadata.get("error_type")
    if isinstance(error_type, str) and error_type in _REFUSAL_TYPES:
        return ProviderFailure(FailureCategory.REFUSAL, "provider_refusal")
    budget = metadata.get("limit_source")
    if (
        isinstance(budget, str)
        and budget in _BUDGET_SOURCES
        or error_type == "payment_required"
        or status == 402
    ):
        return ProviderFailure(FailureCategory.COST_BLOCKED, "credit_limit")
    # 403 can mean a guardrail or moderation block. It must never be treated as
    # an availability failure that permits another provider to answer instead.
    if status == 403 or error_type == "permission_denied":
        return ProviderFailure(FailureCategory.REFUSAL, "provider_refusal")
    if error_type == "authentication" or status == 401:
        return ProviderFailure(FailureCategory.AUTH_FAILURE, "not_authorized")
    if error_type == "rate_limit_exceeded" or status == 429:
        return ProviderFailure(FailureCategory.RATE_LIMIT, "rate_limit")
    if error_type == "timeout" or status in (408, 504):
        return ProviderFailure(FailureCategory.TIMEOUT, "timeout")
    if status in (400, 413, 422):
        return ProviderFailure(FailureCategory.POLICY_BLOCKED, "request_rejected")
    if 400 <= status <= 599:
        return ProviderFailure(FailureCategory.UNAVAILABLE, f"http_{status}")
    return ProviderFailure(FailureCategory.UNAVAILABLE, "provider_error")


def _parse(body: bytes, model_id: str) -> ProviderOutput:
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "not_json") from None
    if not isinstance(document, dict):
        raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "not_object")
    if "error" in document:
        raise _error_failure(document["error"])
    choices = document.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "bad_choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "bad_choice")
    message = choice.get("message")
    if choice.get("finish_reason") == "content_filter" or (
        isinstance(message, dict) and message.get("refusal")
    ):
        raise ProviderFailure(FailureCategory.REFUSAL, "provider_refusal")
    if "error" in choice or choice.get("finish_reason") == "error":
        raise _error_failure(choice.get("error"))
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "bad_message")
    if (
        message.get("tool_calls")
        or message.get("function_call")
        or choice.get("finish_reason") == "tool_calls"
    ):
        raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "unexpected_tool_call")
    if not _valid_model_id(document.get("model")):
        raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "bad_model")
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "no_text")
    text = text.strip()
    if len(text) > MAX_RESULT_CHARS:
        raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "text_too_large")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # JSON permits escaped lone surrogates, but the desktop wire format
        # cannot encode them. Never return malformed provider text as success.
        raise ProviderFailure(
            FailureCategory.INVALID_RESPONSE, "invalid_response_text"
        ) from None
    # The remote ID can be a canonical alias. It cannot change the requested
    # model identity in Sam's registry, nor is it used to enable any next call.
    return ProviderOutput(
        text=text, model_id=model_id, usage=_usage(document.get("usage"))
    )


def _usage(value: object) -> UsageMetadata | None:
    if not isinstance(value, dict):
        return None

    def count(key: str) -> int | None:
        number = value.get(key)
        return number if type(number) is int and 0 <= number <= 10_000_000 else None

    return UsageMetadata(
        input_tokens=count("prompt_tokens"), output_tokens=count("completion_tokens")
    )


__all__ = ["OPENROUTER_ENDPOINT", "OpenRouterProvider", "build_body", "build_client"]
