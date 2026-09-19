"""Deterministic, bounded chunking.

Turns a ``ParsedDocument`` into a finite sequence of ``DocumentChunk``.
Pure and deterministic — no randomness, no LLM call, no I/O. The same
``ParsedDocument`` and ``ChunkerConfig`` always produce byte-identical
chunk text, offsets, and ids.

A chunk never spans more than one ``ParsedSegment`` — a PDF chunk never
crosses a page boundary, a Markdown chunk never crosses a section
boundary — so ``sam.knowledge.parser``'s per-segment provenance
(``page_number``/``section_title``/``paragraph_index``) always applies
to the chunk as a whole, never to only part of it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sam.knowledge.errors import ChunkingError
from sam.knowledge.models import (
    MAX_CHUNK_CHARACTERS_LIMIT,
    MAX_CHUNKS_PER_RESOURCE,
    MAX_OVERLAP_CHARACTERS_LIMIT,
    MIN_CHUNK_CHARACTERS,
    ChunkLocation,
    DocumentChunk,
    ParsedDocument,
)


class ChunkerConfig(BaseModel):
    """Bounded, validated chunking configuration.

    ``overlap_characters`` may be ``0`` (overlap is optional); when
    positive it must be strictly less than ``max_chunk_characters`` —
    otherwise a window would never advance and chunking could not
    terminate.
    """

    model_config = ConfigDict(frozen=True)

    max_chunk_characters: int = Field(
        default=1_000, ge=MIN_CHUNK_CHARACTERS, le=MAX_CHUNK_CHARACTERS_LIMIT
    )
    overlap_characters: int = Field(default=0, ge=0, le=MAX_OVERLAP_CHARACTERS_LIMIT)

    @model_validator(mode="after")
    def _validate_overlap(self) -> Self:
        if self.overlap_characters >= self.max_chunk_characters:
            raise ValueError(
                "overlap_characters must be less than max_chunk_characters"
            )
        return self


def _windows(length: int, max_chars: int, overlap: int) -> Iterator[tuple[int, int]]:
    if length == 0:
        return
    step = max_chars - overlap
    start = 0
    while start < length:
        end = min(start + max_chars, length)
        yield start, end
        if end >= length:
            return
        start += step


def chunk_document(
    *,
    resource_id: str,
    collection_id: str,
    parsed: ParsedDocument,
    config: ChunkerConfig,
    now: datetime,
) -> tuple[DocumentChunk, ...]:
    """Bound and split every segment's text into chunks, in order.

    Raises ``ChunkingError`` (never truncates silently) if the number of
    chunks a document would produce exceeds ``MAX_CHUNKS_PER_RESOURCE`` —
    a document that large must be rejected, not partially learned.
    """

    chunks: list[DocumentChunk] = []
    sequence_index = 0
    for segment in parsed.segments:
        for start, end in _windows(
            len(segment.text), config.max_chunk_characters, config.overlap_characters
        ):
            text = segment.text[start:end]
            if not text.strip():
                continue
            if sequence_index >= MAX_CHUNKS_PER_RESOURCE:
                raise ChunkingError(
                    f"document would produce more than {MAX_CHUNKS_PER_RESOURCE} chunks"
                )
            location = ChunkLocation(
                page_number=segment.page_number,
                section_title=segment.section_title,
                paragraph_index=segment.paragraph_index,
                character_start=start,
                character_end=end,
            )
            chunks.append(
                DocumentChunk(
                    chunk_id=f"{resource_id}:{sequence_index:06d}",
                    resource_id=resource_id,
                    collection_id=collection_id,
                    sequence_index=sequence_index,
                    text=text,
                    location=location,
                    created_at=now,
                )
            )
            sequence_index += 1
    return tuple(chunks)


__all__ = ["ChunkerConfig", "chunk_document"]
