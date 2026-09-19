"""Tests for sam.knowledge.index.InMemoryLexicalIndex."""

from __future__ import annotations

from datetime import UTC, datetime

from sam.knowledge.index import InMemoryLexicalIndex
from sam.knowledge.models import ChunkLocation, DocumentChunk

NOW = datetime.now(UTC)


def _chunk(
    chunk_id: str, resource_id: str, collection_id: str, text: str, idx: int = 0
) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        resource_id=resource_id,
        collection_id=collection_id,
        sequence_index=idx,
        text=text,
        location=ChunkLocation(),
        created_at=NOW,
    )


class TestIndexingAndSearch:
    def test_search_finds_matching_terms(self) -> None:
        index = InMemoryLexicalIndex()
        index.index_chunks(
            (_chunk("c1", "r1", "docs", "graph neural networks are powerful"),)
        )
        results = index.search(
            collection_id=None, resource_id=None, query="graph networks", limit=10
        )
        assert len(results) == 1
        assert results[0].chunk_id == "c1"

    def test_search_no_match_returns_empty(self) -> None:
        index = InMemoryLexicalIndex()
        index.index_chunks((_chunk("c1", "r1", "docs", "graph neural networks"),))
        results = index.search(
            collection_id=None, resource_id=None, query="quantum physics", limit=10
        )
        assert results == ()

    def test_search_blank_query_returns_empty(self) -> None:
        index = InMemoryLexicalIndex()
        index.index_chunks((_chunk("c1", "r1", "docs", "graph neural networks"),))
        results = index.search(
            collection_id=None, resource_id=None, query="!!! ???", limit=10
        )
        assert results == ()

    def test_search_scoped_to_collection(self) -> None:
        index = InMemoryLexicalIndex()
        index.index_chunks(
            (
                _chunk("c1", "r1", "research", "graph neural networks"),
                _chunk("c2", "r2", "personal", "graph neural networks"),
            )
        )
        results = index.search(
            collection_id="research", resource_id=None, query="graph", limit=10
        )
        assert [m.chunk_id for m in results] == ["c1"]

    def test_search_scoped_to_resource(self) -> None:
        index = InMemoryLexicalIndex()
        index.index_chunks(
            (
                _chunk("c1", "r1", "docs", "graph neural networks", idx=0),
                _chunk("c2", "r2", "docs", "graph neural networks", idx=0),
            )
        )
        results = index.search(
            collection_id=None, resource_id="r1", query="graph", limit=10
        )
        assert [m.chunk_id for m in results] == ["c1"]

    def test_higher_term_frequency_scores_higher(self) -> None:
        index = InMemoryLexicalIndex()
        index.index_chunks(
            (
                _chunk("low", "r1", "docs", "graph mentioned once"),
                _chunk("high", "r1", "docs", "graph graph graph graph"),
            )
        )
        results = index.search(
            collection_id=None, resource_id=None, query="graph", limit=10
        )
        assert results[0].chunk_id == "high"

    def test_deterministic_tie_break_by_resource_then_sequence(self) -> None:
        index = InMemoryLexicalIndex()
        index.index_chunks(
            (
                _chunk("c-b-1", "resB", "docs", "graph", idx=1),
                _chunk("c-a-0", "resA", "docs", "graph", idx=0),
                _chunk("c-a-1", "resA", "docs", "graph", idx=1),
            )
        )
        results = index.search(
            collection_id=None, resource_id=None, query="graph", limit=10
        )
        assert [m.chunk_id for m in results] == ["c-a-0", "c-a-1", "c-b-1"]

    def test_repeated_search_is_stable(self) -> None:
        index = InMemoryLexicalIndex()
        index.index_chunks(
            (
                _chunk("c1", "r1", "docs", "alpha beta"),
                _chunk("c2", "r1", "docs", "alpha gamma", idx=1),
            )
        )
        first = index.search(
            collection_id=None, resource_id=None, query="alpha", limit=10
        )
        second = index.search(
            collection_id=None, resource_id=None, query="alpha", limit=10
        )
        assert first == second

    def test_limit_bounds_result_count(self) -> None:
        index = InMemoryLexicalIndex()
        index.index_chunks(
            tuple(_chunk(f"c{i}", "r1", "docs", "graph", idx=i) for i in range(20))
        )
        results = index.search(
            collection_id=None, resource_id=None, query="graph", limit=3
        )
        assert len(results) == 3

    def test_remove_resource_deletes_its_chunks_only(self) -> None:
        index = InMemoryLexicalIndex()
        index.index_chunks(
            (
                _chunk("c1", "r1", "docs", "graph networks"),
                _chunk("c2", "r2", "docs", "graph networks", idx=0),
            )
        )
        index.remove_resource("r1")
        results = index.search(
            collection_id=None, resource_id=None, query="graph", limit=10
        )
        assert [m.chunk_id for m in results] == ["c2"]

    def test_remove_unknown_resource_is_noop(self) -> None:
        index = InMemoryLexicalIndex()
        index.remove_resource("does-not-exist")  # must not raise

    def test_search_case_insensitive(self) -> None:
        index = InMemoryLexicalIndex()
        index.index_chunks((_chunk("c1", "r1", "docs", "Graph Neural Networks"),))
        results = index.search(
            collection_id=None, resource_id=None, query="GRAPH neural", limit=10
        )
        assert len(results) == 1
