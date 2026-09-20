"""Voice operation -> ``PermissionRequest`` translation.

Builds, never decides. ``PermissionEngine.evaluate`` is the sole
authorization authority. The mapping is fixed and explicit (rows live in
``sam.permissions.policy``):

    START_SESSION      -> VOICE / CREATE   (LOW)
    PROCESS_UTTERANCE  -> VOICE / READ     (MEDIUM)
    END_SESSION        -> VOICE / UPDATE   (LOW)

Nothing here reads a transcript, an identity signal, a provider result, or
a confidence value. For an utterance the confirmation ``target`` embeds the
utterance id and a digest of the exact audio, so a confirmation approved for
one utterance can never be consumed for another.
"""

from __future__ import annotations

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
from sam.voice.errors import VoicePolicyError
from sam.voice.models import VoiceOperation

_OPERATION_TO_RESOURCE_ACTION: dict[
    VoiceOperation, tuple[PermissionResource, PermissionAction]
] = {
    VoiceOperation.START_SESSION: (PermissionResource.VOICE, PermissionAction.CREATE),
    VoiceOperation.PROCESS_UTTERANCE: (PermissionResource.VOICE, PermissionAction.READ),
    VoiceOperation.END_SESSION: (PermissionResource.VOICE, PermissionAction.UPDATE),
}

# Fail at import time if an operation is added without a mapping.
if set(_OPERATION_TO_RESOURCE_ACTION) != set(VoiceOperation):
    raise AssertionError("every VoiceOperation must have a resource/action mapping")


def build_permission_request(
    operation: VoiceOperation,
    principal: Principal,
    *,
    session_id: str | None = None,
    utterance_id: str | None = None,
    audio_digest: str | None = None,
    reason: str | None = None,
) -> PermissionRequest:
    resource, action = _OPERATION_TO_RESOURCE_ACTION[operation]
    if operation is VoiceOperation.START_SESSION:
        segments: tuple[str, ...] = ("session",)
        target = operation.value
        correlation = None
    else:
        if session_id is None:
            raise VoicePolicyError("a session id is required")
        segments = ("session", session_id)
        target = operation.value
        correlation = session_id
        if operation is VoiceOperation.PROCESS_UTTERANCE:
            if utterance_id is None or audio_digest is None:
                raise VoicePolicyError("an utterance id and audio digest are required")
            target = f"{operation.value} {utterance_id} #{audio_digest[:16]}"
            correlation = utterance_id
    try:
        return PermissionRequest(
            principal=principal,
            action=action,
            resource=resource,
            scope=PermissionScope(segments=segments),
            reason=reason,
            target=target,
            correlation_id=correlation,
        )
    except ValidationError:
        raise VoicePolicyError("permission request could not be constructed") from None


def risk_for(operation: VoiceOperation) -> RiskLevel:
    resource, action = _OPERATION_TO_RESOURCE_ACTION[operation]
    entry = classify(resource, action)
    return entry.risk if entry is not None else RiskLevel.CRITICAL


def resource_action_for(
    operation: VoiceOperation,
) -> tuple[PermissionResource, PermissionAction]:
    return _OPERATION_TO_RESOURCE_ACTION[operation]


__all__ = ["build_permission_request", "resource_action_for", "risk_for"]
