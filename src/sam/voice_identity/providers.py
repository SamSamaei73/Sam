"""Provider-independent speaker-embedding abstractions.

Sam's security model never depends on a particular ML library. A provider
turns validated audio into a fixed-length embedding (biometric data: never
logged, audited, prompted, or persisted by anything but the secure store) and
declares the exact model it ran, so a stored template can be rejected if the
model changes.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Callable, Sequence
from threading import RLock
from typing import Protocol

from sam.voice.models import ValidatedAudio
from sam.voice.transcription import simulate_provider_delay


class SpeakerEmbeddingProvider(Protocol):
    provider_id: str
    model_id: str
    model_revision: str
    dimension: int

    def embed(
        self, audio: ValidatedAudio, *, timeout_seconds: float
    ) -> Sequence[float]:
        """Return one embedding for ``audio`` (in memory only)."""


class SpeakerVerificationProvider(Protocol):
    """Scores an embedding against an enrolled template."""

    def score(self, template: Sequence[float], embedding: Sequence[float]) -> float: ...


def normalize(vector: Sequence[float]) -> tuple[float, ...]:
    values = [float(x) for x in vector]
    norm = math.sqrt(sum(x * x for x in values))
    if not values or not math.isfinite(norm) or norm == 0.0:
        raise ValueError("cannot normalize an empty or degenerate vector")
    return tuple(x / norm for x in values)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        raise ValueError("dimension mismatch")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        raise ValueError("degenerate vector")
    return dot / (na * nb)


class CosineSpeakerVerification:
    def score(self, template: Sequence[float], embedding: Sequence[float]) -> float:
        return cosine_similarity(template, embedding)


class FakeSpeakerEmbeddingProvider:
    """Deterministic synthetic embeddings for tests: no audio model, no
    biometrics. Audio registered by digest maps to a chosen vector; any other
    audio maps to a stable pseudo-random vector (a "different speaker")."""

    provider_id = "fake-speaker-embedding"

    def __init__(
        self,
        *,
        dimension: int = 16,
        model_id: str = "fake-ecapa",
        model_revision: str = "test-1",
        raises: BaseException | None = None,
        delay_seconds: float = 0.0,
        result: Callable[[ValidatedAudio], object] | None = None,
    ) -> None:
        self.dimension = dimension
        self.model_id = model_id
        self.model_revision = model_revision
        self._raises = raises
        self._delay = delay_seconds
        self._result = result
        self._by_digest: dict[str, tuple[float, ...]] = {}
        self._lock = RLock()
        self.call_count = 0

    def register(self, audio_digest: str, vector: Sequence[float]) -> None:
        self._by_digest[audio_digest] = tuple(float(x) for x in vector)

    def pseudo_vector(self, seed: str) -> tuple[float, ...]:
        raw = hashlib.sha256(seed.encode()).digest() * ((self.dimension * 4) // 32 + 1)
        values = [
            (struct.unpack_from("<H", raw, i * 2)[0] / 32767.5) - 1.0
            for i in range(self.dimension)
        ]
        return normalize(values)

    def embed(
        self, audio: ValidatedAudio, *, timeout_seconds: float
    ) -> Sequence[float]:
        with self._lock:
            self.call_count += 1
        simulate_provider_delay(self._delay, timeout_seconds, honor_timeout=True)
        if self._raises is not None:
            raise self._raises
        if self._result is not None:
            return self._result(audio)  # type: ignore[return-value]  # untrusted on purpose
        digest = audio.metadata.digest_sha256
        return self._by_digest.get(digest) or self.pseudo_vector(digest)

    def __repr__(self) -> str:
        return f"FakeSpeakerEmbeddingProvider(model_id={self.model_id!r})"


__all__ = [
    "CosineSpeakerVerification",
    "FakeSpeakerEmbeddingProvider",
    "SpeakerEmbeddingProvider",
    "SpeakerVerificationProvider",
    "cosine_similarity",
    "normalize",
]
