"""Embedding provider abstraction — the future semantic-retrieval boundary.

No external embedding API (OpenAI, Anthropic, Voyage, Cohere, ...) is
called anywhere in this phase. ``FakeEmbeddingProvider`` is a
deterministic, local, stdlib-only stand-in that exists purely to prove
the architectural boundary — its vectors carry no real semantic meaning
and must never be presented to a user or another system as if they did.
Nothing in ``sam.knowledge.retrieval`` currently consults an
``EmbeddingProvider`` at all; Phase 7's retrieval path is lexical (see
``sam.knowledge.index``). This module is deliberately unwired, ready for
a future vector index to depend on.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Protocol

Embedding = tuple[float, ...]

_FAKE_EMBEDDING_DIMENSION = 32


class EmbeddingProvider(Protocol):
    """A batch text-to-vector contract. No implementation in this phase
    calls a network service — see the module docstring."""

    def embed(self, texts: Sequence[str]) -> Sequence[Embedding]: ...


class FakeEmbeddingProvider:
    """Deterministic, content-derived vectors with no semantic meaning.

    Each dimension is a normalized count of how often one fixed hash
    bucket appears among the text's characters — reproducible, bounded,
    and entirely local. This is explicitly **not** a semantic embedding:
    two texts about the same topic in different words will not
    necessarily produce similar vectors.
    """

    def __init__(self, *, dimension: int = _FAKE_EMBEDDING_DIMENSION) -> None:
        if dimension < 1:
            raise ValueError("dimension must be positive")
        self._dimension = dimension

    def embed(self, texts: Sequence[str]) -> Sequence[Embedding]:
        return tuple(self._embed_one(text) for text in texts)

    def _embed_one(self, text: str) -> Embedding:
        buckets = [0] * self._dimension
        for char in text:
            digest = hashlib.sha256(char.encode("utf-8", errors="ignore")).digest()
            bucket = digest[0] % self._dimension
            buckets[bucket] += 1
        total = sum(buckets) or 1
        return tuple(count / total for count in buckets)


__all__ = ["Embedding", "EmbeddingProvider", "FakeEmbeddingProvider"]
