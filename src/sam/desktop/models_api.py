"""AI provider status and the owner's routing/privacy/content preferences.

Routes here are owner-bound (refused while Guest Mode is active) and carry no
authority over providers: they cannot enable OpenAI/Grok, switch paid fallback
on, set a model/endpoint/credential, or grant anything. Loosening privacy needs
the backend-verified step-up secret; tightening does not.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException

from sam.desktop.models import (
    ContentPolicyInfo,
    ModelPreferencesInfo,
    ModelPreferencesRequest,
    ModelsStatusResponse,
    ProviderStatus,
)
from sam.desktop.runtime import DesktopRuntime
from sam.desktop.security import owner_bridge_runtime
from sam.models.models import ClaudeImprovementState, GeminiFreeAttestation, ProviderId
from sam.models.registry import ProviderDescriptor

router = APIRouter(prefix="/desktop/v1", tags=["desktop-models"])
_owner_runtime = Depends(owner_bridge_runtime)
STEP_UP_COOLDOWN = timedelta(minutes=5)


def _status(runtime: DesktopRuntime) -> ModelsStatusResponse:
    model_router, store = runtime.model_router, runtime.model_settings
    if model_router is None or store is None:
        return ModelsStatusResponse(available=False)
    providers = []
    for descriptor in model_router.registry():
        providers.append(
            _provider(
                model_router.provider_state(descriptor.provider_id),
                descriptor,
                model_router.provider_detail(descriptor.provider_id),
            )
        )
    prefs, content = store.preferences(), store.content()
    return ModelsStatusResponse(
        available=True,
        providers=providers,
        preferences=ModelPreferencesInfo(
            preferred_provider=(
                prefs.preferred_provider.value if prefs.preferred_provider else None
            ),
            allow_free_fallback=prefs.allow_free_fallback,
            personal_to_free_tier=prefs.personal_to_free_tier,
            private_to_free_tier=prefs.private_to_free_tier,
            claude_improvement_state=prefs.claude_improvement_state.value,
            private_to_claude_when_improvement_enabled=(
                prefs.private_to_claude_when_improvement_enabled
            ),
            gemini_attestation=prefs.gemini_attestation.value,
        ),
        content=ContentPolicyInfo(
            topic_blocklist=list(content.topic_blocklist),
            follow_user_tone=content.follow_user_tone,
        ),
    )


def _provider(
    state: object, descriptor: ProviderDescriptor, detail: str | None
) -> ProviderStatus:
    return ProviderStatus(
        provider_id=descriptor.provider_id.value,
        display_name=descriptor.display_name,
        state=getattr(state, "value", "unavailable"),
        enabled=descriptor.enabled,
        cost_class=descriptor.cost_class.value,
        external=descriptor.external_processing,
        free_tier_data_use=descriptor.free_tier_data_use,
        note=descriptor.data_use_note,
        detail=detail,
        models=[m.model_id for m in descriptor.models if descriptor.enabled],
    )


@router.get("/models", response_model=ModelsStatusResponse)
def models_status(runtime: DesktopRuntime = _owner_runtime) -> ModelsStatusResponse:
    return _status(runtime)


@router.post("/models/preferences", response_model=ModelsStatusResponse)
def set_model_preferences(
    payload: ModelPreferencesRequest, runtime: DesktopRuntime = _owner_runtime
) -> ModelsStatusResponse:
    model_router, store = runtime.model_router, runtime.model_settings
    if model_router is None or store is None:
        return ModelsStatusResponse(available=False)
    current = store.preferences()
    loosens = (
        (payload.personal_to_free_tier and not current.personal_to_free_tier)
        or (payload.private_to_free_tier and not current.private_to_free_tier)
        or (
            payload.private_to_claude_when_improvement_enabled
            and not current.private_to_claude_when_improvement_enabled
        )
        or (
            payload.gemini_attestation == "owner_attested_unbilled"
            and current.gemini_attestation
            is not GeminiFreeAttestation.OWNER_ATTESTED_UNBILLED
        )
    )
    if loosens:
        supplied = payload.step_up.get_secret_value() if payload.step_up else None
        verdict = runtime.check_step_up(
            "models:privacy", supplied, cooldown=STEP_UP_COOLDOWN
        )
        if verdict != "ok":
            runtime.activity.add("permission", "Privacy change refused", verdict)
            code = {
                "unavailable": "step_up_unavailable",
                "failed": "step_up_failed",
                "locked": "step_up_locked",
            }[verdict]
            raise HTTPException(status_code=403, detail={"code": code})
    preferred = (
        ProviderId(payload.preferred_provider) if payload.preferred_provider else None
    )
    if preferred is not None and not model_router.registry().get(preferred).enabled:
        # A disabled provider can never be selected, not even as a preference.
        raise HTTPException(status_code=422, detail={"code": "provider_disabled"})
    try:
        store.update(
            preferred_provider=preferred,
            allow_free_fallback=payload.allow_free_fallback,
            personal_to_free_tier=payload.personal_to_free_tier,
            private_to_free_tier=payload.private_to_free_tier,
            claude_improvement_state=ClaudeImprovementState(
                payload.claude_improvement_state
            ),
            private_to_claude_when_improvement_enabled=(
                payload.private_to_claude_when_improvement_enabled
            ),
            gemini_attestation=GeminiFreeAttestation(payload.gemini_attestation),
            topic_blocklist=tuple(payload.topic_blocklist),
        )
    except ValueError:
        raise HTTPException(
            status_code=422, detail={"code": "invalid_preferences"}
        ) from None
    runtime.activity.add("settings", "Model preferences changed", "updated")
    return _status(runtime)


__all__ = ["router"]
