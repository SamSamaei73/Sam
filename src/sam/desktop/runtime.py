"""The desktop composition root.

Builds the existing engines once, in-process, with **no new policy**:

* the local principal is a constant of the trusted backend (``local-user``);
  no request can carry or change it;
* a small, documented set of *bootstrap grants* is created here, by trusted
  code, for that principal (see ``bootstrap_grants``). They are ordinary
  ``PermissionGrant`` rows that the Permissions view shows and the user may
  revoke — the UI can never create one;
* MCP is composed as an empty registry whose **administrative** handle is
  discarded after construction; only the read-only reader is kept, so nothing
  reachable from this runtime can register, enable, or replace a tool;
* voice input and speech output are only "configured" if a real provider was
  injected/configured. No fake provider is ever silently used in production.

Nothing here writes to Memory or Knowledge on its own.
"""

from __future__ import annotations

import hmac
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from sam.agent.core import AgentCore
from sam.core.config import Settings
from sam.knowledge.audit import InMemoryKnowledgeAuditSink
from sam.knowledge.engine import KnowledgeEngine
from sam.knowledge.index import InMemoryLexicalIndex
from sam.knowledge.store import InMemoryKnowledgeStore
from sam.mcp.registry import MCPRegistryAdmin, MCPRegistryReader
from sam.memory.engine import MemoryEngine
from sam.memory.store import InMemoryMemoryStore
from sam.memory.working import InMemoryWorkingMemoryStore
from sam.permissions.audit import InMemoryAuditSink
from sam.permissions.confirmation import InMemoryConfirmationProvider
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    PermissionAction,
    PermissionGrant,
    PermissionResource,
    PermissionScope,
    Principal,
    PrincipalKind,
    utc_now,
)
from sam.permissions.store import InMemoryPermissionStore
from sam.tts.agent_boundary import TTSAgentBoundary
from sam.tts.audit import InMemoryTTSAuditSink
from sam.tts.credentials import TTSCredentialReference, credentials_from_settings
from sam.tts.fish_audio import FISH_ALLOWED_MODELS, FISH_PROVIDER_ID, FishAudioProvider
from sam.tts.gateway import TTSGateway
from sam.tts.models import TrustedVoiceProfile
from sam.tts.profiles import TrustedVoiceProfiles
from sam.tts.provider import SpeechSynthesisProvider
from sam.voice.agent_boundary import VoiceAgentBoundary
from sam.voice.audit import InMemoryVoiceAuditSink
from sam.voice.gateway import VoiceGateway
from sam.voice.models import StartSessionRequest
from sam.voice.transcription import TranscriptionProvider

LOCAL_PRINCIPAL = Principal(kind=PrincipalKind.USER, id="local-user")
KNOWLEDGE_COLLECTION = "default"
DEFAULT_SPEECH_PROFILE = "sam_default"
MAX_ACTIVITY_ENTRIES = 200
MAX_STEP_UP_ATTEMPTS = 3


@dataclass(frozen=True)
class ActivityRecord:
    timestamp: datetime
    kind: str
    label: str
    outcome: str


class ActivityLog:
    """A bounded, in-memory, content-free log of what the desktop did this
    session. It stores labels and outcomes only — never message text."""

    def __init__(self) -> None:
        self._items: deque[ActivityRecord] = deque(maxlen=MAX_ACTIVITY_ENTRIES)
        self._lock = RLock()

    def add(self, kind: str, label: str, outcome: str) -> None:
        with self._lock:
            self._items.append(ActivityRecord(utc_now(), kind, label, outcome))

    def items(self) -> tuple[ActivityRecord, ...]:
        with self._lock:
            return tuple(self._items)


