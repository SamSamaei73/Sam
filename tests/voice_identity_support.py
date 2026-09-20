"""Fixtures for voice-identity tests: synthetic embeddings only.

No real audio model, no biometrics. Every "voice" is a fixed vector; audio is a
deterministic synthetic PCM ramp registered against a vector by digest.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sam.permissions.audit import InMemoryAuditSink
from sam.permissions.confirmation import InMemoryConfirmationProvider
from sam.permissions.engine import PermissionEngine
from sam.permissions.store import InMemoryPermissionStore
from sam.voice.audio import validate_audio
from sam.voice.models import AudioFormat, AudioInput, ValidatedAudio
from sam.voice.transcription import FakeTranscriptionProvider
from sam.voice_identity.audit import InMemoryVoiceIdentityAuditSink
from sam.voice_identity.authority import OwnerProofAuthority, StepUpAuthority
from sam.voice_identity.challenge import ChallengeService
from sam.voice_identity.coordinator import VoiceIdentityCoordinator
from sam.voice_identity.enrollment import EnrollmentService
from sam.voice_identity.guest import GuestSessionManager
from sam.voice_identity.policy import ThresholdPolicy
from sam.voice_identity.providers import FakeSpeakerEmbeddingProvider, normalize
from sam.voice_identity.store import InMemoryVoiceProfileStore
from sam.voice_identity.verification import SpeakerVerifier
from tests.voice_support import make_wav

DIM = 16
START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


_PSEUDO = FakeSpeakerEmbeddingProvider(dimension=DIM)


def _pick_voices() -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Two synthetic 'speakers' that are clearly different (|cos| < 0.2)."""

    owner = _PSEUDO.pseudo_vector("owner-voice-0")
    for i in range(200):
        other = _PSEUDO.pseudo_vector(f"other-voice-{i}")
        if abs(sum(a * b for a, b in zip(owner, other, strict=True))) < 0.2:
            return owner, other
    raise AssertionError("no distinct synthetic voices found")


OWNER_VOICE, OTHER_VOICE = _pick_voices()


def jitter(voice: Sequence[float], k: int, amount: float = 0.3) -> tuple[float, ...]:
    """A natural-looking take of ``voice``: close, but never identical."""

    noise = _PSEUDO.pseudo_vector(f"take-noise-{k}")
    return normalize([v + amount * n for v, n in zip(voice, noise, strict=True)])


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class Harness:
    """Every voice-identity component wired together over fakes."""

    def __init__(
        self,
        *,
        threshold: float = 0.5,
        stt_text: Any = "hello there",
        embedder: FakeSpeakerEmbeddingProvider | None = None,
        store: InMemoryVoiceProfileStore | None = None,
    ) -> None:
        self.clock = Clock()
        self.embedder = embedder or FakeSpeakerEmbeddingProvider(dimension=DIM)
        self.store = store or InMemoryVoiceProfileStore()
        self.audit = InMemoryVoiceIdentityAuditSink()
        self.step_up = StepUpAuthority(clock=self.clock)
        self.proofs = OwnerProofAuthority(clock=self.clock)
        self.policy = ThresholdPolicy(threshold)
        self.verifier = SpeakerVerifier(
            embedder=self.embedder,
            store=self.store,
            policy=self.policy,
            clock=self.clock,
        )
        self.enrollment = EnrollmentService(
            embedder=self.embedder,
            store=self.store,
            step_up=self.step_up,
            audit=self.audit,
            policy=self.policy,
            clock=self.clock,
        )
        self.challenges = ChallengeService(clock=self.clock)
        self.guests = GuestSessionManager(
            proofs=self.proofs, step_up=self.step_up, audit=self.audit, clock=self.clock
        )
        self.stt = FakeTranscriptionProvider(stt_text)
        self.coordinator = VoiceIdentityCoordinator(
            verifier=self.verifier,
            challenges=self.challenges,
            guests=self.guests,
            proofs=self.proofs,
            audit=self.audit,
            transcriber=self.stt,
            clock=self.clock,
        )
        self._n = 0

    def audio(
        self, voice: Sequence[float], *, frames: int = 40_000, seed: int | None = None
    ) -> ValidatedAudio:
        """A fresh, valid, unique clip that the fake embedder maps to ``voice``."""

        self._n += 1
        n = self._n if seed is None else seed
        wav = make_wav(frames=frames, seed=n)
        validated = validate_audio(
            AudioInput(content=wav, declared_format=AudioFormat.WAV_PCM16)
        )
        self.embedder.register(validated.metadata.digest_sha256, jitter(voice, n))
        return validated

    def audio_input(self, voice: Sequence[float]) -> AudioInput:
        self._n += 1
        wav = make_wav(frames=40_000, seed=self._n)
        validated = validate_audio(
            AudioInput(content=wav, declared_format=AudioFormat.WAV_PCM16)
        )
        self.embedder.register(validated.metadata.digest_sha256, jitter(voice, self._n))
        return AudioInput(content=wav, declared_format=AudioFormat.WAV_PCM16)

    def enroll_owner(
        self, voice: Sequence[float] = OWNER_VOICE, samples: int = 4
    ) -> None:
        grant = self.step_up.mint("enroll")
        session = self.enrollment.begin(grant)
        for _ in range(samples):
            assert self.enrollment.add_sample(session, self.audio(voice)).accepted
        outcome = self.enrollment.complete(session)
        assert outcome.completed, outcome

    def activate_guest(self, minutes: int = 15) -> Any:
        """Owner says the challenge (STT fake echoes it) -> proof -> guest."""

        challenge = self.coordinator.issue_challenge("s1")
        self.stt._result = " ".join(challenge.digits) + " " + challenge.word_id
        proof = self.coordinator.prove_owner(
            session_id="s1",
            challenge_id=challenge.challenge_id,
            audio=self.audio_input(OWNER_VOICE),
        )
        assert proof is not None
        return self.guests.activate(
            proof, self.step_up.mint("start_guest"), session_id="s1", minutes=minutes
        )


def permission_engine() -> tuple[
    PermissionEngine, InMemoryPermissionStore, InMemoryConfirmationProvider
]:
    store = InMemoryPermissionStore()
    confirmations = InMemoryConfirmationProvider()
    engine = PermissionEngine(
        store=store, confirmation_provider=confirmations, audit_sink=InMemoryAuditSink()
    )
    return engine, store, confirmations
