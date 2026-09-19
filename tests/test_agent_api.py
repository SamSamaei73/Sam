"""Tests for the agent HTTP boundary and safe error mapping."""

from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sam.agent.core import AgentCore
from sam.agent.errors import (
    AuthenticationError,
    InvalidRequestError,
    ProviderOverloadedError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from sam.agent.models import Message, MessageRole, ProviderResponse
from sam.core.config import Settings
from sam.main import create_app


class FakeProvider:
    def __init__(self, result: ProviderResponse | Exception) -> None:
        self.result = result

    def complete(self, messages: Sequence[Message]) -> ProviderResponse:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def success_response() -> ProviderResponse:
    return ProviderResponse(
        message=Message(role=MessageRole.ASSISTANT, content="Response"),
        model="test-model",
    )


def client_for(result: ProviderResponse | Exception) -> TestClient:
    application = create_app(Settings())
    application.state.agent_core = AgentCore(FakeProvider(result))
    return TestClient(application)


def test_agent_api_success() -> None:
    with client_for(success_response()) as client:
        response = client.post("/agent", json={"message": "Hello"})

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Response"
    assert body["status"] == "completed"
    assert body["turns"] == 1
    assert len(body["execution_id"]) == 32


def test_agent_api_rejects_empty_payload() -> None:
    with client_for(success_response()) as client:
        response = client.post("/agent", json={"message": ""})

    assert response.status_code == 422


def test_agent_api_rejects_unknown_fields() -> None:
    with client_for(success_response()) as client:
        response = client.post(
            "/agent", json={"message": "Hello", "unexpected": "value"}
        )

    assert response.status_code == 422


def test_agent_api_maps_invalid_request() -> None:
    with client_for(InvalidRequestError("message must not be blank")) as client:
        response = client.post("/agent", json={"message": "Hello"})

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "invalid_request",
            "message": "Invalid agent request",
        }
    }


def test_agent_api_maps_provider_timeout() -> None:
    with client_for(
        ProviderTimeoutError("provider detail must stay private")
    ) as client:
        response = client.post("/agent", json={"message": "Hello"})

    assert response.status_code == 504
    assert response.json() == {
        "error": {
            "code": "provider_timeout",
            "message": "Provider request timed out",
        }
    }


def test_agent_api_does_not_expose_authentication_exception_details() -> None:
    with client_for(AuthenticationError("secret api key detail")) as client:
        response = client.post("/agent", json={"message": "Hello"})

    body: dict[str, Any] = response.json()
    assert response.status_code == 502
    assert body["error"] == {
        "code": "provider_authentication_failed",
        "message": "Provider authentication failed",
    }


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (ProviderRateLimitError("private"), 429, "provider_rate_limited"),
        (ProviderOverloadedError("private"), 503, "provider_overloaded"),
        (ProviderServerError("private"), 502, "provider_server_error"),
        (ProviderUnavailableError("private"), 503, "provider_unavailable"),
        (ProviderRequestError("private"), 502, "provider_request_failed"),
    ],
)
def test_agent_api_maps_provider_error_taxonomy(
    error: Exception, status_code: int, code: str
) -> None:
    with client_for(error) as client:
        response = client.post("/agent", json={"message": "Hello"})

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["message"] != "private"


def test_agent_openapi_documents_error_responses() -> None:
    with client_for(success_response()) as client:
        schema = client.get("/openapi.json").json()

    responses = schema["paths"]["/agent"]["post"]["responses"]
    for status_code in ("400", "429", "502", "503", "504"):
        assert status_code in responses
