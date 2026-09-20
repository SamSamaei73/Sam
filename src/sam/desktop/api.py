"""The fixed, typed desktop routes (``/desktop/v1``).

There is deliberately no generic route: no "call any engine method", no URL
or path parameter that selects a target, no shell, no file-read. Each handler
composes one *existing* engine operation, with the principal taken from the
trusted runtime, and translates the result into a safe shape. Authorization
decisions come exclusively from the engines' own ``PermissionEngine`` calls;
nothing here computes, caches, or overrides an ALLOW.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from sam.agent.errors import AgentError
from sam.agent.models import AgentRequest
from sam.desktop.models import (
    MAX_ACTIVITY_ITEMS,
    MAX_SNIPPET_CHARS,
    ActivityItem,
    ActivityResponse,
    Challenge,
    ChatRequest,
    ChatResponse,
    ConfirmationDecisionRequest,
    DecisionResponse,
    GrantInfo,
    HealthState,
    KnowledgeHit,
    KnowledgeIngestRequest,
    KnowledgeIngestResponse,
    KnowledgeListResponse,
    KnowledgeQueryRequest,
    KnowledgeQueryResponse,
    KnowledgeRemoveRequest,
    MemoryItem,
    MemoryResponse,
    MemorySearchRequest,
    OperationResult,
    OperationStatus,
    PermissionsResponse,
    ResourceInfo,
    RevokeGrantRequest,
    ServerInfo,
    SourceLocation,
    SpeakRequest,
    SpeakResponse,
    SpeechProfile,
    StatusResponse,
    ToolInfo,
    ToolsResponse,
    VoiceResponse,
    VoiceUtteranceRequest,
)
from sam.desktop.runtime import KNOWLEDGE_COLLECTION, DesktopRuntime
from sam.desktop.security import bridge_runtime
from sam.knowledge.models import (
    GetResourceRequest as _Unused,  # noqa: F401  (kept out of the public surface)
)
from sam.knowledge.models import (
    IngestionStatus,
    IngestResourceRequest,
    KnowledgeResource,
    ListResourcesRequest,
    RemoveResourceRequest,
    ResourceSourceKind,
    ResourceType,
    RetrievalQuery,
    RetrieveRequest,
)
from sam.memory.models import Memory
from sam.memory.models import RetrievalQuery as MemoryQuery
from sam.permissions.errors import ConfirmationError, GrantNotFoundError
from sam.permissions.models import ConfirmationStatus, GrantStatus, RiskLevel
from sam.permissions.policy import classify
from sam.tts.agent_boundary import SpeechProposal
from sam.tts.models import TTSStatus
from sam.voice.models import (
    AudioFormat,
    AudioInput,
    VoiceProcessingRequest,
    VoiceProcessingStatus,
)

router = APIRouter(prefix="/desktop/v1", tags=["desktop"])
_runtime = Depends(bridge_runtime)

_AGENT_MESSAGES = {
    "invalid_request": "That message couldn't be processed.",
    "provider_authentication_failed": "Sam's language model isn't configured.",
    "provider_timeout": "The language model timed out.",
    "provider_unavailable": "The language model is unavailable.",
    "provider_rate_limited": "The language model rate limit was reached.",
    "provider_overloaded": "The language model is overloaded.",
    "provider_server_error": "The language model had a server error.",
    "provider_request_failed": "The language model request failed.",
    "malformed_provider_response": "The language model returned an invalid response.",
    "agent_execution_failed": "Sam couldn't complete that request.",
}
_MESSAGES = {
    "permission_denied": "Sam isn't permitted to do that.",
    "confirmation_required": "This needs your confirmation.",
    "confirmation_invalid": "That confirmation is no longer valid.",
    "secret_detected": "That looked like it contained a secret, so it was withheld.",
    "unsupported_resource_type": "That file type isn't supported.",
    "resource_too_large": "That file is too large.",
    "parsing_error": "That file couldn't be read.",
    "extraction_error": "No readable text was found in that file.",
    "duplicate_resource": "That file is already in Knowledge.",
    "resource_not_found": "That item no longer exists.",
    "invalid_encoding": "The file data was malformed.",
    "not_configured": "That capability isn't configured.",
    "transcription_error": "Speech recognition failed.",
    "transcription_timeout": "Speech recognition timed out.",
    "invalid_transcript": "Speech recognition returned an unusable result.",
    "empty_transcript": "No speech was recognised.",
    "malformed_audio": "The recording couldn't be read.",
    "unsupported_audio": "That audio format isn't supported.",
    "audio_too_large": "The recording is too long.",
    "audio_duration": "The recording is too long.",
    "session_limit": "The voice session is full; try again.",
    "text_invalid": "That text can't be spoken.",
    "text_too_large": "That text is too long to read aloud.",
    "unknown_profile": "That voice isn't available.",
    "profile_disabled": "That voice isn't available.",
    "authentication_error": "The voice service rejected Sam's credentials.",
    "rate_limited": "The voice service is rate limited.",
    "timeout": "The voice service timed out.",
    "provider_error": "The voice service had a problem.",
    "invalid_audio": "The voice service returned unusable audio.",
    "output_too_large": "The generated audio was too large.",
    "agent_error": "Sam couldn't reply to that.",
}
_DEFAULT_MESSAGE = "The request couldn't be completed."


def _message(code: str | None) -> str | None:
    return None if code is None else _MESSAGES.get(code, _DEFAULT_MESSAGE)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _challenge(
    runtime: DesktopRuntime, confirmation_id: str | None
) -> Challenge | None:
    """Describe a pending confirmation from the *existing* confirmation
    record. Display-only; every field comes from the backend's record."""

    if confirmation_id is None:
        return None
    record = runtime.confirmations.get(confirmation_id)
    if record is None or record.principal != runtime.principal:
        return None
    return Challenge(
        confirmation_id=record.confirmation_id,
        action=record.action.value,
        resource=record.resource.value,
        scope=record.scope.as_text(),
        risk=record.risk.value,
        target=record.target,
        reason=record.reason,
        expires_at=record.expires_at.isoformat(),
    )


