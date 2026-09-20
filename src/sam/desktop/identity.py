"""Desktop composition of owner voice identity, Guest Mode and language.

Voice identity is an authentication signal, not authorization. Nothing here
produces a PermissionEngine decision. A guest gets a SEPARATE permission
store/engine that grants only the voice-session operations needed to hold a
conversation (so PermissionEngine is still the authority, and the guest holds
no Knowledge, Memory, tool, permission or send/publish/delete grant of any
kind). That store is discarded when the guest session ends.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Literal

from sam.language.policy import LanguagePolicy, LanguagePreference
from sam.permissions.audit import InMemoryAuditSink
from sam.permissions.confirmation import InMemoryConfirmationProvider
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    PermissionAction,
    PermissionGrant,
    PermissionResource,
    PermissionScope,
    Principal,
    utc_now,
)
from sam.permissions.store import InMemoryPermissionStore
from sam.voice.agent_boundary import AgentSink, VoiceAgentBoundary
from sam.voice.audit import InMemoryVoiceAuditSink
from sam.voice.gateway import VoiceGateway
from sam.voice.models import StartSessionRequest
from sam.voice.transcription import TranscriptionProvider
from sam.voice_identity.audit import InMemoryVoiceIdentityAuditSink
from sam.voice_identity.authority import OwnerProofAuthority, StepUpAuthority
from sam.voice_identity.challenge import ChallengeService
from sam.voice_identity.coordinator import VoiceIdentityCoordinator
from sam.voice_identity.enrollment import EnrollmentService
from sam.voice_identity.guest import GuestSession, GuestSessionManager
from sam.voice_identity.policy import ThresholdPolicy
from sam.voice_identity.providers import SpeakerEmbeddingProvider
from sam.voice_identity.store import VoiceProfileStore
from sam.voice_identity.verification import SpeakerVerifier


@dataclass
class _GuestVoice:
    guest_id: str
    principal: Principal
    boundary_factory: Callable[[LanguagePreference], VoiceAgentBoundary]
    session_id: str
    # The guest's own permission store (exposed read-only-by-convention so
    # tests can prove it holds nothing but voice-session grants).
    permission_store: InMemoryPermissionStore


class DesktopVoiceIdentity:
    def __init__(
        self,
        *,
        embedder: SpeakerEmbeddingProvider,
        store: VoiceProfileStore,
        transcriber: TranscriptionProvider,
        agent: AgentSink,
        threshold: float,
        language: LanguagePolicy,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._transcriber = transcriber
        self._agent = agent
        self._clock = clock
        self.language = language
        self.embedder = embedder
        self.audit = InMemoryVoiceIdentityAuditSink()
        self.step_up = StepUpAuthority(clock=clock)
        self.proofs = OwnerProofAuthority(clock=clock)
        self.session_id = secrets.token_hex(16)  # binds challenges/proofs
        self.verifier = SpeakerVerifier(
            embedder=embedder,
            store=store,
            policy=ThresholdPolicy(threshold),
            clock=clock,
        )
        self.enrollment = EnrollmentService(
            embedder=embedder,
            store=store,
            step_up=self.step_up,
            audit=self.audit,
            policy=ThresholdPolicy(threshold),
            clock=clock,
        )
        self.challenges = ChallengeService(clock=clock)
        self.guests = GuestSessionManager(
            proofs=self.proofs, step_up=self.step_up, audit=self.audit, clock=clock
        )
        self.coordinator = VoiceIdentityCoordinator(
            verifier=self.verifier,
            challenges=self.challenges,
            guests=self.guests,
            proofs=self.proofs,
            audit=self.audit,
            transcriber=transcriber,
            clock=clock,
        )
        self.last_speaker_result: str | None = None
        self._guest_voice: _GuestVoice | None = None
        self._lock = RLock()

    # ------------------------------------------------------------ guest voice

    def guest_voice(self) -> _GuestVoice | None:
        """A voice pipeline for the CURRENT guest only, over its own
        permission engine. ``None`` when no guest session is active."""

        session = self.guests.current()
        with self._lock:
            if session is None:
                self._guest_voice = None  # discard: grants and context are gone
                return None
            if (
                self._guest_voice is None
                or self._guest_voice.guest_id != session.guest_id
            ):
                self._guest_voice = self._build_guest_voice(session)
            return self._guest_voice

    def _build_guest_voice(self, session: GuestSession) -> _GuestVoice:
        store = InMemoryPermissionStore()
        now = self._clock()
        for action in (
            PermissionAction.CREATE,
            PermissionAction.READ,
            PermissionAction.UPDATE,
        ):
            store.create_grant(
                PermissionGrant(
                    grant_id=f"guest-{session.guest_id}-{action.value}",
                    principal=session.principal,
                    resource=PermissionResource.VOICE,
                    action=action,
                    scope=PermissionScope.identifier("session"),
                    created_at=now,
                    updated_at=now,
                    expires_at=session.expires_at,
                    metadata={"origin": "guest_mode"},
                )
            )
        engine = PermissionEngine(
            store=store,
            confirmation_provider=InMemoryConfirmationProvider(),
            audit_sink=InMemoryAuditSink(),
            clock=self._clock,
        )
        gateway = VoiceGateway(
            permission_engine=engine,
            transcription_provider=self._transcriber,
            audit_sink=InMemoryVoiceAuditSink(),
            clock=self._clock,
        )
        started = gateway.start_session(
            StartSessionRequest(principal=session.principal)
        )
        assert started.session_id is not None
        agent = self._agent

        def factory(preference: LanguagePreference) -> VoiceAgentBoundary:
            def language_for(text: str, tag: str | None) -> Literal["fa", "en"]:
                decision = self.language.resolve(preference, text, stt_language=tag)
                return decision.response_language

            return VoiceAgentBoundary(gateway, agent, language_for=language_for)

        return _GuestVoice(
            session.guest_id,
            session.principal,
            factory,
            started.session_id,
            store,
        )


__all__ = ["DesktopVoiceIdentity"]
