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
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import Literal

from sam.agent.core import AgentCore
from sam.career.attempts import AttemptStore, ExternalAction
from sam.career.audit import InMemoryCareerAuditSink
from sam.career.readiness import ExternalActionReadiness
from sam.career.repository import CareerRepository, InMemoryCareerRepository
from sam.career.service import CareerService
from sam.core.config import Settings
from sam.desktop.career_ports import KnowledgePapers, ProfessionalEvidence
from sam.desktop.identity import DesktopVoiceIdentity
from sam.knowledge.audit import InMemoryKnowledgeAuditSink
from sam.knowledge.engine import KnowledgeEngine
from sam.knowledge.index import InMemoryLexicalIndex
from sam.knowledge.store import InMemoryKnowledgeStore, KnowledgeStore
from sam.language.hint import HintedTranscriptionProvider
from sam.language.policy import LanguagePolicy, LanguagePreference
from sam.mcp.registry import MCPRegistryAdmin, MCPRegistryReader
from sam.memory.engine import MemoryEngine
from sam.memory.store import InMemoryMemoryStore, MemoryStore
from sam.memory.working import InMemoryWorkingMemoryStore
from sam.models.factory import ModelSettingsStore
from sam.models.models import PrivacyClass
from sam.models.router import ModelRouter
from sam.permissions.audit import InMemoryAuditSink
from sam.permissions.confirmation import InMemoryConfirmationProvider
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    GrantStatus,
    PermissionAction,
    PermissionGrant,
    PermissionResource,
    PermissionScope,
    Principal,
    PrincipalKind,
    utc_now,
)
from sam.permissions.store import InMemoryPermissionStore
from sam.proactive.audit import InMemoryProactiveAuditSink
from sam.proactive.conditions import (
    ConditionEntry,
    ConditionRegistry,
    RequiredPermission,
)
from sam.proactive.observers import (
    CountObserver,
    deadline_entry,
    professional_conflicts_entry,
    provider_status_entry,
)
from sam.proactive.repository import (
    InMemoryProactiveRepository,
    ProactiveRepository,
)
from sam.proactive.service import ProactiveService
from sam.professional.audit import InMemoryProfessionalAuditSink
from sam.professional.candidates import RouterCandidateExtractor
from sam.professional.identity import OwnerProfessionalIdentity
from sam.professional.repository import (
    InMemoryProfessionalRepository,
    ProfessionalRepository,
)
from sam.professional.service import ProfessionalService
from sam.storage.audit import (
    DurableCareerAuditSink,
    DurablePermissionAuditSink,
)
from sam.storage.career import SQLiteCareerRepository
from sam.storage.knowledge import SQLiteKnowledgeStore, rebuild_index
from sam.storage.memory import SQLiteMemoryStore
from sam.storage.migrations import CURRENT_VERSION
from sam.storage.proactive import SQLiteProactiveRepository
from sam.storage.professional import SQLiteProfessionalRepository
from sam.storage.settings import InMemoryOwnerSettings, OwnerSettingsStore
from sam.system.secrets import CredentialReport
from sam.system.startup import DurableState, StartupPhase, StartupReport
from sam.tts.agent_boundary import TTSAgentBoundary
from sam.tts.audit import InMemoryTTSAuditSink
from sam.tts.credentials import (
    TTSCredentialReference,
    credentials_from_settings,
    gemini_credentials_from_settings,
)
from sam.tts.fish_audio import FISH_ALLOWED_MODELS, FISH_PROVIDER_ID, FishAudioProvider
from sam.tts.gateway import TTSGateway
from sam.tts.gemini_tts import GEMINI_PROVIDER_ID, GEMINI_TTS_MODEL, GeminiTTSProvider
from sam.tts.models import TrustedVoiceProfile, TTSAudioFormat
from sam.tts.profiles import TrustedVoiceProfiles
from sam.tts.provider import SpeechSynthesisProvider
from sam.voice.agent_boundary import VoiceAgentBoundary
from sam.voice.audit import InMemoryVoiceAuditSink
from sam.voice.gateway import VoiceGateway
from sam.voice.models import StartSessionRequest
from sam.voice.transcription import TranscriptionProvider
from sam.voice.wake import WakeRateLimiter
from sam.voice_identity.providers import SpeakerEmbeddingProvider
from sam.voice_identity.store import VoiceProfileStore

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
        model_router: ModelRouter | None = None,
        model_settings: ModelSettingsStore | None = None,
        mcp_registry: MCPRegistryReader,
        transcription_provider: TranscriptionProvider | None,
        speech_provider: SpeechSynthesisProvider | None,
        speech_profiles: TrustedVoiceProfiles | None,
        extra_speech_providers: Sequence[SpeechSynthesisProvider] = (),
        speaker_embedder: SpeakerEmbeddingProvider | None = None,
        profile_store: VoiceProfileStore | None = None,
        clock: Callable[[], datetime] = utc_now,
        durable: DurableState | None = None,
        startup: StartupReport | None = None,
        credentials: CredentialReport | None = None,
    ) -> None:
        self.settings = settings
        # Phase 17: durable state (None = in-memory development / tests) and
        # the startup report. A BLOCKED startup serves only its status.
        self.durable = durable
        self.startup = startup or StartupReport()
        self.credentials = credentials
        self.owner_settings: OwnerSettingsStore = (
            durable.owner_settings if durable is not None else InMemoryOwnerSettings()
        )
        self.principal = LOCAL_PRINCIPAL
        self.agent = agent
        self.agent_configured = agent_configured
        self.model_router = model_router
        self.model_settings = model_settings
        self.mcp_registry = mcp_registry
        self.clock = clock

        self.grants = InMemoryPermissionStore()
        self.confirmations = InMemoryConfirmationProvider()
        self.permission_audit = (
            DurablePermissionAuditSink(durable.audit)
            if durable is not None
            else InMemoryAuditSink()
        )
        self.permissions = PermissionEngine(
            store=self.grants,
            confirmation_provider=self.confirmations,
            audit_sink=self.permission_audit,
            clock=clock,
        )

        # Phase 17: durable repositories load here (REPOSITORY_INIT). A persisted
        # record that fails validation BLOCKS startup (fail closed); there is
        # never a silent fallback to empty state for durable data.
        repos = _repositories(durable, self.startup)
        knowledge_index = InMemoryLexicalIndex()
        if isinstance(repos.knowledge, SQLiteKnowledgeStore):
            rebuild_index(repos.knowledge, knowledge_index)
        self.knowledge = KnowledgeEngine(
            store=repos.knowledge,
            index=knowledge_index,
            permission_engine=self.permissions,
            audit_sink=InMemoryKnowledgeAuditSink(),
            clock=clock,
        )
        # Professional Intelligence (Phase 14): a SEPARATE domain from Memory and
        # Knowledge. In-memory only; model candidate extraction (optional, untrusted)
        # goes through the Phase 13 router, so its privacy and cost policy apply.
        self.professional_audit = InMemoryProfessionalAuditSink()
        self.professional = ProfessionalService(
            repository=repos.professional,
            permission_engine=self.permissions,
            audit_sink=self.professional_audit,
            candidate_extractor=(
                RouterCandidateExtractor(model_router)
                if model_router is not None
                else None
            ),
            owner_identity=_owner_identity(settings),
            clock=clock,
        )
        # Career & PhD Agent (Phase 16): review-first. Reads Professional evidence
        # and Knowledge papers through read-only ports; drafts stay local. No
        # submission adapter and no e-mail tool are configured, so nothing can be
        # submitted or sent from this runtime (fail closed).
        self.career_audit = (
            DurableCareerAuditSink(durable.audit)
            if durable is not None
            else InMemoryCareerAuditSink()
        )
        self.career = CareerService(
            permission_engine=self.permissions,
            evidence=ProfessionalEvidence(self.professional),
            papers=KnowledgePapers(self.knowledge, KNOWLEDGE_COLLECTION),
            repository=repos.career,
            audit_sink=self.career_audit,
            clock=clock,
            attempt_store=repos.attempts,
            readiness=self._career_readiness,
        )
        # Proactive Agent (Phase 15): owner-defined reminders, summaries and
        # condition watches. In-memory only. Every run is authorized by the
        # PermissionEngine at run time; summaries go through the Phase 13 router;
        # observers only READ, each under its own permission. The scheduler thread
        # is started by the application lifespan, never here.
        self.proactive_audit = InMemoryProactiveAuditSink()
        self.proactive = ProactiveService(
            permission_engine=self.permissions,
            repository=repos.proactive,
            registry=_proactive_registry(self),
            router=model_router,
            audit_sink=self.proactive_audit,
            clock=clock,
        )
        self.memory = MemoryEngine(
            store=repos.memory,
            working_store=InMemoryWorkingMemoryStore(),
        )
        self.activity = ActivityLog()
        self._step_up_failures: dict[str, tuple[int, datetime]] = {}
        self.language = LanguagePolicy()
        self._step_up_lock = RLock()

        self.voice_boundary: VoiceAgentBoundary | None = None
        self.voice_gateway: VoiceGateway | None = None
        # Hands-free wake word: ONLY a local recognizer (set by create_runtime
        # from the local voice stack, or by a trusted test composition). Never
        # a cloud STT provider. Room audio is bounded by the rate limiter.
        self.wake_transcriber: TranscriptionProvider | None = None
        self.wake_limiter = WakeRateLimiter()
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

        # Owner voice identity is composed only when an embedder, a secure
        # profile store AND a transcription provider are all configured.
        self.speech_profile_languages: dict[str, frozenset[str]] = {}
        self.identity: DesktopVoiceIdentity | None = None
        if (
            speaker_embedder is not None
            and profile_store is not None
            and transcription_provider is not None
        ):
            self.identity = DesktopVoiceIdentity(
                embedder=speaker_embedder,
                store=profile_store,
                transcriber=transcription_provider,
                agent=agent,
                threshold=settings.speaker_verification_threshold,
                language=self.language,
                clock=clock,
            )

        self.speech_profiles = speech_profiles
        self.speech_boundary: TTSAgentBoundary | None = None
        self.speech_provider_id: str | None = None
        if speech_provider is not None and speech_profiles is not None:
            gateway = TTSGateway(
                permission_engine=self.permissions,
                provider=speech_provider,
                extra_providers=extra_speech_providers,
                profiles=speech_profiles,
                audit_sink=InMemoryTTSAuditSink(),
                clock=clock,
            )
            self.speech_boundary = TTSAgentBoundary(gateway)
            self.speech_provider_id = speech_provider.provider_id
            for profile in speech_profiles.list_profiles():
                # Gemini is the Persian voice; Fish stays English-only. A
                # request in a language the profile lacks is text-only.
                self.speech_profile_languages[profile.profile_id] = (
                    frozenset({"fa"})
                    if profile.provider_id == GEMINI_PROVIDER_ID
                    else frozenset({"en"})
                )

        if durable is not None and not self.startup.blocked:
            # Crash recovery for Career items (their attempts were recovered at
            # the RECOVERY phase): nothing that was in flight becomes retryable.
            self.startup.recovery.update(self.career.recover_after_restart())
        self.startup.done(StartupPhase.REPOSITORY_INIT)
        bootstrap_grants(self)
        self.startup.done(StartupPhase.SECURITY_BOUNDARY_INIT)

    # ---------------------------------------------------------- phase 17

    @property
    def blocked_reason(self) -> str | None:
        return self.startup.reason_code if self.startup.blocked else None

    def _career_readiness(
        self, action: ExternalAction, adapter_configured: bool
    ) -> ExternalActionReadiness:
        """Real SUBMIT / SEND readiness, from facts only this runtime knows.
        Phase 17 configures no adapter, so this is never fully ready."""

        durable = self.durable is not None and not self.startup.blocked
        resource_action = (
            PermissionAction.SUBMIT
            if action is ExternalAction.SUBMIT
            else PermissionAction.SEND
        )
        granted = any(
            g.status is GrantStatus.ACTIVE
            for g in self.grants.list_grants(
                self.principal,
                resource=PermissionResource.CAREER,
                action=resource_action,
            )
        )
        return ExternalActionReadiness(
            durable_store_ready=durable and self.career.attempts.durable,
            migrations_ready=durable and self.startup.schema_version == CURRENT_VERSION,
            idempotency_ready=durable and self.career.attempts.durable,
            # a trusted adapter must also support read-only reconciliation;
            # none is configured in this build
            reconciliation_ready=False,
            destination_validation_ready=True,
            adapter_configured=adapter_configured,
            permission_ready=granted,
        )

    # ------------------------------------------------------------ step-up

    def check_step_up(
        self,
        key: str,
        supplied: str | None,
        *,
        cooldown: timedelta | None = None,
    ) -> str:
        """Verify the step-up secret (the Phase 11 authentication boundary).

        Returns ``"ok"``, ``"unavailable"`` (no secret configured: the action
        cannot be approved from the desktop), ``"failed"`` or ``"locked"``.
        A confirmation id is locked for good after too many wrong attempts;
        a reusable action key (``cooldown`` given) unlocks after the cooldown.
        """

        secret = self.settings.desktop_step_up_secret
        if secret is None:
            return "unavailable"
        now = self.clock()
        with self._step_up_lock:
            count, last = self._step_up_failures.get(key, (0, now))
            if count >= MAX_STEP_UP_ATTEMPTS:
                if cooldown is not None and now - last >= cooldown:
                    count = 0
                else:
                    return "locked"
            expected = secret.get_secret_value().encode("utf-8")
            given = (supplied or "").encode("utf-8")
            if supplied and hmac.compare_digest(given, expected):
                self._step_up_failures.pop(key, None)
                return "ok"
            count += 1
            self._step_up_failures[key] = (count, now)
            return "locked" if count >= MAX_STEP_UP_ATTEMPTS else "failed"

    def warm_wake_recognizer(self) -> None:
        """Load the local wake recognizer in the background (no download)."""

        preload = getattr(self.wake_transcriber, "preload", None)
        if callable(preload):
            threading.Thread(
                target=preload, name="sam-wake-warmup", daemon=True
            ).start()

    def voice_boundary_for(
        self, preference: LanguagePreference
    ) -> VoiceAgentBoundary | None:
        """A per-request owner voice boundary that applies the language policy."""

        if self.voice_gateway is None:
            return None

        def language_for(text: str, tag: str | None) -> Literal["fa", "en"]:
            decision = self.language.resolve(preference, text, stt_language=tag)
            return decision.response_language

        return VoiceAgentBoundary(
            self.voice_gateway, self.agent, language_for=language_for
        )

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


