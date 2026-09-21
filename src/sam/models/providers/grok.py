"""Grok / xAI API: DISABLED. No credits are bought, the consumer Grok site is
never automated, and no xAI credential is read or required."""

from __future__ import annotations

from sam.models.models import ProviderId
from sam.models.providers.disabled import DisabledPaidProvider


class GrokProvider(DisabledPaidProvider):
    def __init__(self) -> None:
        super().__init__(ProviderId.GROK_API)


__all__ = ["GrokProvider"]
