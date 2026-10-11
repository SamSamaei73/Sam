"""Trusted composition of the router from Sam's own configuration."""

from __future__ import annotations

from dataclasses import replace
from threading import RLock

import httpx

from sam.core.config import Settings
from sam.models.audit import InMemoryRoutingAuditSink, RoutingAuditSink
from sam.models.credentials import ProviderCredential
from sam.models.models import (
    ClaudeImprovementState,
    GeminiFreeAttestation,
    ProviderId,
)
from sam.models.policies import CostPolicy, OwnerPreferences, PersonalContentPolicy
from sam.models.provider import ModelProvider
from sam.models.providers.claude_subscription import (
    ClaudeSubscriptionProvider,
    Runner,
    run_bounded,
)
from sam.models.providers.gemini import GeminiFreeProvider
from sam.models.providers.grok import GrokProvider
from sam.models.providers.openai import OpenAIProvider
from sam.models.providers.openrouter import OpenRouterProvider
from sam.models.registry import build_registry
from sam.models.router import ModelRouter


class ModelSettingsStore:
    """The owner's trusted routing/privacy/content settings for this session.

    They change only through the Desktop settings route (step-up authenticated
    when they loosen privacy), never from a prompt, a model, or a provider.
    In-memory: they reset to trusted bootstrap defaults on restart.
    """

    def __init__(self, *, preferences: OwnerPreferences | None = None) -> None:
        self._preferences = preferences or OwnerPreferences()
        self._content = PersonalContentPolicy()
        self._lock = RLock()

    def preferences(self) -> OwnerPreferences:
        with self._lock:
            return self._preferences

    def content(self) -> PersonalContentPolicy:
        with self._lock:
            return self._content

    def update(
        self,
        *,
        preferred_provider: ProviderId | None,
        allow_free_fallback: bool,
        personal_to_free_tier: bool,
        private_to_free_tier: bool,
        claude_improvement_state: ClaudeImprovementState,
        private_to_claude_when_improvement_enabled: bool,
        gemini_attestation: GeminiFreeAttestation,
        topic_blocklist: tuple[str, ...],
    ) -> None:
        new_content = replace(self._content, topic_blocklist=topic_blocklist)
        with self._lock:
            self._preferences = OwnerPreferences(
                preferred_provider=preferred_provider,
                allow_free_fallback=allow_free_fallback,
                personal_to_free_tier=personal_to_free_tier,
                private_to_free_tier=private_to_free_tier,
                claude_improvement_state=claude_improvement_state,
                private_to_claude_when_improvement_enabled=(
                    private_to_claude_when_improvement_enabled
                ),
                gemini_attestation=gemini_attestation,
            )
            self._content = new_content


def build_model_router(
    settings: Settings,
    *,
    store: ModelSettingsStore | None = None,
    audit: RoutingAuditSink | None = None,
    gemini_transport: httpx.BaseTransport | None = None,
    openrouter_transport: httpx.BaseTransport | None = None,
    claude_runner: Runner = run_bounded,
) -> tuple[ModelRouter, ModelSettingsStore]:
    """Build the router from trusted settings. An OpenRouter key opts in to
    that provider as the default primary, without enabling any paid fallback.
    An explicitly supplied store retains the owner's existing preferences.
    Direct OpenAI and Grok adapters remain disabled and make zero calls.
    """

    openrouter_key = settings.openrouter_api_key
    openrouter_credential = (
        ProviderCredential(openrouter_key.get_secret_value())
        if openrouter_key
        else None
    )
    store = store or ModelSettingsStore(
        preferences=OwnerPreferences(
            preferred_provider=(
                ProviderId.OPENROUTER if openrouter_credential else None
            )
        )
    )
    key = settings.gemini_api_key
    credential = ProviderCredential(key.get_secret_value()) if key else None
    registry = build_registry(
        gemini_configured=credential is not None,
        claude_enabled=settings.claude_subscription_enabled,
        deployment_mode=settings.deployment_mode,
        openrouter_configured=openrouter_credential is not None,
        openrouter_model=settings.openrouter_model,
    )
    providers: dict[ProviderId, ModelProvider] = {
        ProviderId.CLAUDE_SUBSCRIPTION: ClaudeSubscriptionProvider(
            executable=settings.claude_code_executable,
            runner=claude_runner,
            deployment_mode=settings.deployment_mode,
        ),
        ProviderId.GEMINI_FREE: GeminiFreeProvider(
            credential,
            transport=gemini_transport,
            attestation=lambda: store.preferences().gemini_attestation,
        ),
        ProviderId.OPENAI_API: OpenAIProvider(),
        ProviderId.GROK_API: GrokProvider(),
        ProviderId.OPENROUTER: OpenRouterProvider(
            openrouter_credential,
            model_id=settings.openrouter_model,
            transport=openrouter_transport,
        ),
    }
    router = ModelRouter(
        registry=registry,
        providers=providers,
        preferences=store.preferences,
        content_policy=store.content,
        audit=audit or InMemoryRoutingAuditSink(),
        cost=CostPolicy(
            allowed_paid_providers=(
                frozenset({ProviderId.OPENROUTER})
                if openrouter_credential
                else frozenset()
            )
        ),
    )
    return router, store


__all__ = ["ModelSettingsStore", "build_model_router"]
