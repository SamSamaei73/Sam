"""The pure ingestion pipeline: validate → checksum → parse → validate
content → chunk.

``run_ingestion_pipeline`` never touches ``KnowledgeStore`` or
``KnowledgeIndex`` — see ``sam.knowledge.engine.KnowledgeEngine.ingest``,
which is the only caller, and only invokes this (and the parser it
selects) after ``PermissionEngine.evaluate`` has already returned ALLOW.
Duplicate-content detection (a store read) is likewise the engine's
responsibility, not this module's — see that method's docstring.

Every stage fails closed: any recognized problem raises a typed
``KnowledgeError`` and this function converts it into a rejected
``IngestionOutcome`` — never a partially-built resource, never a
resource marked accepted with no chunks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sam.knowledge.chunker import ChunkerConfig, chunk_document
from sam.knowledge.errors import (
    ChunkingError,
    ExtractionError,
    KnowledgeError,
    ParsingError,
    ResourceTooLargeError,
    SecretDetectedError,
    UnsupportedResourceError,
)
from sam.knowledge.metadata import (
    is_restricted_content,
    is_restricted_filename,
    is_restricted_metadata,
)
from sam.knowledge.models import (
    MAX_EXTRACTED_TEXT_CHARS,
    DocumentChunk,
    IngestResourceRequest,
    KnowledgeErrorCategory,
    KnowledgeResource,
    new_id,
)
from sam.knowledge.parser import DocumentParser, select_parser
from sam.knowledge.sources import validate_declared_type


@dataclass(frozen=True)
class IngestionOutcome:
    """A pure computation result — ``accepted=True`` always carries a
    complete ``resource`` and at least one chunk; ``accepted=False``
    always carries neither, only an ``error_category``."""

    accepted: bool
    resource: KnowledgeResource | None
    chunks: tuple[DocumentChunk, ...]
    error_category: KnowledgeErrorCategory | None


def compute_checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def run_ingestion_pipeline(
    request: IngestResourceRequest,
    *,
    parsers: tuple[DocumentParser, ...],
    chunker_config: ChunkerConfig,
    now: datetime,
) -> IngestionOutcome:
    try:
        return _run_unsafe(
            request, parsers=parsers, chunker_config=chunker_config, now=now
        )
    except KnowledgeError as error:
        return IngestionOutcome(
            accepted=False,
            resource=None,
            chunks=(),
            error_category=_category_for(error),
        )


def _run_unsafe(
    request: IngestResourceRequest,
    *,
    parsers: tuple[DocumentParser, ...],
    chunker_config: ChunkerConfig,
    now: datetime,
) -> IngestionOutcome:
    validate_declared_type(request.content, request.declared_resource_type)

    if is_restricted_filename(request.source_label) or is_restricted_filename(
        request.name
    ):
        raise SecretDetectedError(
            "resource name/source looks like a secret-bearing filename"
        )
    if is_restricted_metadata(request.metadata):
        raise SecretDetectedError("resource metadata looks like a secret")

    parser = select_parser(request.declared_resource_type, parsers)
    parsed = parser.parse(request.content, request.metadata)

    if not parsed.has_extractable_text:
        raise ExtractionError("no extractable text found in this document")

    total_chars = sum(len(segment.text) for segment in parsed.segments)
    if total_chars > MAX_EXTRACTED_TEXT_CHARS:
        raise ResourceTooLargeError("extracted text exceeds the maximum allowed size")

    if _contains_secret(tuple(segment.text for segment in parsed.segments)):
        raise SecretDetectedError("document content looks like a secret")

    resource_id = new_id()
    chunks = chunk_document(
        resource_id=resource_id,
        collection_id=request.collection_id,
        parsed=parsed,
        config=chunker_config,
        now=now,
    )
    if not chunks:
        # Defensive backstop only: has_extractable_text already proved at
        # least one segment has non-blank text, so chunking it can never
        # legitimately produce zero chunks. Never reached in practice.
        raise ExtractionError("no extractable text found in this document")

    resource = KnowledgeResource(
        resource_id=resource_id,
        collection_id=request.collection_id,
        name=request.name,
        resource_type=request.declared_resource_type,
        source=request.source_label,
        source_kind=request.source_kind,
        size_bytes=len(request.content),
        checksum=compute_checksum(request.content),
        metadata=request.metadata,
        chunk_count=len(chunks),
        created_at=now,
        updated_at=now,
    )
    return IngestionOutcome(
        accepted=True, resource=resource, chunks=chunks, error_category=None
    )


def _contains_secret(texts: tuple[str, ...]) -> bool:
    """Secret scan over parsed segment texts, including across boundaries.

    Scanning each segment alone would miss a secret whose characters are
    split across adjacent segments (a PDF page break, a CSV row, a JSON
    element). So, in addition to every individual segment, the segments
    are scanned once as a single joined text, twice: joined with no
    separator (a token split mid-string) and joined with a newline (a
    phrase such as ``password is`` / value split across a break).

    This is bounded: the caller has already rejected any document whose
    extracted text exceeds ``MAX_EXTRACTED_TEXT_CHARS``, so the joined
    text is at most that size plus one separator per segment. The detector
    itself is unchanged and no matched text is logged or returned — only a
    boolean. Joining is deliberately conservative (fail closed): it can
    over-reject text whose adjacent segments happen to form a secret-shaped
    string, never under-reject a split secret.
    """

    if any(is_restricted_content(text) for text in texts):
        return True
    if len(texts) < 2:
        return False
    return is_restricted_content("".join(texts)) or is_restricted_content(
        "\n".join(texts)
    )


def _category_for(error: KnowledgeError) -> KnowledgeErrorCategory:
    if isinstance(error, UnsupportedResourceError):
        return KnowledgeErrorCategory.UNSUPPORTED_RESOURCE_TYPE
    if isinstance(error, ResourceTooLargeError):
        return KnowledgeErrorCategory.RESOURCE_TOO_LARGE
    if isinstance(error, ParsingError):
        return KnowledgeErrorCategory.PARSING_ERROR
    if isinstance(error, ExtractionError):
        return KnowledgeErrorCategory.EXTRACTION_ERROR
    if isinstance(error, ChunkingError):
        return KnowledgeErrorCategory.CHUNKING_ERROR
    if isinstance(error, SecretDetectedError):
        return KnowledgeErrorCategory.SECRET_DETECTED
    return KnowledgeErrorCategory.INTERNAL_ERROR


__all__ = ["IngestionOutcome", "compute_checksum", "run_ingestion_pipeline"]
