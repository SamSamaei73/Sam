"""Tests for sam.knowledge.chunker — deterministic, bounded chunking."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sam.knowledge.chunker import ChunkerConfig, chunk_document
from sam.knowledge.errors import ChunkingError
from sam.knowledge.models import MAX_CHUNKS_PER_RESOURCE, ParsedDocument, ParsedSegment
from sam.knowledge.models import ResourceType as RT

NOW = datetime.now(UTC)


def _doc(*segments: ParsedSegment) -> ParsedDocument:
    return ParsedDocument(resource_type=RT.TXT, segments=segments)


class TestChunkerConfig:
    def test_default_valid(self) -> None:
        ChunkerConfig()

    def test_overlap_equal_to_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChunkerConfig(max_chunk_characters=100, overlap_characters=100)

    def test_overlap_greater_than_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChunkerConfig(max_chunk_characters=100, overlap_characters=200)

    def test_zero_overlap_allowed(self) -> None:
        ChunkerConfig(max_chunk_characters=100, overlap_characters=0)

    def test_negative_overlap_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChunkerConfig(max_chunk_characters=100, overlap_characters=-1)

    def test_max_chunk_characters_floor(self) -> None:
        with pytest.raises(ValidationError):
            ChunkerConfig(max_chunk_characters=1)

    def test_max_chunk_characters_ceiling(self) -> None:
        with pytest.raises(ValidationError):
            ChunkerConfig(max_chunk_characters=100_000)


class TestChunkDocument:
    def test_never_creates_empty_chunks(self) -> None:
        doc = _doc(ParsedSegment(text="   \n\n  "))
        chunks = chunk_document(
            resource_id="r1",
            collection_id="docs",
            parsed=doc,
            config=ChunkerConfig(),
            now=NOW,
        )
        assert chunks == ()

    def test_single_short_segment_one_chunk(self) -> None:
        doc = _doc(ParsedSegment(text="hello world"))
        chunks = chunk_document(
            resource_id="r1",
            collection_id="docs",
            parsed=doc,
            config=ChunkerConfig(),
            now=NOW,
        )
        assert len(chunks) == 1
        assert chunks[0].text == "hello world"
        assert chunks[0].chunk_id == "r1:000000"

    def test_chunk_ids_deterministic_and_sequential(self) -> None:
        doc = _doc(ParsedSegment(text="x" * 250))
        config = ChunkerConfig(max_chunk_characters=100, overlap_characters=0)
        chunks = chunk_document(
            resource_id="r1", collection_id="docs", parsed=doc, config=config, now=NOW
        )
        assert [c.chunk_id for c in chunks] == ["r1:000000", "r1:000001", "r1:000002"]
        assert [c.sequence_index for c in chunks] == [0, 1, 2]

    def test_repeated_chunking_is_byte_identical(self) -> None:
        doc = _doc(ParsedSegment(text="abcdefgh" * 40, section_title="S"))
        config = ChunkerConfig(max_chunk_characters=50, overlap_characters=10)
        first = chunk_document(
            resource_id="r1", collection_id="docs", parsed=doc, config=config, now=NOW
        )
        second = chunk_document(
            resource_id="r1", collection_id="docs", parsed=doc, config=config, now=NOW
        )
        assert first == second

    def test_overlap_produces_overlapping_text(self) -> None:
        text = "0123456789" * 10  # 100 chars
        doc = _doc(ParsedSegment(text=text))
        config = ChunkerConfig(max_chunk_characters=60, overlap_characters=10)
        chunks = chunk_document(
            resource_id="r1", collection_id="docs", parsed=doc, config=config, now=NOW
        )
        assert chunks[0].text[-10:] == chunks[1].text[:10]

    def test_character_offsets_preserved(self) -> None:
        text = "0123456789" * 10
        doc = _doc(ParsedSegment(text=text))
        config = ChunkerConfig(max_chunk_characters=60, overlap_characters=0)
        chunks = chunk_document(
            resource_id="r1", collection_id="docs", parsed=doc, config=config, now=NOW
        )
        assert chunks[0].location.character_start == 0
        assert chunks[0].location.character_end == 60
        assert chunks[1].location.character_start == 60

    def test_chunk_never_spans_two_segments(self) -> None:
        doc = _doc(
            ParsedSegment(text="a" * 30, page_number=1),
            ParsedSegment(text="b" * 30, page_number=2),
        )
        config = ChunkerConfig(max_chunk_characters=100, overlap_characters=0)
        chunks = chunk_document(
            resource_id="r1", collection_id="docs", parsed=doc, config=config, now=NOW
        )
        assert len(chunks) == 2
        assert chunks[0].location.page_number == 1
        assert chunks[0].text == "a" * 30
        assert chunks[1].location.page_number == 2
        assert chunks[1].text == "b" * 30

    def test_page_number_propagated(self) -> None:
        doc = _doc(ParsedSegment(text="hello", page_number=7))
        chunks = chunk_document(
            resource_id="r1",
            collection_id="docs",
            parsed=doc,
            config=ChunkerConfig(),
            now=NOW,
        )
        assert chunks[0].location.page_number == 7
        assert chunks[0].location.section_title is None

    def test_section_title_propagated(self) -> None:
        doc = _doc(ParsedSegment(text="hello", section_title="Intro"))
        chunks = chunk_document(
            resource_id="r1",
            collection_id="docs",
            parsed=doc,
            config=ChunkerConfig(),
            now=NOW,
        )
        assert chunks[0].location.section_title == "Intro"
        assert chunks[0].location.page_number is None

    def test_paragraph_index_propagated(self) -> None:
        doc = _doc(ParsedSegment(text="hello", paragraph_index=3))
        chunks = chunk_document(
            resource_id="r1",
            collection_id="docs",
            parsed=doc,
            config=ChunkerConfig(),
            now=NOW,
        )
        assert chunks[0].location.paragraph_index == 3

    def test_no_fields_fabricated_for_plain_text(self) -> None:
        doc = _doc(ParsedSegment(text="hello"))
        chunks = chunk_document(
            resource_id="r1",
            collection_id="docs",
            parsed=doc,
            config=ChunkerConfig(),
            now=NOW,
        )
        loc = chunks[0].location
        assert loc.page_number is None
        assert loc.section_title is None
        assert loc.paragraph_index is None

    def test_empty_document_produces_no_chunks(self) -> None:
        doc = _doc()
        chunks = chunk_document(
            resource_id="r1",
            collection_id="docs",
            parsed=doc,
            config=ChunkerConfig(),
            now=NOW,
        )
        assert chunks == ()

    def test_exceeding_max_chunks_raises(self) -> None:
        # One char per chunk (huge overlap-free split) across a text long
        # enough to exceed MAX_CHUNKS_PER_RESOURCE.
        text = "a" * (MAX_CHUNKS_PER_RESOURCE * 50 + 1)
        doc = _doc(ParsedSegment(text=text))
        config = ChunkerConfig(max_chunk_characters=50, overlap_characters=0)
        with pytest.raises(ChunkingError):
            chunk_document(
                resource_id="r1",
                collection_id="docs",
                parsed=doc,
                config=config,
                now=NOW,
            )

    def test_windows_terminate_with_full_overlap_minus_one(self) -> None:
        # overlap = max_chunk_characters - 1 is the slowest-advancing
        # legal configuration; must still terminate in bounded steps.
        text = "x" * 300
        doc = _doc(ParsedSegment(text=text))
        config = ChunkerConfig(max_chunk_characters=50, overlap_characters=49)
        chunks = chunk_document(
            resource_id="r1", collection_id="docs", parsed=doc, config=config, now=NOW
        )
        assert len(chunks) > 0
        assert chunks[-1].location.character_end == 300
