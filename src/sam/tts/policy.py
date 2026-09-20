"""Synthesis request -> ``PermissionRequest`` translation. Builds, never
decides: ``PermissionEngine.evaluate`` is the sole authority.

Mapping (row lives in ``sam.permissions.policy``):

    synthesize  ->  SPEECH_SYNTHESIS / SEND   (MEDIUM, no mandatory confirmation)

``SPEECH_SYNTHESIS`` is a *different resource* from Phase 9's ``VOICE``, so a
local voice-session grant can never authorize sending text to an external
provider. The scope is ``(provider_id, profile_id)`` — taken from Sam's
trusted profile, never from text, the LLM, or the provider — so a grant for
one profile or provider does not cover another. The confirmation ``target``
embeds a digest of the exact text, so a confirmation approved for one text
cannot be consumed for another. Nothing here reads a provider response.
"""

from __future__ import annotations

import hashlib

from pydantic import ValidationError

from sam.permissions.models import (
    PermissionAction,
    PermissionRequest,
    PermissionResource,
    PermissionScope,
    Principal,
    RiskLevel,
)
from sam.permissions.policy import classify
from sam.tts.errors import TTSError
from sam.tts.models import TrustedVoiceProfile, TTSErrorCategory

RESOURCE = PermissionResource.SPEECH_SYNTHESIS
ACTION = PermissionAction.SEND


class TTSPolicyError(TTSError):
    category = TTSErrorCategory.POLICY_ERROR


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_permission_request(
    principal: Principal,
    profile: TrustedVoiceProfile,
    *,
    request_id: str,
    text: str,
) -> PermissionRequest:
    try:
        return PermissionRequest(
            principal=principal,
            action=ACTION,
            resource=RESOURCE,
            scope=PermissionScope(segments=(profile.provider_id, profile.profile_id)),
            reason=None,
            target=f"synthesize #{text_digest(text)[:16]}",
            correlation_id=request_id,
        )
    except ValidationError:
        raise TTSPolicyError("permission request could not be constructed") from None


def risk_for() -> RiskLevel:
    entry = classify(RESOURCE, ACTION)
    return entry.risk if entry is not None else RiskLevel.CRITICAL


__all__ = [
    "ACTION",
    "RESOURCE",
    "TTSPolicyError",
    "build_permission_request",
    "risk_for",
    "text_digest",
]
