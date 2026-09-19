"""Bounded orchestration for one agent request."""

import logging
from collections.abc import Sequence

from sam.agent.errors import AgentError, AgentExecutionError, InvalidRequestError
from sam.agent.models import AgentRequest, AgentResponse, Message, MessageRole
from sam.agent.provider import LLMProvider

logger = logging.getLogger(__name__)


class AgentCore:
    """Coordinate a bounded provider call without provider-specific coupling."""

    def __init__(self, provider: LLMProvider, max_turns: int = 1) -> None:
        if max_turns != 1:
            raise ValueError("Phase 2 supports exactly one bounded turn")
        self._provider = provider
        self._max_turns = max_turns

    def execute(
        self, request: AgentRequest, execution_id: str = "direct"
    ) -> AgentResponse:
        """Execute at most the configured number of provider turns."""

        message = request.message.strip()
        if not message:
            raise InvalidRequestError("message must not be blank")

        logger.info("Agent execution started id=%s", execution_id)
        messages: Sequence[Message] = [
            Message(role=MessageRole.USER, content=message),
        ]
        try:
            for _ in range(self._max_turns):
                logger.info("Provider call started id=%s", execution_id)
                provider_response = self._provider.complete(messages)
                logger.info("Provider call completed id=%s", execution_id)
                logger.info("Agent execution completed id=%s", execution_id)
                return AgentResponse(
                    message=provider_response.message.content,
                    execution_id=execution_id,
                    turns=1,
                )
        except AgentError:
            logger.info("Agent execution failed id=%s", execution_id)
            raise
        except Exception as error:
            logger.info("Agent execution failed id=%s", execution_id)
            raise AgentExecutionError("Agent execution failed") from error

        raise AgentExecutionError("Agent execution exceeded its turn limit")