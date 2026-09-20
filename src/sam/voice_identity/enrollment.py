"""Explicit owner voice enrollment.

The owner deliberately starts enrollment (backend-verified step-up first),
records 3-5 varied samples (ideally Persian and English), and Sam aggregates
their embeddings into ONE L2-normalized template that is persisted only in the
secure store. Raw enrollment audio is never stored: it is embedded in memory
and dropped, and the per-sample embeddings are discarded when enrollment
completes or is cancelled.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock

from sam.voice.models import DEFAULT_VOICE_PROVIDER_TIMEOUT_SECONDS, ValidatedAudio
from sam.voice_identity.audit import VoiceIdentityAuditSink
from sam.voice_identity.authority import StepUpAuthority, StepUpGrant
from sam.voice_identity.errors import EnrollmentError, ProfileStoreError
from sam.voice_identity.models import OwnerTemplate, utc_now
from sam.voice_identity.policy import (
    DUPLICATE_SIMILARITY,
    ENROLLMENT_SESSION_TTL,
    MAX_ENROLLMENT_SAMPLES,
    MIN_ENROLLMENT_SAMPLES,
    MIN_SAMPLE_SECONDS,
    MIN_SELF_CONSISTENCY,
    ThresholdPolicy,
)
from sam.voice_identity.providers import (
    SpeakerEmbeddingProvider,
    cosine_similarity,
    normalize,
)
from sam.voice_identity.quality import quality_problem
from sam.voice_identity.store import VoiceProfileStore
from sam.voice_identity.verification import valid_embedding


@dataclass
class _Session:
    session_id: str
    purpose: str
    started_at: datetime
    embeddings: list[tuple[float, ...]] = field(default_factory=list, repr=False)
    digests: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class EnrollmentProgress:
    accepted: bool
    sample_count: int
    needed: int
    reason_code: str


@dataclass(frozen=True)
class EnrollmentOutcome:
    completed: bool
    sample_count: int
    reason_code: str


@dataclass(frozen=True)
class EnrollmentStatus:
    enrolled: bool | None  # None: the secure store could not be read
    model_id: str
    sample_count: int | None = None


class EnrollmentService:
    def __init__(
        self,
        *,
        embedder: SpeakerEmbeddingProvider,
        store: VoiceProfileStore,
        step_up: StepUpAuthority,
        audit: VoiceIdentityAuditSink,
        policy: ThresholdPolicy | None = None,
        timeout_seconds: float = DEFAULT_VOICE_PROVIDER_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._step_up = step_up
        self._audit = audit
        self._policy = policy or ThresholdPolicy()
        self._timeout = timeout_seconds
        self._clock = clock
        self._session: _Session | None = None
        self._lock = RLock()

    # ------------------------------------------------------------ status

    def status(self) -> EnrollmentStatus:
        try:
            template = self._store.load()
        except ProfileStoreError:
            return EnrollmentStatus(None, self._embedder.model_id)
        if template is None:
            return EnrollmentStatus(False, self._embedder.model_id)
        return EnrollmentStatus(True, template.model_id, template.sample_count)

    # ------------------------------------------------------------- flow

    def begin(self, grant: StepUpGrant | None, *, re_enroll: bool = False) -> str:
        purpose = "re_enroll" if re_enroll else "enroll"
        self._step_up.consume(grant, purpose)  # raises AuthorizationError
        status = self.status()
        if status.enrolled is None:
            raise EnrollmentError("secure store unavailable")
        if status.enrolled and not re_enroll:
            raise EnrollmentError("an owner voice profile already exists")
        if not status.enrolled and re_enroll:
            raise EnrollmentError("there is no owner voice profile to replace")
        session = _Session(secrets.token_hex(16), purpose, self._clock())
        with self._lock:
            self._discard_locked()  # a new attempt supersedes any old one
            self._session = session
        self._audit.record("owner_enrollment_started", "ok")
        return session.session_id

    def add_sample(self, session_id: str, audio: ValidatedAudio) -> EnrollmentProgress:
        with self._lock:
            session = self._active(session_id)
            count = len(session.embeddings)
            if count >= MAX_ENROLLMENT_SAMPLES:
                return self._progress(session, False, "enough_samples")
            if audio.metadata.duration_seconds < MIN_SAMPLE_SECONDS:
                return self._progress(session, False, "audio_too_short")
            problem = quality_problem(audio)
            if problem is not None:
                return self._progress(session, False, problem)
            if audio.metadata.digest_sha256 in session.digests:
                return self._progress(session, False, "duplicate_sample")
            embedding = self._embed(audio)
            if embedding is None:
                return self._progress(session, False, "provider_error")
            unit = normalize(embedding)
            if any(
                cosine_similarity(unit, e) >= DUPLICATE_SIMILARITY
                for e in session.embeddings
            ):
                return self._progress(session, False, "duplicate_sample")
            session.embeddings.append(unit)
            session.digests.add(audio.metadata.digest_sha256)
            return self._progress(session, True, "ok")

    def complete(self, session_id: str) -> EnrollmentOutcome:
        with self._lock:
            session = self._active(session_id)
            embeddings = list(session.embeddings)
            if len(embeddings) < MIN_ENROLLMENT_SAMPLES:
                return EnrollmentOutcome(False, len(embeddings), "too_few_samples")
            reason = self._consistency_problem(embeddings)
            if reason is not None:
                self._fail_locked("owner_enrollment_failed", reason)
                return EnrollmentOutcome(False, len(embeddings), reason)
            mean = [sum(col) / len(embeddings) for col in zip(*embeddings, strict=True)]
            try:
                template = OwnerTemplate(
                    vector=normalize(mean),
                    model_id=self._embedder.model_id,
                    model_revision=self._embedder.model_revision,
                    sample_count=len(embeddings),
                    created_at=self._clock(),
                )
                self._store.save(template)  # atomic replace: old template invalid
            except (ProfileStoreError, ValueError):
                self._fail_locked("owner_enrollment_failed", "store_error")
                return EnrollmentOutcome(False, len(embeddings), "store_error")
            self._discard_locked()
            self._audit.record("owner_enrollment_completed", "ok")
            return EnrollmentOutcome(True, template.sample_count, "ok")

    def cancel(self, session_id: str) -> None:
        with self._lock:
            if self._session is not None and self._session.session_id == session_id:
                self._discard_locked()

    def delete_profile(self, grant: StepUpGrant | None) -> None:
        self._step_up.consume(grant, "delete_profile")
        with self._lock:
            self._discard_locked()
        try:
            self._store.delete()
        except ProfileStoreError:
            raise EnrollmentError(
                "the owner voice profile could not be removed"
            ) from None
        self._audit.record("owner_profile_deleted", "ok")

    # ---------------------------------------------------------- internals

    def _embed(self, audio: ValidatedAudio) -> tuple[float, ...] | None:
        deadline = time.monotonic() + self._timeout
        try:
            raw = self._embedder.embed(audio, timeout_seconds=self._timeout)
        except Exception:
            return None
        if time.monotonic() > deadline:
            return None
        return valid_embedding(raw, self._embedder.dimension)

    def _consistency_problem(self, embeddings: list[tuple[float, ...]]) -> str | None:
        n = len(embeddings)
        for i in range(n):
            for j in range(i + 1, n):
                if (
                    cosine_similarity(embeddings[i], embeddings[j])
                    < MIN_SELF_CONSISTENCY
                ):
                    return "inconsistent_samples"
        # Held-out check: each sample must verify against the mean of the
        # others at Sam's own threshold, or the owner would often be rejected.
        for i in range(n):
            rest = [e for k, e in enumerate(embeddings) if k != i]
            mean = normalize([sum(col) / len(rest) for col in zip(*rest, strict=True)])
            if not self._policy.accepts(cosine_similarity(mean, embeddings[i])):
                return "unstable_samples"
        return None

    def _active(self, session_id: str) -> _Session:
        session = self._session
        if session is None or session.session_id != session_id:
            raise EnrollmentError("enrollment session is invalid")
        if self._clock() - session.started_at > ENROLLMENT_SESSION_TTL:
            self._discard_locked()
            raise EnrollmentError("enrollment session expired")
        return session

    def _progress(
        self, session: _Session, accepted: bool, reason: str
    ) -> EnrollmentProgress:
        return EnrollmentProgress(
            accepted, len(session.embeddings), MIN_ENROLLMENT_SAMPLES, reason
        )

    def _fail_locked(self, event: str, reason: str) -> None:
        self._discard_locked()
        self._audit.record(event, reason)

    def _discard_locked(self) -> None:
        if self._session is not None:
            self._session.embeddings.clear()  # drop biometric intermediates
            self._session.digests.clear()
        self._session = None


__all__ = [
    "EnrollmentOutcome",
    "EnrollmentProgress",
    "EnrollmentService",
    "EnrollmentStatus",
]