@dataclass(frozen=True)
class _Repositories:
    memory: MemoryStore
    knowledge: KnowledgeStore
    professional: ProfessionalRepository
    career: CareerRepository
    proactive: ProactiveRepository
    attempts: AttemptStore | None


def _repositories(
    durable: DurableState | None, startup: StartupReport
) -> _Repositories:
    """Durable repositories when storage is ready, in-memory otherwise.

    Short-term working memory, confirmations and step-up state are never
    among them: they are intentionally ephemeral."""

    if durable is not None and not startup.blocked:
        db = durable.db
        try:
            return _Repositories(
                memory=SQLiteMemoryStore(db),
                knowledge=SQLiteKnowledgeStore(db),
                professional=SQLiteProfessionalRepository(db),
                career=SQLiteCareerRepository(db),
                proactive=SQLiteProactiveRepository(db),
                attempts=durable.attempt_store,
            )
        except Exception:
            # Reason code only: never the record, the path or the error text.
            startup.fail(StartupPhase.REPOSITORY_INIT, "repository_load_failed")
    return _Repositories(
        memory=InMemoryMemoryStore(),
        knowledge=InMemoryKnowledgeStore(),
        professional=InMemoryProfessionalRepository(),
        career=InMemoryCareerRepository(),
        proactive=InMemoryProactiveRepository(),
        attempts=None,
    )