def _result(
    runtime: DesktopRuntime,
    *,
    permission: str | None,
    ok: bool,
    reason: str | None,
    confirmation_id: str | None,
    reference: str | None,
) -> OperationResult:
    """One translation for every engine's (permission outcome, success,
    category, confirmation) into the UI's five-state status."""

    if permission == "confirm_required":
        return OperationResult(
            status="confirmation_required",
            reason_code="confirmation_required",
            message=_message("confirmation_required"),
            reference_id=reference,
            challenge=_challenge(runtime, confirmation_id),
        )
    if permission == "deny":
        return OperationResult(
            status="denied",
            reason_code=reason or "permission_denied",
            message=_message(reason or "permission_denied"),
            reference_id=reference,
        )
    if ok:
        return OperationResult(status="ok", reference_id=reference)
    status: OperationStatus = "rejected" if reason in _REJECTION_REASONS else "failed"
    return OperationResult(
        status=status,
        reason_code=reason or "failed",
        message=_message(reason),
        reference_id=reference,
    )


_REJECTION_REASONS = frozenset(
    {
        "secret_detected",
        "unsupported_resource_type",
        "resource_too_large",
        "parsing_error",
        "extraction_error",
        "resource_not_found",
        "invalid_encoding",
        "duplicate_resource",
        "text_invalid",
        "text_too_large",
        "unknown_profile",
        "profile_disabled",
        "malformed_audio",
        "unsupported_audio",
        "audio_too_large",
        "audio_duration",
        "empty_transcript",
    }
)


