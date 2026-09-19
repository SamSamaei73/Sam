"""Tests for bounded AgentCore execution and the tool contract."""

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from sam.agent.core import AgentCore
from sam.agent.errors import AgentExecutionError, InvalidRequestError
from sam.agent.models import AgentRequest, Message, MessageRole, ProviderResponse
from sam.agent.tools import Tool


class FakeProvider:
    def __init__(self, response: ProviderResponse | Exception) -> None:
        self.response = response
        self.calls: list[Sequence[Message]] = []

    def complete(self, messages: Sequence[Message]) -> ProviderResponse:
        self.calls.append(messages)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def provider_response() -> ProviderResponse:
    return ProviderResponse(
        message=Message(role=MessageRole.ASSISTANT, content="Done"),
        model="test-model",
    )


def test_agent_core_success_is_bounded_to_one_turn() -> None:
    provider = FakeProvider(provider_response())
    core = AgentCore(provider)

    result = core.execute(AgentRequest(message="Do this"))

    assert result.message == "Done"
    assert result.execution_id == "direct"
    assert result.status == "completed"
    assert result.turns == 1
    assert len(provider.calls) == 1
    assert provider.calls[0][0].role is MessageRole.USER
    assert provider.calls[0][0].content == "Do this"


def test_agent_core_rejects_blank_direct_requests() -> None:
    provider = FakeProvider(provider_response())

    with pytest.raises(InvalidRequestError):
        AgentCore(provider).execute(AgentRequest(message="   "))


def test_agent_core_translates_unexpected_provider_failures() -> None:
    provider = FakeProvider(RuntimeError("private provider detail"))

    with pytest.raises(AgentExecutionError) as error:
        AgentCore(provider).execute(AgentRequest(message="Do this"))

    assert str(error.value) == "Agent execution failed"
    assert "private provider detail" not in str(error.value)


def test_agent_core_rejects_unbounded_configuration() -> None:
    with pytest.raises(ValueError):
        AgentCore(FakeProvider(provider_response()), max_turns=0)

    with pytest.raises(ValueError):
        AgentCore(FakeProvider(provider_response()), max_turns=2)


class FakeTool:
    name = "test_tool"
    description = "A deterministic test tool."

    def input_schema(self) -> Mapping[str, Any]:
        return {"type": "object"}

    def execute(self, arguments: Mapping[str, Any]) -> Any:
        return arguments


def accept_tool(tool: Tool) -> tuple[str, Mapping[str, Any], Any]:
    return tool.name, tool.input_schema(), tool.execute({"value": "ok"})


def test_tool_contract_is_implementable_without_execution_side_effects() -> None:
    name, schema, result = accept_tool(FakeTool())

    assert name == "test_tool"
    assert schema == {"type": "object"}
    assert result == {"value": "ok"}
