"""Speaker verification: an authentication *signal*, never an authorization.

Fail-closed by construction: every defect (no audio, a store failure, a model
mismatch, a provider error or hostile embedding) becomes a non-owner result,
and a store read *failure* is never reported as "not enrolled".
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from datetime import datetime

from sam.voice.models import DEFAULT_VOICE_PROVIDER_TIMEOUT_SECONDS, ValidatedAudio
from sam.voice_identity.errors import ProfileStoreError
from sam.voice_identity.models import OwnerTemplate, SpeakerVerification, utc_now
from sam.voice_identity.policy import (
    MAX_EMBEDDING_DIMENSION,
    MIN_EMBEDDING_DIMENSION,
    MIN_VERIFICATION_SECONDS,
    SpeakerResult,
    ThresholdPolicy,
)
from sam.voice_identity.providers import (
    CosineSpeakerVerification,
    SpeakerEmbeddingProvider,
    SpeakerVerificationProvider,
)
from sam.voice_identity.quality import quality_problem
from sam.voice_identity.store import VoiceProfileStore


def valid_embedding(raw: object, dimension: int) -> tuple[float, ...] | None:
    """Untrusted provider output -> a finite vector of the expected size."""

    if isinstance(raw, str | bytes) or not isinstance(raw, Sequence):
        return None
    if not MIN_EMBEDDING_DIMENSION <= len(raw) <= MAX_EMBEDDING_DIMENSION:
        return None
    if len(raw) != dimension:
        return None
    out: list[float] = []
    for value in raw:
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            return None
        out.append(float(value))
    return tuple(out)


class SpeakerVerifier:
    def __init__(
        self,
        *,
        embedder: SpeakerEmbeddingProvider,
        store: VoiceProfileStore,
        policy: ThresholdPolicy | None = None,
        scorer: SpeakerVerificationProvider | None = None,
        timeout_seconds: float = DEFAULT_VOICE_PROVIDER_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._policy = policy or ThresholdPolicy()
        self._scorer = scorer or CosineSpeakerVerification()
        self._timeout = timeout_seconds
        self._clock = clock

    @property
    def embedder(self) -> SpeakerEmbeddingProvider:
        return self._embedder

    def is_enrolled(self) -> bool | None:
        """``True``/``False``, or ``None`` if the store could not be read."""

        try:
            return self._store.load() is not None
        except ProfileStoreError:
            return None

    def verify(self, audio: ValidatedAudio) -> SpeakerVerification:
        return self._verify(audio)[0]

    def verify_diagnostic(
        self, audio: ValidatedAudio
    ) -> tuple[SpeakerVerification, float | None]:
        """Calibration/testing only: also returns the similarity. The score is
        never exposed through the coordinator, the API, audit, or logs."""

        return self._verify(audio)

    def _result(
        self,
        result: SpeakerResult,
        reason: str,
        audio: ValidatedAudio,
        score: float | None = None,
    ) -> tuple[SpeakerVerification, float | None]:
        return (
            SpeakerVerification(result.value, reason, audio.metadata.digest_sha256),
            score,
        )

    def _verify(
        self, audio: ValidatedAudio
    ) -> tuple[SpeakerVerification, float | None]:
        if audio.metadata.duration_seconds < MIN_VERIFICATION_SECONDS:
            return self._result(
                SpeakerResult.INSUFFICIENT_AUDIO, "audio_too_short", audio
            )
        problem = quality_problem(audio)
        if problem is not None:
            return self._result(SpeakerResult.INSUFFICIENT_AUDIO, problem, audio)
        template = self._load_template()
        if template is False:
            return self._result(
                SpeakerResult.VERIFICATION_ERROR, "profile_unreadable", audio
            )
        if template is None:
            return self._result(SpeakerResult.NOT_ENROLLED, "not_enrolled", audio)
        assert isinstance(template, OwnerTemplate)
        if (
            template.model_id != self._embedder.model_id
            or template.model_revision != self._embedder.model_revision
            or template.dimension != self._embedder.dimension
        ):
            return self._result(
                SpeakerResult.VERIFICATION_ERROR, "model_mismatch", audio
            )
        deadline = time.monotonic() + self._timeout
        try:
            raw = self._embedder.embed(audio, timeout_seconds=self._timeout)
        except Exception:
            return self._result(
                SpeakerResult.VERIFICATION_ERROR, "provider_error", audio
            )
        if time.monotonic() > deadline:
            return self._result(
                SpeakerResult.VERIFICATION_ERROR, "provider_timeout", audio
            )
        embedding = valid_embedding(raw, template.dimension)
        if embedding is None:
            return self._result(
                SpeakerResult.VERIFICATION_ERROR, "provider_invalid", audio
            )
        try:
            score = self._scorer.score(template.vector, embedding)
        except Exception:
            return self._result(
                SpeakerResult.VERIFICATION_ERROR, "provider_invalid", audio
            )
        if not math.isfinite(score):
            return self._result(
                SpeakerResult.VERIFICATION_ERROR, "provider_invalid", audio
            )
        if self._policy.accepts(score):
            return self._result(
                SpeakerResult.OWNER_VERIFIED, "speaker_match", audio, score
            )
        return self._result(
            SpeakerResult.OWNER_NOT_VERIFIED, "speaker_mismatch", audio, score
        )

    def _load_template(self) -> OwnerTemplate | None | bool:
        try:
            return self._store.load()
        except ProfileStoreError:
            return False


__all__ = ["SpeakerVerifier", "valid_embedding"]