def _resource_info(resource: KnowledgeResource) -> ResourceInfo:
    return ResourceInfo(
        resource_id=resource.resource_id,
        name=resource.name,
        resource_type=resource.resource_type.value,
        size_bytes=resource.size_bytes,
        chunk_count=resource.chunk_count,
        created_at=resource.created_at.isoformat(),
    )


def _memory_item(memory: Memory) -> MemoryItem:
    return MemoryItem(
        memory_id=memory.memory_id,
        memory_type=memory.memory_type.value,
        content=memory.content,
        source=memory.source.value,
        confidence=memory.confidence.value,
        created_at=memory.created_at.isoformat(),
        tags=list(memory.tags),
    )


def _decode(data: str) -> bytes | None:
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None


# ---------------------------------------------------------------- status


@router.get("/status", response_model=StatusResponse)
def status(runtime: DesktopRuntime = _runtime) -> StatusResponse:
    registry = runtime.mcp_registry
    tools = registry.list_tools()
    servers = registry.list_servers()
    return StatusResponse(
        backend=HealthState(
            status="ok",
            service=runtime.settings.app_name,
            environment=runtime.settings.app_env,
        ),
        agent="configured" if runtime.agent_configured else "not_configured",
        knowledge="available",
        memory="available",
        tools="configured" if tools else "foundation_ready",
        tool_count=len(tools),
        server_count=len(servers),
        voice_input="configured" if runtime.voice_boundary else "not_configured",
        speech_output="configured" if runtime.speech_boundary else "not_configured",
        speech_profiles=[
            SpeechProfile(profile_id=p) for p in runtime.speech_profile_ids
        ],
        computer_control="foundation_ready",
        coding_agent="foundation_ready",
        principal_label=runtime.principal.id,
    )


# ------------------------------------------------------------------ chat


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, runtime: DesktopRuntime = _runtime) -> ChatResponse:
    if not runtime.agent_configured:
        runtime.activity.add("chat", "Sam request", "not configured")
        return ChatResponse(
            status="not_configured",
            reason_code="not_configured",
            message="Sam's language model isn't configured.",
        )
    execution_id = uuid4().hex
    try:
        response = runtime.agent.execute(
            AgentRequest(message=payload.message), execution_id
        )
    except AgentError as error:
        runtime.activity.add("chat", "Sam request", "failed")
        return ChatResponse(
            status="failed",
            reason_code=error.error_code,
            message=_AGENT_MESSAGES.get(error.error_code, _DEFAULT_MESSAGE),
            reference_id=execution_id,
        )
    except Exception:
        runtime.activity.add("chat", "Sam request", "failed")
        return ChatResponse(
            status="failed",
            reason_code="agent_execution_failed",
            message=_DEFAULT_MESSAGE,
            reference_id=execution_id,
        )
    runtime.activity.add("chat", "Sam request", "completed")
    return ChatResponse(status="ok", reply=response.message, reference_id=execution_id)


# ------------------------------------------------------------- knowledge


@router.get("/knowledge/resources", response_model=KnowledgeListResponse)
def knowledge_list(runtime: DesktopRuntime = _runtime) -> KnowledgeListResponse:
    result = runtime.knowledge.list_resources(
        ListResourcesRequest(
            principal=runtime.principal, collection_id=KNOWLEDGE_COLLECTION
        )
    )
    base = _result(
        runtime,
        permission=result.permission_outcome.value,
        ok=result.outcome.value == "success",
        reason=result.error_category.value if result.error_category else None,
        confirmation_id=result.confirmation_id,
        reference=result.operation_id,
    )
    return KnowledgeListResponse(
        **base.model_dump(), resources=[_resource_info(r) for r in result.resources]
    )


