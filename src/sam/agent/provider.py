"""Provider abstraction used by the agent core."""

from collections.abc import Sequence
from typing import Protocol

from sam.agent.models import Message, ProviderResponse


class LLMProvider(Protocol):
    """Contract for a synchronous language model provider."""

    def complete(self, messages: Sequence[Message]) -> ProviderResponse:
        """Generate one response for the supplied conversation."""