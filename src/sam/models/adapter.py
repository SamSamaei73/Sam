"""The ONE trusted model-invocation boundary for AgentCore.

``AgentCore`` keeps calling ``LLMProvider.complete(messages)``; this adapter
turns that into a routed, policy-checked call. Nothing else in Sam calls a
language-model provider directly (TTS and STT are separate, specialized paths).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

from sam.agent.errors import (
    AgentError,
    AuthenticationError,
    MalformedProviderResponseError,
    PolicyBlockedError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from sam.agent.models import Message, MessageRole, ProviderResponse
from sam.language.policy import detect_language
from sam.models.models import (
    Capability,
    FailureCategory,
    ModelRequest,
    PrivacyClass,
    TaskProfile,
)
from sam.models.router import ModelRouter

_DECLARED: ContextVar[PrivacyClass] = ContextVar(
    "sam_privacy", default=PrivacyClass.NORMAL
)


@contextmanager
def privacy_scope(privacy: PrivacyClass) -> Iterator[None]:
    """Trusted callers mark a request PERSONAL/PRIVATE for its duration. A scope
    can only RAISE the class (never lower it), and SECRET is set by detection,
    not by a caller."""

    token = _DECLARED.set(max(_DECLARED.get(), privacy))
    try:
        yield
    finally:
        _DECLARED.reset(token)


_FAILURES: dict[FailureCategory, type[AgentError]] = {
    FailureCategory.AUTH_FAILURE: AuthenticationError,
    FailureCategory.RATE_LIMIT: ProviderRateLimitError,
    FailureCategory.TIMEOUT: ProviderTimeoutError,
    FailureCategory.UNAVAILABLE: ProviderUnavailableError,
    FailureCategory.COST_BLOCKED: ProviderUnavailableError,
    FailureCategory.INVALID_RESPONSE: MalformedProviderResponseError,
    FailureCategory.REFUSAL: ProviderRefusalError,
    FailureCategory.POLICY_BLOCKED: PolicyBlockedError,
}


class RoutedLLMProvider:
    """Implements the Phase 2 ``LLMProvider`` contract over the router."""

    def __init__(self, router: ModelRouter) -> None:
        self._router = router

    @property
    def router(self) -> ModelRouter:
        return self._router

    def open(self) -> None:
        """Nothing to open: adapters create their transport per call."""

    def close(self) -> None:
        """Nothing to close."""

    def complete(self, messages: Sequence[Message]) -> ProviderResponse:
        request = self._request(messages)
        result = self._router.execute(request)
        if result.status is not FailureCategory.SUCCESS or not result.text:
            raise _FAILURES.get(result.status, ProviderUnavailableError)(
                f"model request failed: {result.diagnostic_code}"
            )
        return ProviderResponse(
            message=Message(role=MessageRole.ASSISTANT, content=result.text),
            model=result.model_id or "unknown",
            stop_reason=None,
        )

    @staticmethod
    def _request(messages: Sequence[Message]) -> ModelRequest:
        last_user = next(
            (m.content for m in reversed(messages) if m.role is MessageRole.USER), ""
        )
        language = detect_language(last_user)
        capabilities = {Capability.TEXT}
        profile = TaskProfile.GENERAL
        if language == "fa":
            capabilities.add(Capability.PERSIAN)
            profile = TaskProfile.PERSIAN
        return ModelRequest(
            request_id=uuid.uuid4().hex,
            messages=tuple(messages),
            language=language or "auto",
            capabilities=frozenset(capabilities),
            privacy_class=_DECLARED.get(),
            task_profile=profile,
        )


__all__ = ["RoutedLLMProvider", "privacy_scope"]