@router.post("/knowledge/query", response_model=KnowledgeQueryResponse)
def knowledge_query(
    payload: KnowledgeQueryRequest, runtime: DesktopRuntime = _runtime
) -> KnowledgeQueryResponse:
    try:
        query = RetrievalQuery(
            query=payload.query, collection_id=KNOWLEDGE_COLLECTION, top_k=payload.top_k
        )
    except ValidationError:
        return KnowledgeQueryResponse(status="rejected", reason_code="text_invalid")
    result = runtime.knowledge.retrieve(
        RetrieveRequest(principal=runtime.principal, query=query)
    )
    runtime.activity.add(
        "knowledge", "Knowledge searched", result.permission_outcome.value
    )
    base = _result(
        runtime,
        permission=result.permission_outcome.value,
        ok=result.outcome.value == "success",
        reason=result.error_category.value if result.error_category else None,
        confirmation_id=result.confirmation_id,
        reference=result.operation_id,
    )
    hits = [
        KnowledgeHit(
            resource_id=item.resource.resource_id,
            resource_name=item.resource.name,
            resource_type=item.resource.resource_type.value,
            chunk_id=item.chunk.chunk_id,
            score=round(float(item.score), 4),
            snippet=item.chunk.text[:MAX_SNIPPET_CHARS],
            # Provenance is copied field-for-field from the backend location;
            # an absent field stays absent (never inferred or defaulted).
            location=SourceLocation(
                page_number=item.location.page_number,
                section_title=item.location.section_title,
                paragraph_index=item.location.paragraph_index,
                character_start=item.location.character_start,
                character_end=item.location.character_end,
            ),
        )
        for item in result.results
    ]
    return KnowledgeQueryResponse(**base.model_dump(), hits=hits)


@router.post("/knowledge/ingest", response_model=KnowledgeIngestResponse)
def knowledge_ingest(
    payload: KnowledgeIngestRequest, runtime: DesktopRuntime = _runtime
) -> KnowledgeIngestResponse:
    content = _decode(payload.content_base64)
    if content is None or not content:
        return KnowledgeIngestResponse(
            status="rejected",
            reason_code="invalid_encoding",
            message=_message("invalid_encoding"),
        )
    try:
        request = IngestResourceRequest(
            principal=runtime.principal,
            collection_id=KNOWLEDGE_COLLECTION,
            name=payload.name,
            declared_resource_type=ResourceType(payload.resource_type),
            source_kind=ResourceSourceKind.UPLOAD,
            source_label=payload.name,
            content=content,
        )
    except ValidationError:
        return KnowledgeIngestResponse(
            status="rejected",
            reason_code="resource_too_large",
            message=_message("resource_too_large"),
        )
    result = runtime.knowledge.ingest(request, confirmation_id=payload.confirmation_id)
    runtime.activity.add("knowledge", "Knowledge ingest", result.status.value)
    permission = result.permission_outcome.value
    reason = result.error_category.value if result.error_category else None
    if result.status is IngestionStatus.DUPLICATE:
        reason = "duplicate_resource"
    base = _result(
        runtime,
        permission=permission,
        ok=result.status in (IngestionStatus.SUCCESS, IngestionStatus.DUPLICATE),
        reason=reason,
        confirmation_id=result.confirmation_id,
        reference=result.operation_id,
    )
    if result.status is IngestionStatus.DUPLICATE:
        base = base.model_copy(update={"message": _message("duplicate_resource")})
    return KnowledgeIngestResponse(
        **base.model_dump(),
        resource=_resource_info(result.resource) if result.resource else None,
        duplicate_of=result.duplicate_of,
    )


@router.post("/knowledge/remove", response_model=OperationResult)
def knowledge_remove(
    payload: KnowledgeRemoveRequest, runtime: DesktopRuntime = _runtime
) -> OperationResult:
    try:
        request = RemoveResourceRequest(
            principal=runtime.principal,
            collection_id=KNOWLEDGE_COLLECTION,
            resource_id=payload.resource_id,
        )
    except ValidationError:
        return OperationResult(status="rejected", reason_code="resource_not_found")
    result = runtime.knowledge.remove_resource(
        request, confirmation_id=payload.confirmation_id
    )
    runtime.activity.add(
        "knowledge", "Knowledge removal", result.permission_outcome.value
    )
    return _result(
        runtime,
        permission=result.permission_outcome.value,
        ok=result.removed,
        reason=result.error_category.value if result.error_category else None,
        confirmation_id=result.confirmation_id,
        reference=result.operation_id,
    )


