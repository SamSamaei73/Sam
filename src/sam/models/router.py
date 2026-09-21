"""The deterministic model router.

Order of evaluation (no model output participates in any step):

  validate -> SECRET -> owner blocklist -> capabilities -> registry ->
  drop disabled -> CostPolicy -> PrivacyPolicy -> owner preference ->
  availability -> choose -> execute -> normalize.

At most ``MAX_ATTEMPTS`` providers are tried per request. A fallback is a NEW
decision: cost, privacy, capability, owner settings and availability are all
re-checked. A refusal is never an availability failure and never triggers a
fallback. There is no retry of the same provider.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from sam.models.audit import RoutingAuditEvent, RoutingAuditSink, now
from sam.models.errors import ProviderFailure
from sam.models.health import HealthTracker
from sam.models.models import (
    FALLBACK_CATEGORIES,
    MAX_ATTEMPTS,
    MAX_RESULT_CHARS,
    Availability,
    BillingMode,
    Capability,
    FailureCategory,
    GeminiFreeAttestation,
    ModelRequest,
    PrivacyClass,
    ProviderId,
    ProviderOutput,
    ProviderResult,
    ReasonCode,
    RouteCandidate,
    RouteDecision,
    TaskProfile,
    UsageMetadata,
)
from sam.models.policies import (
    CostPolicy,
    OwnerPreferences,
    PersonalContentPolicy,
    PrivacyPolicy,
)
from sam.models.provider import ModelProvider
from sam.models.registry import ProviderRegistry

_EFFICIENT_PROFILES = frozenset({TaskProfile.FAST_LOW_COST, TaskProfile.SUMMARIZATION})
_CLAUDE_FIRST = (ProviderId.CLAUDE_SUBSCRIPTION, ProviderId.GEMINI_FREE)
_GEMINI_FIRST = (ProviderId.GEMINI_FREE, ProviderId.CLAUDE_SUBSCRIPTION)


@dataclass(frozen=True)
class RoutingPriorities:
    """Configurable preference order per task profile. It is an ordering only:
    eligibility is decided by the policies, never by this table."""

    order: Mapping[TaskProfile, tuple[ProviderId, ...]] = field(
        default_factory=lambda: {
            TaskProfile.SUMMARIZATION: _GEMINI_FIRST,
            TaskProfile.FAST_LOW_COST: _GEMINI_FIRST,
        }
    )
    default: tuple[ProviderId, ...] = _CLAUDE_FIRST

    def for_profile(self, profile: TaskProfile) -> tuple[ProviderId, ...]:
        return self.order.get(profile, self.default)


def _tier(profile: TaskProfile) -> Literal["general", "efficient"]:
    return "efficient" if profile in _EFFICIENT_PROFILES else "general"


class ModelRouter:
    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        providers: Mapping[ProviderId, ModelProvider],
        preferences: Callable[[], OwnerPreferences],
        content_policy: Callable[[], PersonalContentPolicy],
        audit: RoutingAuditSink,
        cost: CostPolicy | None = None,
        privacy: PrivacyPolicy | None = None,
        health: HealthTracker | None = None,
        priorities: RoutingPriorities | None = None,
    ) -> None:
        self._registry = registry
        self._providers = dict(providers)
        self._preferences = preferences
        self._content = content_policy
        self._audit = audit
        self._cost = cost or CostPolicy()
        self._privacy = privacy or PrivacyPolicy()
        self._health = health or HealthTracker()
        self._priorities = priorities or RoutingPriorities()
        for provider_id, adapter in self._providers.items():
            if adapter.provider_id is not provider_id:
                raise ValueError("provider adapter is bound to another id")

    # ----------------------------------------------------------- planning

    def classify(self, request: ModelRequest) -> PrivacyClass:
        detected = self._privacy.classify(
            (m.content for m in request.messages), request.privacy_class
        )
        return max(detected, request.privacy_class)

    def plan(self, request: ModelRequest) -> RouteDecision:
        privacy = self.classify(request)
        profile = request.task_profile
        if privacy is PrivacyClass.SECRET:
            return RouteDecision(
                task_profile=profile,
                privacy_class=privacy,
                blocked=FailureCategory.POLICY_BLOCKED,
                blocked_reason=ReasonCode.SECRET_BLOCKED,
            )
        if self._content().blocked_by_owner(m.content for m in request.messages):
            return RouteDecision(
                task_profile=profile,
                privacy_class=privacy,
                blocked=FailureCategory.POLICY_BLOCKED,
                blocked_reason=ReasonCode.OWNER_BLOCKLIST,
            )
        prefs = self._preferences()
        preferred = prefs.preferred_provider
        order = list(self._priorities.for_profile(profile))
        if preferred is not None:
            order = [preferred, *[p for p in order if p is not preferred]]
        for provider_id in ProviderId:  # anything not listed still gets evaluated
            if provider_id not in order:
                order.append(provider_id)
        candidates: list[RouteCandidate] = []
        excluded: list[tuple[ProviderId, ReasonCode]] = []
        for provider_id in order:
            reason = self._exclusion(provider_id, request, privacy, prefs)
            if reason is not None:
                excluded.append((provider_id, reason))
                continue
            descriptor = self._registry.get(provider_id)
            reasons = (
                (ReasonCode.OWNER_SELECTION,)
                if provider_id is preferred
                else (ReasonCode.PREFERRED_PROVIDER,)
            )
            if len(candidates) < MAX_ATTEMPTS:
                candidates.append(
                    RouteCandidate(
                        provider_id=provider_id,
                        model_id=descriptor.model_for(_tier(profile)).model_id,
                        reasons=reasons,
                    )
                )
        return RouteDecision(
            task_profile=profile,
            privacy_class=privacy,
            candidates=tuple(candidates),
            excluded=tuple(excluded),
        )

    def _exclusion(
        self,
        provider_id: ProviderId,
        request: ModelRequest,
        privacy: PrivacyClass,
        prefs: OwnerPreferences,
    ) -> ReasonCode | None:
        descriptor = self._registry.get(provider_id)
        if not descriptor.enabled:
            return ReasonCode.PROVIDER_DISABLED
        if not self._cost.allows(descriptor.billing_mode):
            return ReasonCode.COST_BLOCKED
        if (
            descriptor.requires_free_attestation
            and prefs.gemini_attestation
            is not GeminiFreeAttestation.OWNER_ATTESTED_UNBILLED
        ):
            # An API key does not prove the project is unbilled: no attestation,
            # no call, no network.
            return ReasonCode.FREE_TIER_UNATTESTED
        adapter = self._providers.get(provider_id)
        if adapter is None:
            return ReasonCode.PROVIDER_UNAVAILABLE
        if request.input_size > descriptor.max_input_chars or not (
            request.capabilities <= descriptor.capabilities
        ):
            return ReasonCode.CAPABILITY_MISMATCH
        privacy_reason = self._privacy.permits(privacy, descriptor, prefs)
        if privacy_reason is not None:
            return privacy_reason
        state = self._health.state(provider_id)
        if state is Availability.AVAILABLE:
            state = adapter.availability()
        if state is Availability.AVAILABLE:
            return None
        if state is Availability.USAGE_LIMIT:
            return ReasonCode.SUBSCRIPTION_LIMIT
        if state is Availability.RATE_LIMITED:
            return (
                ReasonCode.FREE_QUOTA_EXHAUSTED
                if descriptor.free_tier_data_use
                else ReasonCode.SUBSCRIPTION_LIMIT
            )
        if state is Availability.DISABLED:
            return ReasonCode.PROVIDER_DISABLED
        return ReasonCode.PROVIDER_UNAVAILABLE

    # ---------------------------------------------------------- execution

    def execute(self, request: ModelRequest) -> ProviderResult:
        decision = self.plan(request)
        if decision.blocked is not None:
            return self._finish_blocked(request, decision)
        if not decision.candidates:
            return self._finish_none(request, decision)
        prefs = self._preferences()
        last: ProviderResult | None = None
        for index, candidate in enumerate(decision.candidates[:MAX_ATTEMPTS]):
            if index > 0:
                assert last is not None
                if last.status not in FALLBACK_CATEGORIES:
                    break
                descriptor = self._registry.get(candidate.provider_id)
                if not prefs.allow_free_fallback or not descriptor.fallback_eligible:
                    break
                if (
                    self._exclusion(
                        candidate.provider_id,
                        request,
                        decision.privacy_class,
                        prefs,
                    )
                    is not None
                ):
                    break  # re-checked NOW: cost, privacy, capability, health
            last = self._attempt(request, decision, candidate, fallback=index > 0)
            if last.status is FailureCategory.SUCCESS:
                return last
        assert last is not None
        return last

    def _attempt(
        self,
        request: ModelRequest,
        decision: RouteDecision,
        candidate: RouteCandidate,
        *,
        fallback: bool,
    ) -> ProviderResult:
        descriptor = self._registry.get(candidate.provider_id)
        adapter = self._providers[candidate.provider_id]
        started = time.monotonic()
        text: str | None = None
        usage: UsageMetadata | None = None
        diagnostic = "ok"
        status = FailureCategory.SUCCESS
        try:
            output = adapter.complete(
                request,
                model_id=candidate.model_id,
                timeout_seconds=descriptor.timeout_seconds,
            )
            text, usage = self._validate(output, candidate.model_id)
        except ProviderFailure as failure:
            status, diagnostic = failure.category, failure.code
        except _InvalidOutput:
            status, diagnostic = FailureCategory.INVALID_RESPONSE, "invalid_response"
        except Exception:
            status, diagnostic = FailureCategory.UNAVAILABLE, "provider_error"
        latency = int((time.monotonic() - started) * 1000)
        if status is FailureCategory.SUCCESS:
            self._health.record_success(candidate.provider_id)
        else:
            self._health.record_failure(
                candidate.provider_id, status, usage_limit=diagnostic == "usage_limit"
            )
        reasons = list(candidate.reasons)
        if fallback:
            reasons.append(ReasonCode.FALLBACK_USED)
        if status is FailureCategory.REFUSAL:
            reasons.append(ReasonCode.REFUSAL_NO_FAILOVER)
        result = ProviderResult(
            request_id=request.request_id,
            provider_id=candidate.provider_id,
            model_id=candidate.model_id,
            status=status,
            text=text,
            usage=usage,
            latency_ms=latency,
            route_reasons=tuple(reasons),
            fallback_used=fallback,
            provider_policy_limited=status is FailureCategory.REFUSAL,
            diagnostic_code=diagnostic,
        )
        self._record(request, decision, result, descriptor.billing_mode)
        return result

    @staticmethod
    def _validate(output: object, model_id: str) -> tuple[str, UsageMetadata | None]:
        if not isinstance(output, ProviderOutput):
            raise _InvalidOutput
        text = output.text.strip()
        if not text or len(text) > MAX_RESULT_CHARS or output.model_id != model_id:
            raise _InvalidOutput
        return text, output.usage

    # ------------------------------------------------------ non-call results

    def _finish_blocked(
        self, request: ModelRequest, decision: RouteDecision
    ) -> ProviderResult:
        assert decision.blocked is not None and decision.blocked_reason is not None
        result = ProviderResult(
            request_id=request.request_id,
            provider_id=None,
            model_id=None,
            status=decision.blocked,
            latency_ms=0,
            route_reasons=(decision.blocked_reason,),
            diagnostic_code=decision.blocked_reason.value,
        )
        self._record(request, decision, result, None)
        return result

    def _finish_none(
        self, request: ModelRequest, decision: RouteDecision
    ) -> ProviderResult:
        reasons = {reason for _, reason in decision.excluded}
        if ReasonCode.PRIVACY_RESTRICTED in reasons:
            status = FailureCategory.POLICY_BLOCKED
        elif reasons and reasons <= {
            ReasonCode.COST_BLOCKED,
            ReasonCode.PROVIDER_DISABLED,
        }:
            status = FailureCategory.COST_BLOCKED
        else:
            status = FailureCategory.UNAVAILABLE
        result = ProviderResult(
            request_id=request.request_id,
            provider_id=None,
            model_id=None,
            status=status,
            latency_ms=0,
            route_reasons=(ReasonCode.NO_ELIGIBLE_PROVIDER, *sorted(reasons)),
            diagnostic_code=ReasonCode.NO_ELIGIBLE_PROVIDER.value,
        )
        self._record(request, decision, result, None)
        return result

    def _record(
        self,
        request: ModelRequest,
        decision: RouteDecision,
        result: ProviderResult,
        billing: BillingMode | None,
    ) -> None:
        try:
            self._audit.record(
                RoutingAuditEvent(
                    request_id=request.request_id,
                    occurred_at=now(),
                    task_profile=decision.task_profile,
                    privacy_class=decision.privacy_class,
                    provider_id=result.provider_id,
                    model_id=result.model_id,
                    cost_class=billing,
                    reason_codes=result.route_reasons,
                    fallback_used=result.fallback_used,
                    status=result.status,
                    latency_ms=result.latency_ms,
                    input_size=request.input_size,
                    output_size=len(result.text or ""),
                )
            )
        except Exception:  # auditing must never break a request or leak
            return

    # -------------------------------------------------------------- status

    def registry(self) -> ProviderRegistry:
        return self._registry

    def provider_state(self, provider_id: ProviderId) -> Availability:
        descriptor = self._registry.get(provider_id)
        if not descriptor.enabled:
            return Availability.DISABLED
        adapter = self._providers.get(provider_id)
        if adapter is None:
            return Availability.NOT_CONFIGURED
        if (
            descriptor.requires_free_attestation
            and self._preferences().gemini_attestation
            is not GeminiFreeAttestation.OWNER_ATTESTED_UNBILLED
        ):
            return Availability.UNATTESTED
        state = self._health.state(provider_id)
        return state if state is not Availability.AVAILABLE else adapter.availability()

    def provider_detail(self, provider_id: ProviderId) -> str | None:
        """A safe lowercase reason code for a provider that is not available
        (for example ``managed_policy_present``); never a provider message."""

        descriptor = self._registry.get(provider_id)
        adapter = self._providers.get(provider_id)
        if not descriptor.enabled:
            return getattr(adapter, "disabled_detail", None)
        if (
            descriptor.requires_free_attestation
            and self._preferences().gemini_attestation
            is not GeminiFreeAttestation.OWNER_ATTESTED_UNBILLED
        ):
            return "free_tier_unattested"
        detail = getattr(adapter, "detail", None)
        return detail() if callable(detail) else None

    def any_usable(self) -> bool:
        return any(
            self.provider_state(d.provider_id) is Availability.AVAILABLE
            for d in self._registry
            if self._cost.allows(d.billing_mode)
        )

    def capabilities_for(self, language: str) -> frozenset[Capability]:
        if language == "fa":
            return frozenset({Capability.TEXT, Capability.PERSIAN})
        return frozenset({Capability.TEXT})


class _InvalidOutput(Exception):
    pass


__all__ = ["ModelRouter", "RoutingPriorities"]