def _owner_identity(settings: Settings) -> OwnerProfessionalIdentity | None:
    """The owner's professional identity from TRUSTED local settings only.

    Invalid configuration (a one-word name or alias, too many aliases) fails
    CLOSED: no identity, so no author position is ever recorded, rather than a
    partial or loosened match."""

    try:
        return OwnerProfessionalIdentity.from_config(
            settings.owner_name, settings.owner_name_aliases
        )
    except ValueError:
        return None


def _proactive_registry(runtime: DesktopRuntime) -> ConditionRegistry:
    """The trusted conditions a watch may name. Built here, once, by Sam's code.

    Cross-domain signals are READ-only callables; the professional one goes
    through ``ProfessionalService`` (its own PROFESSIONAL/READ check) after the
    proactive runner has asked for that permission itself."""

    def open_conflicts(principal: Principal) -> int | None:
        result = runtime.professional.get_conflicts(principal)
        return len(result.data) if result.ok and result.data is not None else None

    def career_count(key: str) -> Callable[[Principal], int | None]:
        def count(principal: Principal) -> int | None:
            result = runtime.career.counts(principal)
            return result.data[key] if result.ok and result.data is not None else None

        return count

    def career_entry(condition_id: str, key: str, noun: str) -> ConditionEntry:
        # READ-only Career signals: a proactive run can notify, never submit/send.
        return ConditionEntry(
            condition_id=condition_id,
            label="career",
            permission=RequiredPermission(
                PermissionResource.CAREER,
                PermissionAction.READ,
                PermissionScope.from_path("career"),
            ),
            observer=CountObserver(career_count(key), noun),
            max_seconds=5.0,
            privacy_class=PrivacyClass.PRIVATE,
        )

    entries: list[ConditionEntry] = [
        deadline_entry(),
        professional_conflicts_entry(open_conflicts),
        career_entry("career_review_queue", "review", "career item(s)"),
        career_entry("career_deadlines", "deadlines", "application deadline(s)"),
        career_entry("career_follow_ups_due", "follow_ups", "follow-up(s)"),
    ]
    if runtime.model_router is not None:
        entries.append(provider_status_entry(runtime.model_router.provider_state))
    return ConditionRegistry.of(entries)