# ---------------------------------------------------------------- memory


@router.post("/memory/search", response_model=MemoryResponse)
def memory_search(
    payload: MemorySearchRequest, runtime: DesktopRuntime = _runtime
) -> MemoryResponse:
    """Read-only. There is intentionally no write route: nothing the UI does
    (chat, documents, voice) becomes a memory automatically."""

    query = MemoryQuery(principal=runtime.principal, text=payload.text, limit=50)
    found = runtime.memory.retrieve(query)
    working = runtime.memory.list_working(principal=runtime.principal)
    return MemoryResponse(
        status="ok",
        items=[_memory_item(r.memory) for r in found.items],
        working=[
            MemoryItem(
                memory_id=w.key,
                memory_type="working",
                content=w.content,
                source="session",
                confidence="n/a",
                created_at=w.created_at.isoformat(),
            )
            for w in working
        ],
    )


# ----------------------------------------------------------------- tools


@router.get("/tools", response_model=ToolsResponse)
def tools(runtime: DesktopRuntime = _runtime) -> ToolsResponse:
    """Read-only view of the trusted MCP registry. There is no route that can
    register, enable, disable, or edit a tool, and no admin handle exists in
    this runtime."""

    registry = runtime.mcp_registry
    listed = registry.list_tools()
    return ToolsResponse(
        state="configured" if listed else "foundation_ready",
        servers=[
            ServerInfo(server_id=s.server_id, display_name=s.display_name)
            for s in registry.list_servers()
        ],
        tools=[
            ToolInfo(
                tool_id=str(t.tool_id),
                server_id=t.server_id,
                description=t.description,
                permission_resource=t.binding.resource.value,
                permission_action=t.binding.action.value,
                enabled=t.enabled,
                verification_required=t.verification_required,
                credential_configured=t.credential is not None,
            )
            for t in listed
        ],
    )


# ----------------------------------------------------------- permissions


@router.get("/permissions", response_model=PermissionsResponse)
def permissions(runtime: DesktopRuntime = _runtime) -> PermissionsResponse:
    grants = runtime.grants.list_grants(runtime.principal)
    infos = []
    for g in sorted(grants, key=lambda g: g.grant_id):
        entry = classify(g.resource, g.action)
        infos.append(
            GrantInfo(
                grant_id=g.grant_id,
                resource=g.resource.value,
                action=g.action.value,
                scope=g.scope.as_text(),
                status="active" if g.status is GrantStatus.ACTIVE else "revoked",
                expires_at=_iso(g.expires_at),
                requires_confirmation=g.always_require_confirmation
                or bool(entry and entry.requires_confirmation),
                origin=g.metadata.get("origin"),
            )
        )
    return PermissionsResponse(grants=infos)


@router.post("/permissions/revoke", response_model=OperationResult)
def revoke(
    payload: RevokeGrantRequest, runtime: DesktopRuntime = _runtime
) -> OperationResult:
    """Revocation only ever *removes* authority, so it is safe to expose.
    There is no create-grant route."""

    grant = runtime.grants.get_grant(payload.grant_id)
    if grant is None or grant.principal != runtime.principal:
        return OperationResult(
            status="rejected",
            reason_code="resource_not_found",
            message=_message("resource_not_found"),
        )
    try:
        runtime.grants.revoke_grant(payload.grant_id, now=runtime.clock())
    except GrantNotFoundError:
        return OperationResult(
            status="rejected",
            reason_code="resource_not_found",
            message=_message("resource_not_found"),
        )
    runtime.activity.add("permission", "Grant revoked", "revoked")
    return OperationResult(status="ok")


# -------------------------------------------------------------- activity


