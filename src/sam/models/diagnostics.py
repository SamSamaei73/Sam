"""Allowlisted provider blockers. Never expose CLI output or exception text."""

from __future__ import annotations

from dataclasses import dataclass

from sam.models.models import ProviderId
from sam.models.router import ModelRouter


@dataclass(frozen=True)
class ModelBlocker:
    provider: str | None
    reason_code: str
    message: str
    resolution: str


_CLAUDE = {
    "not_installed": (
        "Claude is not installed",
        "Install Claude Code in a trusted local location, sign in with your "
        "subscription, then retry providers.",
    ),
    "not_logged_in": (
        "Claude needs authentication",
        "Open Claude Code on this Mac, run /login with your Claude subscription, "
        "then retry providers.",
    ),
    "not_authenticated": (
        "Claude needs authentication",
        "Open Claude Code on this Mac, run /login with your Claude subscription, "
        "then retry providers.",
    ),
    "auth_status_unreadable": (
        "Claude authentication could not be checked",
        "Check Claude Code's local sign-in status, then retry providers.",
    ),
    "cli_unsupported": (
        "Claude needs an update",
        "Update the trusted Claude Code installation to support Sam's required "
        "isolation options, then retry providers.",
    ),
    "no_home": (
        "Claude's local environment is incomplete",
        "Relaunch the installed Sam app with your Mac login account, "
        "then retry providers.",
    ),
    "invalid_home": (
        "Claude's local environment is invalid",
        "Relaunch the installed Sam app with your Mac login account, "
        "then retry providers.",
    ),
    "invalid_account": (
        "Claude's local account is invalid",
        "Relaunch the installed Sam app with your Mac login account, "
        "then retry providers.",
    ),
    "managed_policy_present": (
        "Claude is blocked by managed policy",
        "Sam cannot safely use this managed Claude Code installation. "
        "Review the managed configuration with its administrator.",
    ),
    "deployment_not_owner_local": (
        "Claude subscription is unavailable here",
        "This integration requires the owner's local deployment.",
    ),
    "provider_disabled": (
        "Claude subscription is disabled",
        "Enable Claude subscription in trusted local startup configuration, "
        "then restart Sam.",
    ),
    "api_key_auth": (
        "Claude is not using subscription authentication",
        "Sign in to Claude Code with your subscription. "
        "Sam will not use Anthropic API billing.",
    ),
    "token_auth": (
        "Claude is not using subscription authentication",
        "Sign in to Claude Code with your subscription. "
        "Sam will not use a supplied token.",
    ),
    "third_party_auth": (
        "Claude is not using subscription authentication",
        "Sign in to Claude Code with your subscription. "
        "Sam will not use a cloud-provider login.",
    ),
    "unsupported_plan": (
        "Claude subscription could not be verified",
        "Check the Claude subscription supported by this local integration, "
        "then retry providers.",
    ),
    "ambiguous_auth": (
        "Claude subscription could not be verified",
        "Check Claude Code's local subscription sign-in, then retry providers.",
    ),
    "rate_limited": (
        "Claude is temporarily rate limited",
        "Wait for the provider limit to reset, then retry. Paid fallback remains off.",
    ),
    "usage_limit": (
        "Claude subscription usage limit reached",
        "Wait for the subscription limit to reset. Paid fallback remains off.",
    ),
}


def safe_provider_detail(detail: str | None) -> str | None:
    if detail is None:
        return None
    allowed = {
        *_CLAUDE,
        "free_tier_unattested",
        "not_configured",
        "probe_failed",
        "provider_unavailable",
    }
    return detail if detail in allowed else "provider_unavailable"


def model_blocker(router: ModelRouter | None) -> ModelBlocker:
    if router is None or not len(router.registry()):
        return ModelBlocker(
            None,
            "model_registry_empty",
            "No model provider is configured",
            "Review AI providers in Settings.",
        )
    descriptors = list(router.registry())
    descriptor = next((p for p in descriptors if p.enabled), descriptors[0])
    state = router.provider_state(descriptor.provider_id).value
    detail = router.provider_detail(descriptor.provider_id)
    code = detail or ("provider_disabled" if not descriptor.enabled else state)
    if descriptor.provider_id is ProviderId.CLAUDE_SUBSCRIPTION:
        message, resolution = _CLAUDE.get(
            code,
            (
                "Claude provider could not start",
                "Retry providers. If it still fails, check the trusted local "
                "Claude Code installation.",
            ),
        )
        return ModelBlocker(
            descriptor.provider_id.value,
            code if code in _CLAUDE else "claude_preflight_failed",
            message,
            resolution,
        )
    if (
        descriptor.provider_id is ProviderId.GEMINI_FREE
        and code == "free_tier_unattested"
    ):
        return ModelBlocker(
            descriptor.provider_id.value,
            code,
            "Gemini Free needs owner attestation",
            "Review the existing free-tier and privacy settings. "
            "Sam cannot verify billing and will not enable it.",
        )
    return ModelBlocker(
        descriptor.provider_id.value,
        "provider_unavailable",
        "No configured model provider is ready",
        "Review the provider status in Settings, then retry providers. "
        "Paid fallback remains off.",
    )
