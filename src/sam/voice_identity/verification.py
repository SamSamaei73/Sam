"""Speaker verification: an authentication *signal*, never an authorization.

Fail-closed by construction: every defect (no audio, a store failure, a model
mismatch, a provider error or hostile embedding) becomes a non-owner result,
and a store read *failure* is never reported as "not enrolled".
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Sequence
from datetime import datetime

from sam.voice.models import DEFAULT_VOICE_PROVIDER_TIMEOUT_SECONDS, ValidatedAudio
from sam.voice_identity.errors import InsufficientSpeechError, ProfileStoreError
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
    embed_speaker,
)
from sam.voice_identity.quality import quality_problem
from sam.voice_identity.store import VoiceProfileStore

logger = logging.getLogger(__name__)
_DIAGNOSTIC_RESULTS = frozenset(result.value for result in SpeakerResult)
_DIAGNOSTIC_REASONS = frozenset(
    {
        "audio_too_short",
        "audio_too_quiet",
        "audio_clipped",
        "profile_unreadable",
        "not_enrolled",
        "model_mismatch",
        "provider_error",
        "provider_timeout",
        "provider_invalid",
        "speaker_match",
        "speaker_mismatch",
    }
)


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
        started = time.monotonic()
        verification = self._verify(audio)[0]
        # Local operational metadata only: distinguish capture length/quality
        # failures from a mismatch. Never log similarity, audio, a digest,
        # transcript, template, embedding, or provider exception text. Duration
        # remains total WAV duration; it is not a measurement of voiced speech.
        result = (
            verification.result
            if verification.result in _DIAGNOSTIC_RESULTS
            else "verification_error"
        )
        reason = (
            verification.reason_code
            if verification.reason_code in _DIAGNOSTIC_REASONS
            else "unknown"
        )
        try:
            logger.info(
                "Speaker verification result=%s reason=%s duration_ms=%d elapsed_ms=%d",
                result,
                reason,
                round(audio.metadata.duration_seconds * 1000),
                max(0, round((time.monotonic() - started) * 1000)),
            )
        except Exception:
            pass  # diagnostics can never change the verification result
        return verification

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
            raw = embed_speaker(
                self._embedder,
                audio,
                minimum_speech_seconds=MIN_VERIFICATION_SECONDS,
                timeout_seconds=self._timeout,
            )
        except InsufficientSpeechError:
            return self._result(
                SpeakerResult.INSUFFICIENT_AUDIO, "audio_too_short", audio
            )
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