_KIND_LABELS = {
    ("knowledge", "read"): "Knowledge read",
    ("knowledge", "write"): "Knowledge ingest",
    ("knowledge", "delete"): "Knowledge removal",
    ("voice", "create"): "Voice session",
    ("voice", "read"): "Voice processed",
    ("voice", "update"): "Voice session",
    ("speech_synthesis", "send"): "Speech generation",
}
_OUTCOME_LABELS = {
    "allow": "allowed",
    "deny": "denied",
    "confirm_required": "confirmation requested",
}


@router.get("/activity", response_model=ActivityResponse)
def activity(runtime: DesktopRuntime = _runtime) -> ActivityResponse:
    """Current-session activity only: PermissionEngine audit events for the
    local principal plus the desktop's own content-free log. Nothing here can
    contain message text, prompts, credentials, or document content."""

    items: list[ActivityItem] = []
    for event in runtime.permission_audit.list_events():
        if event.principal != runtime.principal:
            continue
        label = _KIND_LABELS.get(
            (event.resource.value, event.action.value),
            f"{event.resource.value} {event.action.value}",
        )
        items.append(
            ActivityItem(
                timestamp=event.occurred_at.isoformat(),
                kind="permission",
                label=f"Permission checked: {label}",
                outcome=_OUTCOME_LABELS.get(event.outcome.value, event.outcome.value),
                risk=event.risk.value,
            )
        )
    for record in runtime.activity.items():
        items.append(
            ActivityItem(
                timestamp=record.timestamp.isoformat(),
                kind=record.kind,
                label=record.label,
                outcome=record.outcome,
            )
        )
    items.sort(key=lambda i: i.timestamp, reverse=True)
    return ActivityResponse(items=items[:MAX_ACTIVITY_ITEMS])


# --------------------------------------------------------- confirmations


@router.post("/confirmations/decide", response_model=DecisionResponse)
def decide(
    payload: ConfirmationDecisionRequest, runtime: DesktopRuntime = _runtime
) -> DecisionResponse:
    """Record a human's approve/deny for ONE pending confirmation.

    This only marks the *existing* confirmation record; it grants nothing.
    The operation must then be retried with the confirmation id, where the
    PermissionEngine re-checks the grant, the exact principal/action/resource/
    scope/target binding, expiry and one-time use.
    """

    record = runtime.confirmations.get(payload.confirmation_id)
    if (
        record is None
        or record.principal != runtime.principal
        or record.status is not ConfirmationStatus.PENDING
    ):
        raise HTTPException(status_code=404, detail={"code": "confirmation_not_found"})
    if payload.approved and record.risk is RiskLevel.CRITICAL:
        verdict = runtime.check_step_up(
            payload.confirmation_id,
            payload.step_up.get_secret_value() if payload.step_up else None,
        )
        if verdict != "ok":
            runtime.activity.add("permission", "Critical approval refused", verdict)
            if verdict == "locked":
                # Too many wrong attempts: the confirmation is denied for good.
                with contextlib.suppress(ConfirmationError):
                    runtime.confirmations.decide(
                        payload.confirmation_id, approved=False, now=runtime.clock()
                    )
            code = {
                "unavailable": "step_up_unavailable",
                "failed": "step_up_failed",
                "locked": "step_up_locked",
            }[verdict]
            raise HTTPException(status_code=403, detail={"code": code})
    try:
        runtime.confirmations.decide(
            payload.confirmation_id, approved=payload.approved, now=runtime.clock()
        )
    except ConfirmationError:
        raise HTTPException(
            status_code=409, detail={"code": "confirmation_not_pending"}
        ) from None
    runtime.activity.add(
        "permission",
        "Confirmation answered",
        "approved" if payload.approved else "denied",
    )
    return DecisionResponse(
        status="approved" if payload.approved else "denied",
        confirmation_id=payload.confirmation_id,
    )


# ----------------------------------------------------------------- voice


