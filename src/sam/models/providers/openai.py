"""OpenAI API: DISABLED. A ChatGPT subscription does not include API access,
Sam does not create billing, and no OpenAI credential is read or required."""

from __future__ import annotations

from sam.models.models import ProviderId
from sam.models.providers.disabled import DisabledPaidProvider


class OpenAIProvider(DisabledPaidProvider):
    def __init__(self) -> None:
        super().__init__(ProviderId.OPENAI_API)


__all__ = ["OpenAIProvider"]