def bootstrap_identity(
    resource: PermissionResource, action: PermissionAction, scope: PermissionScope
) -> str:
    return f"{resource.value}:{action.value}:{scope.as_text()}"


def bootstrap_grants(runtime: DesktopRuntime) -> None:
    """Create the trusted local defaults — the ONLY place grants are made.

    Scope is deliberately narrow: the single ``default`` Knowledge collection
    (delete still requires a confirmation by policy), the Phase 9 voice
    session operations (only if voice input is configured), speech synthesis
    for exactly the configured profile(s), the owner's Professional profile and
    the owner's own proactive tasks (``proactive``; deleting a task still
    requires a confirmation by policy). Nothing is created for
    MCP, computer control, coding, Memory writes, or any other resource, and
    nothing is wildcard/root. Each grant is tagged so the Permissions view can
    say where it came from.
    """

    now = runtime.clock()
    principal = runtime.principal
    seq = 0
    revoked = runtime.owner_settings.revoked_bootstrap_grants()

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
        # An owner's revocation of a trusted default survives restarts: the
        # grant is shown, but revoked (only narrowing is ever persisted).
        if bootstrap_identity(resource, action, scope) in revoked:
            runtime.grants.revoke_grant(f"bootstrap-{seq:02d}", now=now)

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
    # Professional Intelligence: read, ingest and review are pre-authorized for the
    # local owner; removal is HIGH and always asks for a confirmation by policy.
    grant(
        PermissionResource.PROFESSIONAL,
        PermissionAction.READ,
        PermissionScope.identifier("profile:read"),
    )
    grant(
        PermissionResource.PROFESSIONAL,
        PermissionAction.WRITE,
        PermissionScope.identifier("profile:ingest"),
    )
    grant(
        PermissionResource.PROFESSIONAL,
        PermissionAction.UPDATE,
        PermissionScope.identifier("profile:review"),
    )
    grant(
        PermissionResource.PROFESSIONAL,
        PermissionAction.DELETE,
        PermissionScope.from_path("profile"),
    )
    # Proactive Agent: the owner may manage and run their own automations. Each
    # scheduled run is still evaluated against these grants at run time (revoking
    # EXECUTE stops them), and deleting a task always asks for a confirmation.
    for action in (
        PermissionAction.READ,
        PermissionAction.CREATE,
        PermissionAction.UPDATE,
        PermissionAction.EXECUTE,
        PermissionAction.DELETE,
    ):
        grant(
            PermissionResource.PROACTIVE,
            action,
            PermissionScope.from_path("proactive"),
        )
    # Career & PhD Agent: local, review-first work only. SUBMIT and SEND are
    # deliberately NOT bootstrapped (and no adapter exists to use them); withdrawal
    # (DELETE) is HIGH and always asks for a confirmation.
    for action in (
        PermissionAction.READ,
        PermissionAction.CREATE,
        PermissionAction.UPDATE,
        PermissionAction.DELETE,
    ):
        grant(
            PermissionResource.CAREER,
            action,
            PermissionScope.from_path("career"),
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


PERSIAN_SPEECH_PROFILE = "sam_persian"


def gemini_speech_from_settings(
    settings: Settings,
) -> tuple[SpeechSynthesisProvider, TrustedVoiceProfile] | None:
    """The OPTIONAL Persian voice: built only when ``GEMINI_API_KEY`` is
    locally configured, otherwise ``None`` (Persian stays text-only). The
    model is a Sam constant; nothing here selects a paid tier or provider."""

    if settings.gemini_api_key is None:
        return None
    try:
        profile = TrustedVoiceProfile(
            profile_id=PERSIAN_SPEECH_PROFILE,
            provider_id=GEMINI_PROVIDER_ID,
            provider_voice_reference=settings.gemini_tts_voice,
            provider_model=GEMINI_TTS_MODEL,
            output_format=TTSAudioFormat.WAV,
        )
    except ValueError:
        return None
    ref = TTSCredentialReference(
        provider_id=GEMINI_PROVIDER_ID, credential_id="gemini-main"
    )
    provider = GeminiTTSProvider(
        credentials=gemini_credentials_from_settings(settings, ref),
        credential_reference=ref,
    )
    return provider, profile


def build_desktop_runtime(
    settings: Settings,
    agent: AgentCore,
    *,
    agent_configured: bool | None = None,
    model_router: ModelRouter | None = None,
    model_settings: ModelSettingsStore | None = None,
    transcription_provider: TranscriptionProvider | None = None,
    speech_provider: SpeechSynthesisProvider | None = None,
    speech_profiles: TrustedVoiceProfiles | None = None,
    mcp_admin: MCPRegistryAdmin | None = None,
    speaker_embedder: SpeakerEmbeddingProvider | None = None,
    profile_store: VoiceProfileStore | None = None,
    clock: Callable[[], datetime] = utc_now,
    durable: DurableState | None = None,
    startup: StartupReport | None = None,
    credentials: CredentialReport | None = None,
    wake_transcriber: TranscriptionProvider | None = None,
) -> DesktopRuntime:
    """Compose the runtime from trusted configuration.

    ``wake_transcriber`` (tests / trusted composition) must be a LOCAL
    recognizer; production uses the local voice stack's recognizer only.

    ``mcp_admin`` exists so a trusted composition/test can pre-register tools;
    the runtime keeps only ``mcp_admin.reader()``. Production passes nothing,
    which yields an empty registry (no connectors are configured)."""

    extra_speech_providers: list[SpeechSynthesisProvider] = []
    local_wake = wake_transcriber
    if speech_provider is None and speech_profiles is None:
        configured = fish_speech_from_settings(settings)
        if configured is not None:
            speech_provider, speech_profiles = configured
        persian = gemini_speech_from_settings(settings)
        if persian is not None:
            gemini_provider, gemini_profile = persian
            fish_profiles = (
                list(speech_profiles.list_profiles()) if speech_profiles else []
            )
            speech_profiles = TrustedVoiceProfiles([*fish_profiles, gemini_profile])
            if speech_provider is None:
                speech_provider = gemini_provider
            else:
                extra_speech_providers.append(gemini_provider)
    if (
        speaker_embedder is None
        and profile_store is None
        and transcription_provider is None
        and settings.voice_identity_enabled
    ):
        from sam.voice_local.factory import local_voice_from_settings

        stack = local_voice_from_settings(settings)
        if stack is not None:
            speaker_embedder = stack.embedder
            profile_store = stack.store
            transcription_provider = stack.transcriber
            local_wake = stack.transcriber
    if transcription_provider is not None:
        # Lets the user's language preference nudge the recognizer, per request.
        transcription_provider = HintedTranscriptionProvider(transcription_provider)
    admin = mcp_admin or MCPRegistryAdmin()
    runtime = DesktopRuntime(
        settings=settings,
        agent=agent,
        agent_configured=(
            agent_configured
            if agent_configured is not None
            else (model_router.any_usable() if model_router is not None else False)
        ),
        mcp_registry=admin.reader(),
        transcription_provider=transcription_provider,
        speech_provider=speech_provider,
        model_router=model_router,
        model_settings=model_settings,
        speech_profiles=speech_profiles,
        extra_speech_providers=extra_speech_providers,
        speaker_embedder=speaker_embedder,
        profile_store=profile_store,
        clock=clock,
        durable=durable,
        startup=startup,
        credentials=credentials,
    )
    # Wake detection needs owner identity too: without it no turn could ever
    # be verified, so there is nothing to wake for.
    if runtime.identity is not None:
        runtime.wake_transcriber = local_wake
        if runtime.owner_settings.voice_activation():
            runtime.warm_wake_recognizer()
    return runtime


__all__ = [
    "DEFAULT_SPEECH_PROFILE",
    "KNOWLEDGE_COLLECTION",
    "LOCAL_PRINCIPAL",
    "ActivityLog",
    "DesktopRuntime",
    "bootstrap_grants",
    "bootstrap_identity",
    "build_desktop_runtime",
    "PERSIAN_SPEECH_PROFILE",
    "fish_speech_from_settings",
    "gemini_speech_from_settings",
]
