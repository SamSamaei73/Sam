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


LanguageChoice = Literal["auto", "fa", "en"]


class ChatRequest(_Request):
    message: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_CHARS)
    # A presentation/instruction preference only; never authority.
    language: LanguageChoice = "auto"
    # The owner may mark a message more sensitive. It can only RAISE how
    # restricted the request is (never lower it), and SECRET is always detected
    # by Sam, not chosen here.
    privacy: Literal["normal", "personal", "private"] = "normal"


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
    language: LanguageChoice = "auto"


class EnrollBeginRequest(_Request):
    """Start (or restart) owner enrollment. ``step_up`` is the backend-verified
    Phase 11 authentication secret; a voice match can never stand in for it."""

    step_up: SecretStr = Field(min_length=1, max_length=256)
    re_enroll: bool = False


class EnrollSampleRequest(_Request):
    session_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    audio_base64: str = Field(min_length=1, max_length=MAX_AUDIO_BASE64_CHARS)


class EnrollSessionRequest(_Request):
    session_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)


class ProfileDeleteRequest(_Request):
    step_up: SecretStr = Field(min_length=1, max_length=256)


class GuestStartRequest(_Request):
    """Start Guest Mode: the owner speaks a fresh challenge (speaker match +
    challenge content), plus the step-up secret. Nothing here names a
    principal, an owner flag, or a capability."""

    challenge_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    audio_base64: str = Field(min_length=1, max_length=MAX_AUDIO_BASE64_CHARS)
    step_up: SecretStr = Field(min_length=1, max_length=256)
    minutes: int = Field(default=15, ge=1, le=30)


class SpeakRequest(_Request):
    """Read-aloud: text plus a *trusted profile id* the backend advertised.
    No endpoint, model, reference id, key, or timeout can be expressed."""

    text: str = Field(min_length=1, max_length=MAX_TTS_INPUT_CHARS)
    voice_profile: str = Field(min_length=1, max_length=64)
    # The language of the text. If given, the backend refuses a profile that
    # cannot speak it (text-only instead) and never falls back to another
    # profile or provider.
    language: Literal["fa", "en"] | None = None
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
    language: Literal["fa", "en"] | None = None
    direction: Literal["rtl", "ltr"] | None = None


class HealthState(BaseModel):
    status: str
    service: str
    environment: str


class SpeechProfile(BaseModel):
    profile_id: str
    # Languages this trusted voice can speak. A response in a language no
    # profile covers stays text-only (never silently sent to another provider).
    languages: list[Literal["fa", "en"]] = ["en"]


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
    voice_identity: Capability = "not_configured"
    persian_tts: Capability = "not_configured"
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
    # Safe, normalized speaker metadata: never a score, embedding, or template.
    speaker: Literal["owner", "guest"] | None = None
    speaker_result: str | None = None
    language: Literal["fa", "en"] | None = None
    direction: Literal["rtl", "ltr"] | None = None


class SpeakResponse(OperationResult):
    audio_base64: str | None = None
    audio_format: str | None = None
    byte_length: int | None = None


class DecisionResponse(BaseModel):
    status: Literal["approved", "denied"]
    confirmation_id: str


class GuestInfo(BaseModel):
    active: bool
    seconds_remaining: int = 0


class IdentityStatusResponse(BaseModel):
    """Everything the Settings screen may show about voice identity."""

    available: bool
    enrolled: bool | None = None  # None: the secure store could not be read
    mode: Literal["owner_only", "guest_mode"] = "owner_only"
    guest: GuestInfo = GuestInfo(active=False)
    last_verification: Literal["verified", "not_verified", "unknown"] | None = None
    speaker_model: Capability = "not_configured"
    local_stt: Capability = "not_configured"
    persian_tts: Capability = "not_configured"
    samples_needed: int = 3
    samples_max: int = 5


class EnrollBeginResponse(OperationResult):
    session_id: str | None = None
    samples_needed: int = 3


class EnrollProgressResponse(OperationResult):
    accepted: bool = False
    sample_count: int = 0
    samples_needed: int = 3


class ChallengeResponse(OperationResult):
    challenge_id: str | None = None
    text_en: str | None = None
    text_fa: str | None = None
    expires_in_seconds: int = 0


# ------------------------------------------------ AI providers (Phase 13)

ProviderIdLiteral = Literal[
    "claude_subscription", "gemini_free", "openai_api", "grok_api"
]
ProviderStateLiteral = Literal[
    "available",
    "rate_limited",
    "usage_limit",
    "unauthorized",
    "not_configured",
    "disabled",
    "unattested",
    "unavailable",
]
GeminiAttestationLiteral = Literal["unknown", "owner_attested_unbilled"]
ClaudeImprovementLiteral = Literal[
    "unknown", "owner_reports_disabled", "owner_reports_enabled"
]


class ProviderStatus(BaseModel):
    """Trusted, display-only provider state. No key, token, prefix, header or
    provider response body ever appears here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: ProviderIdLiteral
    display_name: str
    state: ProviderStateLiteral
    enabled: bool
    cost_class: Literal["subscription_included", "free_tier", "paid_api", "local"]
    external: bool
    free_tier_data_use: bool
    note: str
    # A safe lowercase reason code when not available (for example
    # ``managed_policy_present`` or ``free_tier_unattested``); never a message.
    detail: str | None = None
    models: list[str]


class ModelPreferencesInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preferred_provider: ProviderIdLiteral | None
    allow_free_fallback: bool
    personal_to_free_tier: bool
    private_to_free_tier: bool
    claude_improvement_state: ClaudeImprovementLiteral
    private_to_claude_when_improvement_enabled: bool
    gemini_attestation: GeminiAttestationLiteral


class ContentPolicyInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["permissive"] = "permissive"
    topic_blocklist: list[str]
    follow_user_tone: bool
    private_content_auto_memory: Literal[False] = False


class ModelsStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    providers: list[ProviderStatus] = []
    preferences: ModelPreferencesInfo | None = None
    content: ContentPolicyInfo | None = None
    routing_mode: Literal["auto"] = "auto"
    paid_fallback: Literal["off"] = "off"
    max_provider_attempts: int = 2


class ModelPreferencesRequest(_Request):
    """The owner's routing/privacy/content preferences. There is no field for a
    provider endpoint, credential, model, cost class, or billing switch.
    ``step_up`` is required only when the change LOOSENS privacy."""

    preferred_provider: ProviderIdLiteral | None = None
    allow_free_fallback: bool = True
    personal_to_free_tier: bool = False
    private_to_free_tier: bool = False
    claude_improvement_state: ClaudeImprovementLiteral = "unknown"
    private_to_claude_when_improvement_enabled: bool = False
    # Loosening: the owner attests the Google project is unbilled. Needs step-up;
    # session-only; Sam cannot independently verify Google Cloud billing state.
    gemini_attestation: GeminiAttestationLiteral = "unknown"
    topic_blocklist: list[str] = Field(default_factory=list, max_length=64)
    step_up: SecretStr | None = Field(default=None, min_length=1, max_length=256)