class DesktopRuntime:
    """Holds the composed engines. Attributes are the *runtime* surface: note
    that no ``MCPRegistryAdmin`` and no credential provider is stored here."""

    def __init__(
        self,
        *,
        settings: Settings,
        agent: AgentCore,
        agent_configured: bool,
        mcp_registry: MCPRegistryReader,
        transcription_provider: TranscriptionProvider | None,
        speech_provider: SpeechSynthesisProvider | None,
        speech_profiles: TrustedVoiceProfiles | None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.settings = settings
        self.principal = LOCAL_PRINCIPAL
        self.agent = agent
        self.agent_configured = agent_configured
        self.mcp_registry = mcp_registry
        self.clock = clock

        self.grants = InMemoryPermissionStore()
        self.confirmations = InMemoryConfirmationProvider()
        self.permission_audit = InMemoryAuditSink()
        self.permissions = PermissionEngine(
            store=self.grants,
            confirmation_provider=self.confirmations,
            audit_sink=self.permission_audit,
            clock=clock,
        )

        self.knowledge = KnowledgeEngine(
            store=InMemoryKnowledgeStore(),
            index=InMemoryLexicalIndex(),
            permission_engine=self.permissions,
            audit_sink=InMemoryKnowledgeAuditSink(),
            clock=clock,
        )
        self.memory = MemoryEngine(
            store=InMemoryMemoryStore(),
            working_store=InMemoryWorkingMemoryStore(),
        )
        self.activity = ActivityLog()
        self._step_up_failures: dict[str, int] = {}
        self._step_up_lock = RLock()

        self.voice_boundary: VoiceAgentBoundary | None = None
        self.voice_gateway: VoiceGateway | None = None
        self._voice_session: str | None = None
        self._voice_lock = RLock()
        if transcription_provider is not None:
            self.voice_gateway = VoiceGateway(
                permission_engine=self.permissions,
                transcription_provider=transcription_provider,
                audit_sink=InMemoryVoiceAuditSink(),
                clock=clock,
            )
            self.voice_boundary = VoiceAgentBoundary(self.voice_gateway, agent)

        self.speech_profiles = speech_profiles
        self.speech_boundary: TTSAgentBoundary | None = None
        self.speech_provider_id: str | None = None
        if speech_provider is not None and speech_profiles is not None:
            gateway = TTSGateway(
                permission_engine=self.permissions,
                provider=speech_provider,
                profiles=speech_profiles,
                audit_sink=InMemoryTTSAuditSink(),
                clock=clock,
            )
            self.speech_boundary = TTSAgentBoundary(gateway)
            self.speech_provider_id = speech_provider.provider_id

        bootstrap_grants(self)

    # ------------------------------------------------------------ step-up

    def check_step_up(self, confirmation_id: str, supplied: str | None) -> str:
        """Verify the step-up secret for approving a CRITICAL confirmation.

        Returns ``"ok"``, ``"unavailable"`` (no secret configured: CRITICAL
        actions cannot be approved from the desktop), ``"failed"`` or
        ``"locked"`` (too many wrong attempts; the caller must deny).
        """

        secret = self.settings.desktop_step_up_secret
        if secret is None:
            return "unavailable"
        with self._step_up_lock:
            if self._step_up_failures.get(confirmation_id, 0) >= MAX_STEP_UP_ATTEMPTS:
                return "locked"
            expected = secret.get_secret_value().encode("utf-8")
            given = (supplied or "").encode("utf-8")
            if supplied and hmac.compare_digest(given, expected):
                self._step_up_failures.pop(confirmation_id, None)
                return "ok"
            count = self._step_up_failures.get(confirmation_id, 0) + 1
            self._step_up_failures[confirmation_id] = count
            return "locked" if count >= MAX_STEP_UP_ATTEMPTS else "failed"

    # -------------------------------------------------------------- voice

    def voice_session(self) -> str | None:
        """The single, server-managed voice session. Opened lazily through the
        gateway (so it is permission-checked), never by a request."""

        if self.voice_gateway is None:
            return None
        with self._voice_lock:
            if self._voice_session is None:
                result = self.voice_gateway.start_session(
                    StartSessionRequest(principal=self.principal)
                )
                self._voice_session = result.session_id
            return self._voice_session

    def reset_voice_session(self) -> None:
        with self._voice_lock:
            self._voice_session = None

    @property
    def speech_profile_ids(self) -> list[str]:
        if self.speech_boundary is None or self.speech_profiles is None:
            return []
        return [p.profile_id for p in self.speech_profiles.list_profiles() if p.enabled]


def bootstrap_grants(runtime: DesktopRuntime) -> None:
    """Create the trusted local defaults — the ONLY place grants are made.

    Scope is deliberately narrow: the single ``default`` Knowledge collection
    (delete still requires a confirmation by policy), the Phase 9 voice
    session operations (only if voice input is configured), and speech
    synthesis for exactly the configured profile(s). Nothing is created for
    MCP, computer control, coding, Memory writes, or any other resource, and
    nothing is wildcard/root. Each grant is tagged so the Permissions view can
    say where it came from.
    """

    now = runtime.clock()
    principal = runtime.principal
    seq = 0

    def grant(
        resource: PermissionResource,
        action: PermissionAction,
        scope: PermissionScope,
        *,
        always_confirm: bool = False,
    ) -> None:
        nonlocal seq
        seq += 1
        # Every trusted grant is recorded (content-free) so the Activity view
        # shows exactly what authority the desktop started with and why.
        runtime.activity.add(
            "permission",
            f"Bootstrap grant: {resource.value} {action.value} {scope.as_text()}",
            "created",
        )
        runtime.grants.create_grant(
            PermissionGrant(
                grant_id=f"bootstrap-{seq:02d}",
                principal=principal,
                resource=resource,
                action=action,
                scope=scope,
                always_require_confirmation=always_confirm,
                created_at=now,
                updated_at=now,
                metadata={"origin": "desktop_bootstrap"},
            )
        )

    coll = KNOWLEDGE_COLLECTION
    grant(
        PermissionResource.KNOWLEDGE,
        PermissionAction.WRITE,
        PermissionScope.identifier(f"{coll}:ingest"),
    )
    grant(
        PermissionResource.KNOWLEDGE,
        PermissionAction.READ,
        PermissionScope.identifier(f"{coll}:list"),
    )
    grant(
        PermissionResource.KNOWLEDGE,
        PermissionAction.READ,
        PermissionScope.identifier(f"{coll}:retrieve"),
    )
    grant(
        PermissionResource.KNOWLEDGE,
        PermissionAction.DELETE,
        PermissionScope.from_path(coll),
    )
    if runtime.voice_gateway is not None:
        for action in (
            PermissionAction.CREATE,
            PermissionAction.READ,
            PermissionAction.UPDATE,
        ):
            grant(
                PermissionResource.VOICE, action, PermissionScope.identifier("session")
            )
    if runtime.speech_boundary is not None and runtime.speech_profiles is not None:
        for profile in runtime.speech_profiles.list_profiles():
            if profile.enabled:
                # SEND transmits the user's text to an external provider, so
                # starting the app must not silently authorize it: every
                # synthesis asks the user first, whatever the policy table says.
                grant(
                    PermissionResource.SPEECH_SYNTHESIS,
                    PermissionAction.SEND,
                    PermissionScope(segments=(profile.provider_id, profile.profile_id)),
                    always_confirm=True,
                )


def fish_speech_from_settings(
    settings: Settings,
) -> tuple[SpeechSynthesisProvider, TrustedVoiceProfiles] | None:
    """Build the real Fish provider + a trusted profile — or ``None``.

    Only when *both* the key and a voice reference are configured, and the
    model is on Sam's allowlist. Nothing here reads the environment: the key
    was loaded by ``Settings`` at bootstrap."""

    reference = (settings.fish_audio_voice_reference or "").strip()
    if settings.fish_audio_api_key is None or not reference:
        return None
    if settings.fish_audio_model not in FISH_ALLOWED_MODELS:
        return None
    try:
        profile = TrustedVoiceProfile(
            profile_id=DEFAULT_SPEECH_PROFILE,
            provider_id=FISH_PROVIDER_ID,
            provider_voice_reference=reference,
            provider_model=settings.fish_audio_model,
        )
    except ValueError:
        return None
    ref = TTSCredentialReference(
        provider_id=FISH_PROVIDER_ID, credential_id="fish-main"
    )
    provider = FishAudioProvider(
        credentials=credentials_from_settings(settings, ref), credential_reference=ref
    )
    return provider, TrustedVoiceProfiles([profile])


def build_desktop_runtime(
    settings: Settings,
    agent: AgentCore,
    *,
    agent_configured: bool | None = None,
    transcription_provider: TranscriptionProvider | None = None,
    speech_provider: SpeechSynthesisProvider | None = None,
    speech_profiles: TrustedVoiceProfiles | None = None,
    mcp_admin: MCPRegistryAdmin | None = None,
) -> DesktopRuntime:
    """Compose the runtime from trusted configuration.

    ``mcp_admin`` exists so a trusted composition/test can pre-register tools;
    the runtime keeps only ``mcp_admin.reader()``. Production passes nothing,
    which yields an empty registry (no connectors are configured)."""

    if speech_provider is None and speech_profiles is None:
        configured = fish_speech_from_settings(settings)
        if configured is not None:
            speech_provider, speech_profiles = configured
    admin = mcp_admin or MCPRegistryAdmin()
    return DesktopRuntime(
        settings=settings,
        agent=agent,
        agent_configured=(
            agent_configured
            if agent_configured is not None
            else settings.anthropic_api_key is not None
        ),
        mcp_registry=admin.reader(),
        transcription_provider=transcription_provider,
        speech_provider=speech_provider,
        speech_profiles=speech_profiles,
    )


__all__ = [
    "DEFAULT_SPEECH_PROFILE",
    "KNOWLEDGE_COLLECTION",
    "LOCAL_PRINCIPAL",
    "ActivityLog",
    "DesktopRuntime",
    "bootstrap_grants",
    "build_desktop_runtime",
    "fish_speech_from_settings",
]
