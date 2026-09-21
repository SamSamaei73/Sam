"""Deterministic in-process provider for tests. No network and no process spawning."""

from __future__ import annotations

from collections.abc import Callable

from sam.models.errors import ProviderFailure
from sam.models.models import (
    Availability,
    FailureCategory,
    ModelRequest,
    ProviderId,
    ProviderOutput,
)


class FakeProvider:
    def __init__(
        self,
        provider_id: ProviderId,
        *,
        text: str | Callable[[ModelRequest], str] = "ok",
        failure: ProviderFailure | None = None,
        availability: Availability = Availability.AVAILABLE,
        raises: Exception | None = None,
        model_override: str | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._text = text
        self._failure = failure
        self._availability = availability
        self._raises = raises
        self._model_override = model_override
        self.calls: list[ModelRequest] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def availability(self) -> Availability:
        return self._availability

    def complete(
        self, request: ModelRequest, *, model_id: str, timeout_seconds: float
    ) -> ProviderOutput:
        self.calls.append(request)
        if self._raises is not None:
            raise self._raises
        if self._failure is not None:
            raise self._failure
        text = self._text(request) if callable(self._text) else self._text
        return ProviderOutput(text=text, model_id=self._model_override or model_id)

    @staticmethod
    def refusal() -> ProviderFailure:
        return ProviderFailure(FailureCategory.REFUSAL, "provider_refusal")


__all__ = ["FakeProvider"]
