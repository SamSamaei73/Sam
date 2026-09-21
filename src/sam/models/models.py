"""Provider-neutral vocabulary for routing. Nothing here is provider-supplied."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sam.agent.models import Message
from sam.core.deployment import DeploymentMode

MAX_REQUEST_CHARS = 200_000
MAX_RESULT_CHARS = 100_000
MAX_ATTEMPTS = 2  # providers tried per user request; never more


class ProviderId(StrEnum):
    """Stable internal ids. Requests, prompts and provider output can never
    introduce another one."""

    CLAUDE_SUBSCRIPTION = "claude_subscription"
    GEMINI_FREE = "gemini_free"
    OPENAI_API = "openai_api"
    GROK_API = "grok_api"


class BillingMode(StrEnum):
    SUBSCRIPTION_INCLUDED = "subscription_included"
    FREE_TIER = "free_tier"
    PAID_API = "paid_api"
    LOCAL = "local"


class GeminiFreeAttestation(StrEnum):
    """An API key does not prove its Google project is unbilled. Until the OWNER
    attests it (step-up authenticated, session-only), Gemini is unavailable.
    Sam cannot independently verify Google Cloud billing state."""

    UNKNOWN = "unknown"
    OWNER_ATTESTED_UNBILLED = "owner_attested_unbilled"


class AuthMode(StrEnum):
    SUBSCRIPTION_LOGIN = "subscription_login"
    API_KEY = "api_key"


class Capability(StrEnum):
    TEXT = "text"
    REASONING = "reasoning"
    CODING = "coding"
    PERSIAN = "persian"
    MULTILINGUAL = "multilingual"
    LONG_CONTEXT = "long_context"
    STRUCTURED_OUTPUT = "structured_output"


class TaskProfile(StrEnum):
    GENERAL = "general"
    DEEP_REASONING = "deep_reasoning"
    CODING = "coding"
    RESEARCH = "research"
    SUMMARIZATION = "summarization"
    PERSIAN = "persian"
    MULTILINGUAL = "multilingual"
    LONG_CONTEXT = "long_context"
    FAST_LOW_COST = "fast_low_cost"
    PRIVACY_SENSITIVE = "privacy_sensitive"


class PrivacyClass(IntEnum):
    """Ordered: a higher class is more restricted. Derived from trusted
    provenance or explicit owner marking, never inferred from topics."""

    PUBLIC = 0
    NORMAL = 1
    PERSONAL = 2
    PRIVATE = 3
    SECRET = 4


class ClaudeImprovementState(StrEnum):
    """What the OWNER reports about Claude's "help improve" setting. Sam never
    scrapes it and never assumes it."""

    UNKNOWN = "unknown"
    OWNER_REPORTS_DISABLED = "owner_reports_disabled"
    OWNER_REPORTS_ENABLED = "owner_reports_enabled"


class Availability(StrEnum):
    AVAILABLE = "available"
    RATE_LIMITED = "rate_limited"
    USAGE_LIMIT = "usage_limit"
    UNAUTHORIZED = "unauthorized"
    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"
    UNATTESTED = "unattested"
    UNAVAILABLE = "unavailable"


class FailureCategory(StrEnum):
    SUCCESS = "success"
    UNAVAILABLE = "unavailable"
    AUTH_FAILURE = "auth_failure"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    REFUSAL = "refusal"
    INVALID_RESPONSE = "invalid_response"
    POLICY_BLOCKED = "policy_blocked"
    COST_BLOCKED = "cost_blocked"


# A REFUSAL is a provider's own safety decision: it is never an availability
# failure and never a reason to try another provider.
FALLBACK_CATEGORIES = frozenset(
    {
        FailureCategory.UNAVAILABLE,
        FailureCategory.AUTH_FAILURE,
        FailureCategory.RATE_LIMIT,
        FailureCategory.TIMEOUT,
    }
)


class ReasonCode(StrEnum):
    PREFERRED_PROVIDER = "preferred_provider"
    OWNER_SELECTION = "owner_selection"
    PRIVACY_RESTRICTED = "privacy_restricted"
    PROVIDER_DISABLED = "provider_disabled"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    FREE_QUOTA_EXHAUSTED = "free_quota_exhausted"
    SUBSCRIPTION_LIMIT = "subscription_limit"
    CAPABILITY_MISMATCH = "capability_mismatch"
    SECRET_BLOCKED = "secret_blocked"
    REFUSAL_NO_FAILOVER = "refusal_no_failover"
    COST_BLOCKED = "cost_blocked"
    OWNER_BLOCKLIST = "owner_blocklist"
    FALLBACK_USED = "fallback_used"
    NO_ELIGIBLE_PROVIDER = "no_eligible_provider"
    FALLBACK_DISABLED = "fallback_disabled"
    FREE_TIER_UNATTESTED = "free_tier_unattested"


class UsageMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    output_tokens: int | None = Field(default=None, ge=0, le=10_000_000)


class ModelRequest(BaseModel):
    """What the router accepts. There is deliberately no field for an endpoint,
    credential, model, provider, cost class, permission or risk: those are
    Sam's trusted configuration, never request data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    messages: tuple[Message, ...] = Field(min_length=1, max_length=64, repr=False)
    language: Literal["fa", "en", "auto"] = "auto"
    capabilities: frozenset[Capability] = frozenset({Capability.TEXT})
    privacy_class: PrivacyClass = PrivacyClass.NORMAL
    task_profile: TaskProfile = TaskProfile.GENERAL
    max_output_tokens: int = Field(default=1024, ge=1, le=8192)

    @field_validator("messages")
    @classmethod
    def _bounded(cls, value: tuple[Message, ...]) -> tuple[Message, ...]:
        if sum(len(m.content) for m in value) > MAX_REQUEST_CHARS:
            raise ValueError("request is too large")
        return value

    @property
    def input_size(self) -> int:
        return sum(len(m.content) for m in self.messages)


