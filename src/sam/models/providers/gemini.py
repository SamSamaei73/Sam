"""Gemini Free Tier text provider (non-streaming REST, one call, no retry).

Official API, verified 2026-09-20 (docs/model-routing.md):
``POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent``
with an ``x-goog-api-key`` header. ``generateContent`` is documented as fully
supported (the newer Interactions API is recommended for new projects, but its
raw response schema is not published in a form Sam could verify).

Security properties enforced here:

* pinned HTTPS host; the URL is built only from an allowlisted model id
* ``trust_env=False`` and ``follow_redirects=False``: no proxy/env can redirect
  the key and a 3xx is refused
* the key exists only in this adapter and only in the request header
* finite timeout; bounded request and response; NO retry, NO other model or
  provider, no billing or tier control
* a non-2xx body is read only up to 16 KiB and reduced to allowlisted fields
  (``gemini_errors``); the raw body is never kept
* Sam does not touch Google's safety settings: a provider block is a REFUSAL
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from sam.agent.models import MessageRole
from sam.models.credentials import ProviderCredential
from sam.models.errors import ProviderFailure
from sam.models.models import (
    Availability,
    FailureCategory,
    GeminiFreeAttestation,
    ModelRequest,
    ProviderId,
    ProviderOutput,
    UsageMetadata,
)
from sam.models.providers.gemini_errors import MAX_ERROR_BODY_BYTES, safe_error_detail
from sam.models.registry import GEMINI_EFFICIENT_MODEL, GEMINI_GENERAL_MODEL

GEMINI_HOST = "generativelanguage.googleapis.com"
GEMINI_ALLOWED_MODELS = frozenset({GEMINI_GENERAL_MODEL, GEMINI_EFFICIENT_MODEL})
MAX_REQUEST_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 1_000_000
_REFUSAL_FINISH = frozenset(
    {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "RECITATION"}
)


def endpoint_for(model_id: str) -> str:
    if model_id not in GEMINI_ALLOWED_MODELS:
        raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "model_not_allowed")
    url = f"https://{GEMINI_HOST}/v1beta/models/{model_id}:generateContent"
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != GEMINI_HOST
        or parts.port not in (None, 443)
        or parts.query
        or parts.username is not None
    ):
        raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "endpoint_not_trusted")
    return url


def build_client(
    transport: httpx.BaseTransport | None, timeout_seconds: float
) -> httpx.Client:
    """The one place an HTTP client is configured. Exposed for tests."""

    return httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        trust_env=False,
        http2=False,
    )


def build_body(request: ModelRequest) -> dict[str, Any]:
    system = [m.content for m in request.messages if m.role is MessageRole.SYSTEM]
    contents = [
        {
            "role": "model" if m.role is MessageRole.ASSISTANT else "user",
            "parts": [{"text": m.content}],
        }
        for m in request.messages
        if m.role in (MessageRole.USER, MessageRole.ASSISTANT)
    ]
    if not contents:
        raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "empty_request")
    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": request.max_output_tokens},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
    return body


class GeminiFreeProvider:
    provider_id = ProviderId.GEMINI_FREE

    def __init__(
        self,
        credential: ProviderCredential | None,
        *,
        transport: httpx.BaseTransport | None = None,
        attestation: Callable[[], GeminiFreeAttestation] | None = None,
    ) -> None:
        self._credential = credential
        self._transport = transport  # tests inject httpx.MockTransport
        # The owner's session-only attestation that the project is unbilled. With
        # none supplied the provider is treated as UNATTESTED and refuses: an API
        # key does not prove a Google project is on the free tier.
        self._attestation = attestation

    def availability(self) -> Availability:
        return (
            Availability.AVAILABLE
            if self._credential is not None
            else Availability.NOT_CONFIGURED
        )

    def __repr__(self) -> str:
        return f"GeminiFreeProvider(configured={self._credential is not None})"

    def complete(
        self, request: ModelRequest, *, model_id: str, timeout_seconds: float
    ) -> ProviderOutput:
        if self._credential is None:
            raise ProviderFailure(FailureCategory.UNAVAILABLE, "not_configured")
        if (
            self._attestation is None
            or self._attestation() is not GeminiFreeAttestation.OWNER_ATTESTED_UNBILLED
        ):
            # Before ANY request: no attestation, no network.
            raise ProviderFailure(
                FailureCategory.POLICY_BLOCKED, "free_tier_unattested"
            )
        url = endpoint_for(model_id)
        if not 0 < timeout_seconds <= 300:
            raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "bad_timeout")
        payload = json.dumps(build_body(request), ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_REQUEST_BYTES:
            raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "request_too_large")
        headers = {
            "x-goog-api-key": self._credential.reveal(),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "sam-models/0.1",
        }
        deadline = time.monotonic() + timeout_seconds
        # Never echoed: the key and every message are scrubbed from diagnostics.
        redact = [self._credential.reveal(), *(m.content for m in request.messages)]
        try:
            with build_client(self._transport, timeout_seconds) as client:
                with client.stream(
                    "POST", url, headers=headers, content=payload
                ) as response:
                    body = self._read(response, deadline, redact)
        except ProviderFailure:
            raise
        except httpx.TimeoutException:
            raise ProviderFailure(FailureCategory.TIMEOUT, "timeout") from None
        except Exception:
            # Connection/protocol errors: generic, no cause, no key in the text.
            raise ProviderFailure(
                FailureCategory.UNAVAILABLE, "request_failed"
            ) from None
        return self._parse(body, model_id)

    @staticmethod
    def _error_detail(
        response: httpx.Response, deadline: float, redact: list[str]
    ) -> dict[str, str | int]:
        """Allowlisted fields of a non-2xx body (bounded read). The raw body is
        never kept: it may echo provider text, prompts or identifiers."""

        status = response.status_code
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            return {"http_status": status, "shape": "encoded"}
        body = bytearray()
        try:
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) >= MAX_ERROR_BODY_BYTES or time.monotonic() > deadline:
                    break
        except httpx.HTTPError:
            return {"http_status": status, "shape": "unreadable"}
        return safe_error_detail(
            status, bytes(body[:MAX_ERROR_BODY_BYTES]), redact=redact
        )

    @classmethod
    def _read(
        cls, response: httpx.Response, deadline: float, redact: list[str]
    ) -> bytes:
        status = response.status_code
        if 300 <= status < 400:
            raise ProviderFailure(FailureCategory.UNAVAILABLE, "redirect_refused")
        if status >= 400:
            # Same category and code as ever (routing is unchanged); the detail
            # is diagnostic metadata only.
            detail = cls._error_detail(response, deadline, redact)
            if status == 429:
                # Free quota exhausted: never enables billing, never retried.
                raise ProviderFailure(
                    FailureCategory.RATE_LIMIT, "free_quota_exhausted", detail
                )
            if status in (401, 403):
                raise ProviderFailure(
                    FailureCategory.AUTH_FAILURE, "not_authorized", detail
                )
            raise ProviderFailure(FailureCategory.UNAVAILABLE, f"http_{status}", detail)
        if status != 200:
            raise ProviderFailure(FailureCategory.UNAVAILABLE, f"http_{status}")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "content_encoding")
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_RESPONSE_BYTES:
                    raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "too_large")
            except ValueError:
                raise ProviderFailure(
                    FailureCategory.INVALID_RESPONSE, "bad_length"
                ) from None
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "too_large")
            chunks.append(chunk)
            if time.monotonic() > deadline:
                raise ProviderFailure(FailureCategory.TIMEOUT, "timeout")
        if time.monotonic() > deadline:
            raise ProviderFailure(FailureCategory.TIMEOUT, "timeout")
        return b"".join(chunks)

    @staticmethod
    def _parse(body: bytes, model_id: str) -> ProviderOutput:
        try:
            document = json.loads(body)
        except ValueError:
            raise ProviderFailure(
                FailureCategory.INVALID_RESPONSE, "not_json"
            ) from None
        if not isinstance(document, dict):
            raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "not_object")
        feedback = document.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise ProviderFailure(FailureCategory.REFUSAL, "provider_refusal")
        candidates = document.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "no_candidates")
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "bad_candidate")
        finish = candidate.get("finishReason")
        content = candidate.get("content")
        texts: list[str] = []
        if isinstance(content, dict) and isinstance(content.get("parts"), list):
            for part in content["parts"]:
                if (
                    isinstance(part, dict)
                    and isinstance(part.get("text"), str)
                    and not part.get("thought")
                ):
                    texts.append(part["text"])
        text = "".join(texts).strip()
        if not text:
            if finish in _REFUSAL_FINISH:
                raise ProviderFailure(FailureCategory.REFUSAL, "provider_refusal")
            raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "no_text")
        return ProviderOutput(
            text=text, model_id=model_id, usage=_usage(document.get("usageMetadata"))
        )


def _usage(value: object) -> UsageMetadata | None:
    if not isinstance(value, dict):
        return None

    def count(key: str) -> int | None:
        item = value.get(key)
        return item if isinstance(item, int) and not isinstance(item, bool) else None

    try:
        return UsageMetadata(
            input_tokens=count("promptTokenCount"),
            output_tokens=count("candidatesTokenCount"),
        )
    except ValueError:
        return None


__all__ = [
    "GEMINI_ALLOWED_MODELS",
    "GEMINI_HOST",
    "GeminiFreeProvider",
    "build_client",
    "build_body",
    "endpoint_for",
]
