"""The contract every provider adapter implements."""

from __future__ import annotations

from typing import Protocol

from sam.models.models import Availability, ModelRequest, ProviderId, ProviderOutput


class ModelProvider(Protocol):
    provider_id: ProviderId

    def availability(self) -> Availability:
        """Cheap, local, non-billable. Never calls a model."""

    def complete(
        self, request: ModelRequest, *, model_id: str, timeout_seconds: float
    ) -> ProviderOutput:
        """One model call. Raise ``ProviderFailure`` with a normalized category
        on any failure. Must not retry, must not switch model/provider/endpoint,
        and must return only text: it can never execute a tool."""


__all__ = ["ModelProvider"]