class ProviderOutput(BaseModel):
    """What a provider adapter hands back. Untrusted until the router
    validates it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(repr=False)
    model_id: str
    usage: UsageMetadata | None = None


class ProviderResult(BaseModel):
    """The router's normalized result. No credentials, headers or raw bodies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    provider_id: ProviderId | None
    model_id: str | None
    status: FailureCategory
    text: str | None = Field(default=None, repr=False)
    usage: UsageMetadata | None = None
    latency_ms: int = Field(ge=0)
    route_reasons: tuple[ReasonCode, ...] = ()
    fallback_used: bool = False
    provider_policy_limited: bool = False
    diagnostic_code: str = Field(default="ok", pattern=r"^[a-z0-9_]{1,64}$")


class RouteCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: ProviderId
    model_id: str
    reasons: tuple[ReasonCode, ...] = ()


class RouteDecision(BaseModel):
    """A deterministic, explainable plan. It contains no prompt text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_profile: TaskProfile
    privacy_class: PrivacyClass
    candidates: tuple[RouteCandidate, ...] = ()
    excluded: tuple[tuple[ProviderId, ReasonCode], ...] = ()
    blocked: FailureCategory | None = None
    blocked_reason: ReasonCode | None = None


__all__ = [
    "FALLBACK_CATEGORIES",
    "MAX_ATTEMPTS",
    "MAX_REQUEST_CHARS",
    "MAX_RESULT_CHARS",
    "AuthMode",
    "Availability",
    "BillingMode",
    "Capability",
    "ClaudeImprovementState",
    "DeploymentMode",
    "FailureCategory",
    "GeminiFreeAttestation",
    "ModelRequest",
    "PrivacyClass",
    "ProviderId",
    "ProviderOutput",
    "ProviderResult",
    "ReasonCode",
    "RouteCandidate",
    "RouteDecision",
    "TaskProfile",
    "UsageMetadata",
]
