"""Deterministic tests for the Claude provider adapter."""

import logging
import time
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from sam.agent import claude
from sam.agent.claude import ClaudeProvider
from sam.agent.errors import (
    AuthenticationError,
    MalformedProviderResponseError,
    ProviderOverloadedError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from sam.agent.models import Message, MessageRole
from sam.core.config import Settings


def _mock_message_response(
    text: str = "ok", model: str = "test-model"
) -> httpx.Response:
    """Build a wire-shaped Anthropic Messages API success response."""

    return httpx.Response(
        200,
        json={
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


class FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, messages: FakeMessages) -> None:
        self.messages = messages


def settings() -> Settings:
    return Settings.model_validate(
        {
            "anthropic_api_key": SecretStr("x"),
            "claude_model": "test-model",
            "claude_timeout": 7.5,
        }
    )


def response(text: str = "Hello") -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        model="test-model",
        stop_reason="end_turn",
    )


def test_successful_response_is_translated() -> None:
    messages = FakeMessages(response())
    provider = ClaudeProvider(settings(), client=cast(Any, FakeClient(messages)))

    result = provider.complete([Message(role=MessageRole.USER, content="Hi")])

    assert result.message.content == "Hello"
    assert result.message.role is MessageRole.ASSISTANT
    assert result.model == "test-model"
    assert messages.calls[0]["model"] == "test-model"
    assert messages.calls[0]["max_tokens"] == 1024
    assert messages.calls[0]["messages"] == [{"role": "user", "content": "Hi"}]


def test_missing_api_key_is_an_authentication_error() -> None:
    provider = ClaudeProvider(Settings())

    with pytest.raises(AuthenticationError):
        provider.complete([Message(role=MessageRole.USER, content="Hi")])


def test_sdk_environment_cannot_override_explicit_endpoint_or_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Undocumented SDK routing and header variables are ignored."""

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-evil: yes")
    provider = ClaudeProvider(settings())

    provider.open()

    assert provider.client is not None
    assert str(provider.client._client.base_url) == "https://api.anthropic.com/"
    assert provider.client.max_retries == 2
    assert "x-evil" not in str(provider.client._client.headers).lower()
    provider.close()
    assert provider.client is None


def test_sdk_retry_policy_is_owned_by_sam_settings() -> None:
    configured = Settings.model_validate(
        {
            "anthropic_api_key": SecretStr("x"),
            "claude_max_retries": 1,
        }
    )
    provider = ClaudeProvider(configured)

    provider.open()

    assert provider.client is not None
    assert provider.client.max_retries == 1
    provider.close()


def test_malformed_response_is_rejected() -> None:
    messages = FakeMessages(
        SimpleNamespace(
            content=[SimpleNamespace(type="tool_use")],
            model="test-model",
            stop_reason="tool_use",
        )
    )
    provider = ClaudeProvider(settings(), client=cast(Any, FakeClient(messages)))

    with pytest.raises(MalformedProviderResponseError):
        provider.complete([Message(role=MessageRole.USER, content="Hi")])


@pytest.mark.parametrize(
    "malformed",
    [
        SimpleNamespace(model="test-model"),
        SimpleNamespace(content=None, model="test-model"),
        SimpleNamespace(content=object(), model="test-model"),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")]),
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text=3)], model="test-model"
        ),
        SimpleNamespace(
            content=[SimpleNamespace(type="tool_use")], model="test-model"
        ),
        object(),
    ],
)
def test_all_malformed_provider_shapes_are_translated(malformed: object) -> None:
    messages = FakeMessages(malformed)
    provider = ClaudeProvider(settings(), client=cast(Any, FakeClient(messages)))

    with pytest.raises(MalformedProviderResponseError):
        provider.complete([Message(role=MessageRole.USER, content="Hi")])


@pytest.mark.parametrize(
    ("exception_name", "application_error"),
    [
        ("auth", AuthenticationError),
        ("timeout", ProviderTimeoutError),
        ("unavailable", ProviderUnavailableError),
        ("rate_limit", ProviderRateLimitError),
        ("overloaded", ProviderOverloadedError),
        ("server", ProviderServerError),
        ("request", ProviderRequestError),
    ],
)
def test_provider_failures_are_translated(
    monkeypatch: pytest.MonkeyPatch,
    exception_name: str,
    application_error: type[Exception],
) -> None:
    exception_type = type(f"Fake{exception_name.title()}", (Exception,), {})
    target_name = {
        "auth": "AnthropicAuthenticationError",
        "timeout": "APITimeoutError",
        "unavailable": "APIConnectionError",
        "rate_limit": "RateLimitError",
        "overloaded": "OverloadedError",
        "server": "InternalServerError",
        "request": "APIStatusError",
    }[exception_name]
    monkeypatch.setattr(claude, target_name, exception_type)
    messages = FakeMessages(error=exception_type("provider failure"))
    provider = ClaudeProvider(settings(), client=cast(Any, FakeClient(messages)))

    with pytest.raises(application_error):
        provider.complete([Message(role=MessageRole.USER, content="Hi")])


def test_client_initialization_failures_are_translated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_client(**kwargs: object) -> None:
        raise RuntimeError("secret")

    monkeypatch.setattr(claude, "Anthropic", fail_client)
    provider = ClaudeProvider(settings())

    with pytest.raises(ProviderRequestError):
        provider.open()


def test_provider_logs_do_not_include_prompt_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sam.core.logging import configure_logging

    configure_logging("DEBUG")
    sentinel = "UNIQUE_PROMPT_SENTINEL"
    messages = FakeMessages(response("safe response"))
    provider = ClaudeProvider(settings(), client=cast(Any, FakeClient(messages)))

    with caplog.at_level("DEBUG"):
        logging.getLogger("httpx").debug(sentinel)
        provider.complete([Message(role=MessageRole.USER, content=sentinel)])

    assert sentinel not in caplog.text


def test_env_base_url_cannot_redirect_real_wire_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poisoned SDK environment never changes the host a real request dials.

    Unlike the attribute-inspection test above, this drives an actual
    ``Anthropic`` client through ``httpx.MockTransport`` so the assertion is
    made against the request that would have gone out on the wire.
    """

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-evil: yes")
    dialed_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        dialed_hosts.append(request.url.host)
        assert "x-evil" not in request.headers
        return _mock_message_response()

    provider = ClaudeProvider(
        settings(), http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    result = provider.complete([Message(role=MessageRole.USER, content="Hi")])

    assert dialed_hosts == ["api.anthropic.com"]
    assert result.message.content == "ok"
    provider.close()


def test_retry_policy_is_enforced_during_real_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured retry count bounds real provider retries end to end."""

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            500,
            json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
        )

    configured = Settings.model_validate(
        {"anthropic_api_key": SecretStr("x"), "claude_max_retries": 1}
    )
    provider = ClaudeProvider(
        configured,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ProviderServerError):
        provider.complete([Message(role=MessageRole.USER, content="Hi")])

    assert attempts == 2  # one original attempt plus exactly one configured retry
    provider.close()


def test_real_sdk_call_does_not_leak_prompt_via_third_party_loggers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A real end-to-end call proves the prompt never reaches captured logs.

    The Anthropic SDK logs ``Request options`` (including the JSON request
    body) at DEBUG on the ``anthropic`` logger when DEBUG is enabled for it.
    This drives a real request through that code path and asserts the
    sentinel prompt is absent, rather than merely emitting a synthetic log
    line on a mocked branch.
    """

    from sam.core.logging import configure_logging

    configure_logging("DEBUG")
    sentinel = "UNIQUE_PROMPT_SENTINEL_WIRE"

    def handler(request: httpx.Request) -> httpx.Response:
        return _mock_message_response(text="safe reply")

    provider = ClaudeProvider(
        settings(), http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with caplog.at_level("DEBUG"):
        result = provider.complete([Message(role=MessageRole.USER, content=sentinel)])

    assert result.message.content == "safe reply"
    assert sentinel not in caplog.text
    provider.close()
