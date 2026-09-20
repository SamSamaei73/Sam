"""Voice-identity data models.

``OwnerTemplate`` is biometric data. It is immutable, its repr is redacted,
and it has exactly one serialization (``to_json``) whose only sink is the
secure profile store.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sam.voice_identity.errors import ProfileStoreError
from sam.voice_identity.policy import MAX_EMBEDDING_DIMENSION, MIN_EMBEDDING_DIMENSION

TEMPLATE_FORMAT_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(UTC)


def l2_norm(vector: tuple[float, ...] | list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


@dataclass(frozen=True, repr=False)
class OwnerTemplate:
    """The aggregated, L2-normalized owner voice template (no raw audio)."""

    vector: tuple[float, ...] = field(repr=False)
    model_id: str
    model_revision: str
    sample_count: int
    created_at: datetime
    format_version: int = TEMPLATE_FORMAT_VERSION

    def __post_init__(self) -> None:
        dim = len(self.vector)
        if not MIN_EMBEDDING_DIMENSION <= dim <= MAX_EMBEDDING_DIMENSION:
            raise ValueError("template dimension is invalid")
        if not all(isinstance(x, float) and math.isfinite(x) for x in self.vector):
            raise ValueError("template contains non-finite values")
        if abs(l2_norm(self.vector) - 1.0) > 1e-3:
            raise ValueError("template must be L2-normalized")
        if not self.model_id or not self.model_revision:
            raise ValueError("template must record its model")
        if self.sample_count < 1 or self.created_at.tzinfo is None:
            raise ValueError("template metadata is invalid")

    @property
    def dimension(self) -> int:
        return len(self.vector)

    def __repr__(self) -> str:  # never print biometric values
        return f"OwnerTemplate(<redacted biometric template model={self.model_id!r}>)"

    __str__ = __repr__

    def to_json(self) -> str:
        """Serialize for the secure store ONLY."""

        return json.dumps(
            {
                "v": self.format_version,
                "model_id": self.model_id,
                "model_revision": self.model_revision,
                "sample_count": self.sample_count,
                "created_at": self.created_at.isoformat(),
                "vector": list(self.vector),
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> OwnerTemplate:
        """Parse untrusted stored text. Any defect raises ProfileStoreError
        (callers must treat that as an error, never as 'not enrolled')."""

        try:
            data: Any = json.loads(text)
            if not isinstance(data, dict) or data.get("v") != TEMPLATE_FORMAT_VERSION:
                raise ValueError
            return cls(
                vector=tuple(float(x) for x in data["vector"]),
                model_id=str(data["model_id"]),
                model_revision=str(data["model_revision"]),
                sample_count=int(data["sample_count"]),
                created_at=datetime.fromisoformat(data["created_at"]),
            )
        except Exception:
            raise ProfileStoreError("stored voice profile is unreadable") from None


@dataclass(frozen=True)
class SpeakerVerification:
    """Safe verification metadata. Deliberately has no score or embedding."""

    result: str
    reason_code: str
    audio_digest: str


__all__ = ["OwnerTemplate", "SpeakerVerification", "l2_norm", "utc_now"]
