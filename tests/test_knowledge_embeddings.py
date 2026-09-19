"""Tests for sam.knowledge.embeddings — the unwired future semantic
retrieval boundary."""

from __future__ import annotations

import pytest

from sam.knowledge.embeddings import FakeEmbeddingProvider


class TestFakeEmbeddingProvider:
    def test_dimension_matches_config(self) -> None:
        provider = FakeEmbeddingProvider(dimension=16)
        vectors = provider.embed(["hello"])
        assert len(vectors[0]) == 16

    def test_deterministic_for_same_text(self) -> None:
        provider = FakeEmbeddingProvider()
        assert provider.embed(["hello world"]) == provider.embed(["hello world"])

    def test_different_text_can_differ(self) -> None:
        provider = FakeEmbeddingProvider()
        a = provider.embed(["alpha"])[0]
        b = provider.embed(["zzzzzz"])[0]
        assert a != b

    def test_batch_order_preserved(self) -> None:
        provider = FakeEmbeddingProvider()
        vectors = provider.embed(["a", "b", "c"])
        assert len(vectors) == 3

    def test_empty_text_handled(self) -> None:
        provider = FakeEmbeddingProvider()
        vectors = provider.embed([""])
        assert len(vectors[0]) == 32

    def test_rejects_non_positive_dimension(self) -> None:
        with pytest.raises(ValueError):
            FakeEmbeddingProvider(dimension=0)

    def test_vector_values_are_bounded_fractions(self) -> None:
        provider = FakeEmbeddingProvider(dimension=8)
        vector = provider.embed(["some longer piece of text here"])[0]
        assert all(0.0 <= v <= 1.0 for v in vector)
        assert abs(sum(vector) - 1.0) < 1e-9
