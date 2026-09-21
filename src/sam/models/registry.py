"""The trusted, immutable provider registry.

Descriptors are Sam configuration. Nothing a user, a prompt, model output, an
MCP result or a provider response says can create, edit or enable one. A
provider cannot mark itself free: the billing mode is here, in Sam's code.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from sam.models.errors import RegistryError
from sam.models.models import (
    AuthMode,
    BillingMode,
    Capability,
    DeploymentMode,
    ProviderId,
)

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Verified against the official Google docs on 2026-09-20 (see docs/model-routing.md).
GEMINI_GENERAL_MODEL = "gemini-3.8-flash"
GEMINI_EFFICIENT_MODEL = "gemini-3.5-flash-lite"
# The Claude Code subscription session picks its own model; Sam never names one.
CLAUDE_SUBSCRIPTION_MODEL = "subscription_default"

_CANONICAL_ORDER = tuple(ProviderId)


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    tier: Literal["general", "efficient"] = "general"

    def __post_init__(self) -> None:
        if not _MODEL_RE.match(self.model_id):
            raise RegistryError("model id is malformed")


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: ProviderId
    display_name: str
    enabled: bool
    billing_mode: BillingMode
    auth_mode: AuthMode
    capabilities: frozenset[Capability]
    models: tuple[ModelSpec, ...]
    external_processing: bool
    free_tier_data_use: bool  # provider's free tier may use content to improve products
    fallback_eligible: bool
    data_use_note: str
    # True when the provider is only 'free' if the OWNER attests the account is
    # unbilled (Sam cannot verify cloud billing state).
    requires_free_attestation: bool = False
    max_input_chars: int = 100_000
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.models:
            raise RegistryError("a provider needs at least one allowed model")
        if not 0 < self.timeout_seconds <= 300:
            raise RegistryError("timeout is out of bounds")
        if len({m.model_id for m in self.models}) != len(self.models):
            raise RegistryError("duplicate model id")

    @property
    def cost_class(self) -> BillingMode:
        """The trusted cost class (billing mode) as a display-friendly name."""

        return self.billing_mode

    def model_for(self, want: Literal["general", "efficient"]) -> ModelSpec:
        for model in self.models:
            if model.tier == want:
                return model
        return self.models[0]

    def allows_model(self, model_id: str) -> bool:
        return any(m.model_id == model_id for m in self.models)


class ProviderRegistry:
    """Immutable after construction: no register/replace/enable method exists."""

    __slots__ = ("_providers",)

    _providers: Mapping[ProviderId, ProviderDescriptor]

    def __init__(self, descriptors: list[ProviderDescriptor]) -> None:
        built: dict[ProviderId, ProviderDescriptor] = {}
        for descriptor in descriptors:
            if not isinstance(descriptor.provider_id, ProviderId):
                raise RegistryError("unknown provider id")
            if descriptor.provider_id in built:
                raise RegistryError("duplicate provider id")
            if descriptor.enabled and descriptor.billing_mode is BillingMode.PAID_API:
                # Paid providers stay architecturally present but can never be
                # operational while PAID_FALLBACK = OFF.
                raise RegistryError("a paid API provider cannot be enabled")
            built[descriptor.provider_id] = descriptor
        object.__setattr__(self, "_providers", MappingProxyType(built))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("the provider registry is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("the provider registry is read-only")

    def get(self, provider_id: ProviderId) -> ProviderDescriptor:
        try:
            return self._providers[provider_id]
        except KeyError:
            raise RegistryError("unknown provider") from None

    def __iter__(self) -> Iterator[ProviderDescriptor]:
        for provider_id in _CANONICAL_ORDER:
            if provider_id in self._providers:
                yield self._providers[provider_id]

    def __len__(self) -> int:
        return len(self._providers)

    def __repr__(self) -> str:
        return f"ProviderRegistry(count={len(self._providers)})"


_TEXT_CAPS = frozenset(
    {
        Capability.TEXT,
        Capability.REASONING,
        Capability.CODING,
        Capability.MULTILINGUAL,
        Capability.PERSIAN,
        Capability.LONG_CONTEXT,
    }
)


def build_registry(
    *,
    gemini_configured: bool,
    claude_enabled: bool = True,
    deployment_mode: DeploymentMode = DeploymentMode.OWNER_LOCAL,
) -> ProviderRegistry:
    """Sam's trusted defaults. OpenAI and Grok are present but permanently
    disabled: enabling one needs a future, reviewed code change."""

    return ProviderRegistry(
        [
            ProviderDescriptor(
                provider_id=ProviderId.CLAUDE_SUBSCRIPTION,
                display_name="Claude (subscription)",
                # Anthropic bars third-party products from offering claude.ai
                # login: subscription use is an OWNER_LOCAL-only personal tool.
                enabled=claude_enabled
                and deployment_mode is DeploymentMode.OWNER_LOCAL,
                billing_mode=BillingMode.SUBSCRIPTION_INCLUDED,
                auth_mode=AuthMode.SUBSCRIPTION_LOGIN,
                capabilities=_TEXT_CAPS,
                models=(ModelSpec(CLAUDE_SUBSCRIPTION_MODEL),),
                external_processing=True,
                free_tier_data_use=False,
                fallback_eligible=True,
                data_use_note=(
                    "Uses your Claude subscription through the local Claude Code "
                    "app, not Anthropic API billing. Data handling follows your "
                    "Claude privacy settings, which Sam cannot read."
                ),
                timeout_seconds=120.0,
            ),
            ProviderDescriptor(
                provider_id=ProviderId.GEMINI_FREE,
                display_name="Gemini (Free Tier)",
                enabled=gemini_configured,
                billing_mode=BillingMode.FREE_TIER,
                auth_mode=AuthMode.API_KEY,
                capabilities=_TEXT_CAPS,
                models=(
                    ModelSpec(GEMINI_GENERAL_MODEL, "general"),
                    ModelSpec(GEMINI_EFFICIENT_MODEL, "efficient"),
                ),
                external_processing=True,
                free_tier_data_use=True,
                fallback_eligible=True,
                data_use_note=(
                    "External cloud provider (Google). On the Free Tier Google may "
                    "use submitted content to improve its products and human "
                    "reviewers may read it. Data leaves this device. Free Tier is "
                    "intended: the owner must attest the project is unbilled, and "
                    "Sam cannot independently verify Google Cloud billing state."
                ),
                requires_free_attestation=True,
                timeout_seconds=60.0,
            ),
            ProviderDescriptor(
                provider_id=ProviderId.OPENAI_API,
                display_name="OpenAI (API)",
                enabled=False,
                billing_mode=BillingMode.PAID_API,
                auth_mode=AuthMode.API_KEY,
                capabilities=frozenset({Capability.TEXT}),
                models=(ModelSpec("not_configured"),),
                external_processing=True,
                free_tier_data_use=False,
                fallback_eligible=False,
                data_use_note=(
                    "Disabled. The OpenAI API is separate paid billing; a ChatGPT "
                    "subscription does not include API access."
                ),
            ),
            ProviderDescriptor(
                provider_id=ProviderId.GROK_API,
                display_name="Grok (xAI API)",
                enabled=False,
                billing_mode=BillingMode.PAID_API,
                auth_mode=AuthMode.API_KEY,
                capabilities=frozenset({Capability.TEXT}),
                models=(ModelSpec("not_configured"),),
                external_processing=True,
                free_tier_data_use=False,
                fallback_eligible=False,
                data_use_note=(
                    "Disabled. The xAI API is separate prepaid billing; a Grok "
                    "subscription does not include API access."
                ),
            ),
        ]
    )


__all__ = [
    "CLAUDE_SUBSCRIPTION_MODEL",
    "GEMINI_EFFICIENT_MODEL",
    "GEMINI_GENERAL_MODEL",
    "ModelSpec",
    "ProviderDescriptor",
    "ProviderRegistry",
    "build_registry",
]
