"""Typed models shared by the agent core and providers."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MessageRole(StrEnum):
    """Roles supported by the agent conversation contract."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ExecutionStatus(StrEnum):
    """Finite states for one bounded agent execution."""

    COMPLETED = "completed"


class Message(BaseModel):
    """A single typed conversation message."""

    role: MessageRole
    content: str = Field(min_length=1)


class AgentRequest(BaseModel):
    """Input accepted by the bounded agent execution endpoint."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=100_000)


class AgentResponse(BaseModel):
    """Stable response returned by the agent core."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    status: ExecutionStatus = ExecutionStatus.COMPLETED
    turns: int = Field(default=1, ge=1)


class ProviderResponse(BaseModel):
    """Provider-neutral response returned from one model turn."""

    message: Message
    model: str = Field(min_length=1)
    stop_reason: str | None = None