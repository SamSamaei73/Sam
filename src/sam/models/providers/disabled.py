"""Paid providers that are architecturally present but operationally absent.

This module has no network, subprocess, credential or SDK code: a disabled
provider makes zero calls by construction, and the router never selects one
while ``PAID_FALLBACK = OFF``.
"""

from __future__ import annotations

from sam.models.errors import ProviderFailure
from sam.models.models import (
    Availability,
    FailureCategory,
    ModelRequest,
    ProviderId,
    ProviderOutput,
)


class DisabledPaidProvider:
    def __init__(self, provider_id: ProviderId) -> None:
        self.provider_id = provider_id

    def availability(self) -> Availability:
        return Availability.DISABLED

    def complete(
        self, request: ModelRequest, *, model_id: str, timeout_seconds: float
    ) -> ProviderOutput:
        raise ProviderFailure(FailureCategory.COST_BLOCKED, "provider_disabled")


__all__ = ["DisabledPaidProvider"]
