"""Request/response shapes for the desktop bridge.

**Requests** carry only what a human can legitimately supply from the UI
(text, a file's bytes, a resource id, an approve/deny choice, a profile id).
Every request model forbids extra fields, so an attempt to send a
``principal``, ``user_id``, ``scope``, ``risk``, ``permission``, ``endpoint``,
``model``, ``reference_id`` or ``api_key`` is rejected (HTTP 422) rather than
ignored. **Responses** contain only safe, bounded, presentation-oriented data:
never a credential, an Authorization header, a provider body, a traceback, or
raw audio in a log-able field.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

# Bounds (kept in one place; all are checked again by the engines they feed).
MAX_CHAT_MESSAGE_CHARS = 100_000  # matches sam.agent.models.AgentRequest
MAX_TTS_INPUT_CHARS = 20_000  # the TTS gateway enforces its own tighter limit
MAX_NAME_CHARS = 200
MAX_QUERY_CHARS = 1_000
MAX_BASE64_CHARS = 28_000_000  # ~20 MB decoded (Knowledge's MAX_RESOURCE_SIZE)
MAX_AUDIO_BASE64_CHARS = 11_500_000  # ~8 MiB decoded (Voice MAX_AUDIO_BYTES)
MAX_ID_CHARS = 100
MAX_SNIPPET_CHARS = 1_200
MAX_ACTIVITY_ITEMS = 100
MAX_STEP_UP_ATTEMPTS = 3
MAX_REQUEST_BODY_BYTES = 30_000_000

Capability = Literal[
    "available", "configured", "not_configured", "foundation_ready", "unavailable"
]
OperationStatus = Literal[
    "ok",
    "confirmation_required",
    "denied",
    "rejected",
    "failed",
    "not_configured",
]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ------------------------------------------------------------------ requests


class ChatRequest(_Request):
    message: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_CHARS)


class KnowledgeQueryRequest(_Request):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    top_k: int = Field(default=8, ge=1, le=20)


class KnowledgeIngestRequest(_Request):
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    resource_type: Literal["pdf", "txt", "markdown", "json", "csv"]
    content_base64: str = Field(min_length=1, max_length=MAX_BASE64_CHARS)
    confirmation_id: str | None = Field(default=None, max_length=MAX_ID_CHARS)


class KnowledgeRemoveRequest(_Request):
    resource_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    confirmation_id: str | None = Field(default=None, max_length=MAX_ID_CHARS)


class MemorySearchRequest(_Request):
    text: str | None = Field(default=None, max_length=MAX_QUERY_CHARS)


class RevokeGrantRequest(_Request):
    grant_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)


class ConfirmationDecisionRequest(_Request):
    """A human's decision on ONE pending confirmation. It carries an id and a
    boolean — never a scope, risk, action, or an ALLOW."""

    confirmation_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    approved: bool
    # Step-up proof, only meaningful when approving a CRITICAL confirmation.
    # A user-typed secret checked by the backend; never logged or echoed.
    step_up: SecretStr | None = Field(default=None, max_length=256)


class VoiceUtteranceRequest(_Request):
    audio_base64: str = Field(min_length=1, max_length=MAX_AUDIO_BASE64_CHARS)
    confirmation_id: str | None = Field(default=None, max_length=MAX_ID_CHARS)


class SpeakRequest(_Request):
    """Read-aloud: text plus a *trusted profile id* the backend advertised.
    No endpoint, model, reference id, key, or timeout can be expressed."""

    text: str = Field(min_length=1, max_length=MAX_TTS_INPUT_CHARS)
    voice_profile: str = Field(min_length=1, max_length=64)
    confirmation_id: str | None = Field(default=None, max_length=MAX_ID_CHARS)


# ----------------------------------------------------------------- responses


class Challenge(BaseModel):
    """Safe description of a pending confirmation. Display-only: the UI can
    answer it (approve/deny) but cannot alter any field of it."""

    confirmation_id: str
    action: str
    resource: str
    scope: str
    risk: Literal["low", "medium", "high", "critical"]
    target: str | None = None
    reason: str | None = None
    expires_at: str


class OperationResult(BaseModel):
    status: OperationStatus
    reason_code: str | None = None
    message: str | None = None
    reference_id: str | None = None
    challenge: Challenge | None = None


class ChatResponse(OperationResult):
    reply: str | None = None


class HealthState(BaseModel):
    status: str
    service: str
    environment: str


class SpeechProfile(BaseModel):
    profile_id: str


class StatusResponse(BaseModel):
    backend: HealthState
    agent: Capability
    knowledge: Capability
    memory: Capability
    tools: Capability
    tool_count: int
    server_count: int
    voice_input: Capability
    speech_output: Capability
    speech_profiles: list[SpeechProfile]
    computer_control: Capability
    coding_agent: Capability
    conversation_history: Literal["session_local"] = "session_local"
    memory_storage: Literal["in_process"] = "in_process"
    principal_label: str


class SourceLocation(BaseModel):
    page_number: int | None = None
    section_title: str | None = None
    paragraph_index: int | None = None
    character_start: int | None = None
    character_end: int | None = None


class ResourceInfo(BaseModel):
    resource_id: str
    name: str
    resource_type: str
    size_bytes: int
    chunk_count: int
    created_at: str


class KnowledgeListResponse(OperationResult):
    resources: list[ResourceInfo] = []


class KnowledgeHit(BaseModel):
    resource_id: str
    resource_name: str
    resource_type: str
    chunk_id: str
    score: float
    snippet: str
    location: SourceLocation


class KnowledgeQueryResponse(OperationResult):
    hits: list[KnowledgeHit] = []


class KnowledgeIngestResponse(OperationResult):
    resource: ResourceInfo | None = None
    duplicate_of: str | None = None


class MemoryItem(BaseModel):
    memory_id: str
    memory_type: str
    content: str
    source: str
    confidence: str
    created_at: str
    tags: list[str] = []


class MemoryResponse(OperationResult):
    items: list[MemoryItem] = []
    working: list[MemoryItem] = []


class ToolInfo(BaseModel):
    tool_id: str
    server_id: str
    description: str
    permission_resource: str
    permission_action: str
    enabled: bool
    verification_required: bool
    credential_configured: bool


class ServerInfo(BaseModel):
    server_id: str
    display_name: str


class ToolsResponse(BaseModel):
    state: Capability
    servers: list[ServerInfo]
    tools: list[ToolInfo]


class GrantInfo(BaseModel):
    grant_id: str
    resource: str
    action: str
    scope: str
    status: Literal["active", "revoked"]
    expires_at: str | None = None
    requires_confirmation: bool
    origin: str | None = None


class PermissionsResponse(BaseModel):
    grants: list[GrantInfo]


class ActivityItem(BaseModel):
    timestamp: str
    kind: str
    label: str
    outcome: str
    risk: str | None = None


class ActivityResponse(BaseModel):
    scope: Literal["current_session"] = "current_session"
    items: list[ActivityItem]


class VoiceResponse(OperationResult):
    transcript: str | None = None
    forwarded_to_agent: bool = False
    reply: str | None = None


class SpeakResponse(OperationResult):
    audio_base64: str | None = None
    audio_format: str | None = None
    byte_length: int | None = None


class DecisionResponse(BaseModel):
    status: Literal["approved", "denied"]
    confirmation_id: str