@router.post("/voice/utterance", response_model=VoiceResponse)
def voice_utterance(
    payload: VoiceUtteranceRequest, runtime: DesktopRuntime = _runtime
) -> VoiceResponse:
    """One explicit recording -> Phase 9 gateway -> (if the transcript is
    eligible) exactly one AgentCore call. Secret-looking transcripts are
    withheld by the Phase 9 gateway and are never forwarded."""

    if runtime.voice_boundary is None:
        return VoiceResponse(
            status="not_configured",
            reason_code="not_configured",
            message=_message("not_configured"),
        )
    audio = _decode(payload.audio_base64)
    if audio is None or not audio:
        return VoiceResponse(
            status="rejected",
            reason_code="malformed_audio",
            message=_message("malformed_audio"),
        )
    session_id = runtime.voice_session()
    if session_id is None:
        return VoiceResponse(
            status="denied",
            reason_code="permission_denied",
            message=_message("permission_denied"),
        )
    outcome = runtime.voice_boundary.handle_voice(
        VoiceProcessingRequest(
            principal=runtime.principal,
            session_id=session_id,
            audio=AudioInput(content=audio, declared_format=AudioFormat.WAV_PCM16),
        ),
        confirmation_id=payload.confirmation_id,
    )
    voice = outcome.voice
    if voice.error_category is not None and voice.error_category.value in (
        "session_error",
        "session_limit",
    ):
        runtime.reset_voice_session()
    runtime.activity.add("voice", "Voice processed", voice.status.value)
    permission = voice.permission_outcome.value if voice.permission_outcome else None
    reason = voice.error_category.value if voice.error_category else None
    if voice.status is VoiceProcessingStatus.SUCCEEDED:
        if outcome.agent_error:
            return VoiceResponse(
                status="failed",
                reason_code="agent_error",
                message=_message("agent_error"),
                transcript=voice.transcript,
                forwarded_to_agent=True,
            )
        return VoiceResponse(
            status="ok",
            transcript=voice.transcript,
            forwarded_to_agent=outcome.forwarded_to_agent,
            reply=outcome.agent_message,
        )
    base = _result(
        runtime,
        permission=permission,
        ok=False,
        reason=reason,
        confirmation_id=voice.confirmation_id,
        reference=voice.utterance_id,
    )
    return VoiceResponse(**base.model_dump())


# ------------------------------------------------------------------- tts


@router.post("/tts/speak", response_model=SpeakResponse)
def tts_speak(
    payload: SpeakRequest, runtime: DesktopRuntime = _runtime
) -> SpeakResponse:
    """One explicit read-aloud request. The UI supplies text and a profile id
    from the list ``/status`` advertised — nothing that selects an endpoint,
    model, voice reference, key, timeout, or scope."""

    if runtime.speech_boundary is None:
        return SpeakResponse(
            status="not_configured",
            reason_code="not_configured",
            message=_message("not_configured"),
        )
    result = runtime.speech_boundary.speak(
        SpeechProposal(text=payload.text, voice_profile=payload.voice_profile),
        principal=runtime.principal,
        confirmation_id=payload.confirmation_id,
    )
    runtime.activity.add("speech", "Speech generation", result.status.value)
    if result.status is TTSStatus.SUCCEEDED and result.audio is not None:
        return SpeakResponse(
            status="ok",
            reference_id=result.synthesis_id,
            audio_base64=base64.b64encode(result.audio.audio_bytes).decode("ascii"),
            audio_format=result.audio.format.value,
            byte_length=result.audio.byte_length,
        )
    permission = result.permission_outcome.value if result.permission_outcome else None
    reason = result.error_category.value if result.error_category else None
    base = _result(
        runtime,
        permission=permission,
        ok=False,
        reason=reason,
        confirmation_id=result.confirmation_id,
        reference=result.synthesis_id,
    )
    return SpeakResponse(**base.model_dump())


__all__: list[Any] = ["router"]
