"""The narrow interface to Phase 7 Knowledge.

Professional Intelligence does NOT parse documents itself. It reuses Knowledge's
pure ingestion pipeline (format check, restricted filename/metadata check,
parsing, secret detection over the extracted text, bounded chunking) and gets
back text with provenance. Nothing is stored in Knowledge: this adapter calls
only ``run_ingestion_pipeline`` (no store, no index, no permission engine), so
a private CV is never copied into a Knowledge collection.

Chunks are re-assembled into their original segments (page, section or row),
because a fixed-size window can cut a CV line in half and line-level parsing
needs whole lines. Chunks are contiguous with no overlap, so re-assembly is
exact and every line keeps the segment's page/section/paragraph provenance
plus its own character offsets.

This module can also read an owner-selected file from inside an explicit root
directory. It is for TRUSTED callers only: no HTTP route and no model-facing
tool can supply a path, and it rejects traversal, absolute paths and symlinks.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from sam.knowledge.chunker import ChunkerConfig
from sam.knowledge.ingestion import run_ingestion_pipeline
from sam.knowledge.models import (
    MAX_RESOURCE_SIZE_BYTES,
    DocumentChunk,
    IngestResourceRequest,
    KnowledgeErrorCategory,
    ResourceSourceKind,
    ResourceType,
)
from sam.knowledge.parser import default_parsers
from sam.permissions.models import Principal, PrincipalKind
from sam.professional.models import SourceLocation

_READER_PRINCIPAL = Principal(kind=PrincipalKind.SAM, id="professional-reader")
_COLLECTION = "professional-transient"
_CHUNKER = ChunkerConfig(max_chunk_characters=4_000, overlap_characters=0)
MAX_SOURCE_BYTES = MAX_RESOURCE_SIZE_BYTES


class ReadRejection(StrEnum):
    """Why a source was refused. Content-free, closed set."""

    SECRET_DETECTED = "secret_detected"
    UNSUPPORTED_TYPE = "unsupported_type"
    PARSING_ERROR = "parsing_error"
    NO_TEXT = "no_text"
    TOO_LARGE = "too_large"
    INVALID_SOURCE = "invalid_source"
    READ_ERROR = "read_error"


_CATEGORY = {
    KnowledgeErrorCategory.SECRET_DETECTED: ReadRejection.SECRET_DETECTED,
    KnowledgeErrorCategory.UNSUPPORTED_RESOURCE_TYPE: ReadRejection.UNSUPPORTED_TYPE,
    KnowledgeErrorCategory.PARSING_ERROR: ReadRejection.PARSING_ERROR,
    KnowledgeErrorCategory.EXTRACTION_ERROR: ReadRejection.NO_TEXT,
    KnowledgeErrorCategory.RESOURCE_TOO_LARGE: ReadRejection.TOO_LARGE,
    KnowledgeErrorCategory.CHUNKING_ERROR: ReadRejection.TOO_LARGE,
}


@dataclass(frozen=True)
class SourceSegment:
    """One page / section / row of the source with its provenance."""

    index: int
    text: str
    location: SourceLocation


@dataclass(frozen=True)
class SourceDocument:
    checksum: str
    resource_type: str
    segments: tuple[SourceSegment, ...]
    chunk_count: int


@dataclass(frozen=True)
class ReadOutcome:
    document: SourceDocument | None = None
    rejection: ReadRejection | None = None


class DocumentReader(Protocol):
    def read(self, *, name: str, resource_type: str, content: bytes) -> ReadOutcome: ...


def safe_label(name: str) -> str | None:
    """A display label that is a plain file name: no path separators, no
    traversal, no control characters. ``None`` if it is not."""

    label = name.strip()
    if (
        not label
        or len(label) > 200
        or "/" in label
        or "\\" in label
        or label in (".", "..")
        or ".." in label.split(os.sep)
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in label)
    ):
        return None
    return label


def _reassemble(chunks: tuple[DocumentChunk, ...]) -> tuple[SourceSegment, ...]:
    segments: list[SourceSegment] = []
    parts: list[str] = []
    current: SourceLocation | None = None

    def flush() -> None:
        if current is not None and parts:
            segments.append(SourceSegment(len(segments), "".join(parts), current))

    for chunk in sorted(chunks, key=lambda c: c.sequence_index):
        loc = chunk.location
        base = SourceLocation(
            page_number=loc.page_number,
            section_title=loc.section_title,
            paragraph_index=loc.paragraph_index,
        )
        starts_new = (
            current is None or base != current or loc.character_start in (0, None)
        )
        if starts_new:
            flush()
            parts = []
            current = base
        parts.append(chunk.text)
    flush()
    return tuple(segments)


class KnowledgePipelineReader:
    """``DocumentReader`` backed by Phase 7 Knowledge's pure pipeline."""

    def read(self, *, name: str, resource_type: str, content: bytes) -> ReadOutcome:
        label = safe_label(name)
        if label is None:
            return ReadOutcome(rejection=ReadRejection.INVALID_SOURCE)
        try:
            declared = ResourceType(resource_type)
        except ValueError:
            return ReadOutcome(rejection=ReadRejection.UNSUPPORTED_TYPE)
        if len(content) > MAX_SOURCE_BYTES:
            return ReadOutcome(rejection=ReadRejection.TOO_LARGE)
        try:
            request = IngestResourceRequest(
                principal=_READER_PRINCIPAL,
                collection_id=_COLLECTION,
                name=label,
                declared_resource_type=declared,
                source_kind=ResourceSourceKind.UPLOAD,
                source_label=label,
                content=content,
            )
        except (ValidationError, ValueError):
            return ReadOutcome(rejection=ReadRejection.INVALID_SOURCE)
        try:
            outcome = run_ingestion_pipeline(
                request,
                parsers=default_parsers(),
                chunker_config=_CHUNKER,
                now=datetime.now(UTC),
            )
        except Exception:
            return ReadOutcome(rejection=ReadRejection.READ_ERROR)
        if not outcome.accepted or outcome.resource is None:
            return ReadOutcome(
                rejection=_CATEGORY.get(
                    outcome.error_category, ReadRejection.PARSING_ERROR
                )
                if outcome.error_category is not None
                else ReadRejection.PARSING_ERROR
            )
        return ReadOutcome(
            document=SourceDocument(
                checksum=outcome.resource.checksum,
                resource_type=declared.value,
                segments=_reassemble(outcome.chunks),
                chunk_count=len(outcome.chunks),
            )
        )


class SourceFileError(Exception):
    """A file could not be read safely. ``code`` is a short, content-free reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def read_source_file(
    root: Path, relative_path: str, *, max_bytes: int = MAX_SOURCE_BYTES
) -> tuple[str, bytes]:
    """Read one regular file at ``relative_path`` INSIDE ``root``. Returns
    ``(label, bytes)``.

    Refuses: an absolute path, ``..`` traversal, a NUL, a symlink anywhere on the
    path (so a link can never lead outside ``root``), anything that is not a
    regular file, and files over ``max_bytes``."""

    if (
        not relative_path
        or "\x00" in relative_path
        or relative_path.startswith(("/", "~"))
        or Path(relative_path).is_absolute()
    ):
        raise SourceFileError("path_not_allowed")
    parts = Path(relative_path).parts
    if any(part in ("..", ".") for part in parts):
        raise SourceFileError("path_not_allowed")
    try:
        root_resolved = root.resolve(strict=True)
    except OSError:
        raise SourceFileError("root_unavailable") from None
    current = root_resolved
    for part in parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError:
            raise SourceFileError("not_found") from None
        if stat.S_ISLNK(info.st_mode):
            raise SourceFileError("symlink_not_allowed")
    try:
        final = current.resolve(strict=True)
        final.relative_to(root_resolved)
    except (OSError, ValueError):
        raise SourceFileError("path_not_allowed") from None
    info = current.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise SourceFileError("not_a_regular_file")
    if info.st_size > max_bytes:
        raise SourceFileError("too_large")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(current, flags)
    except OSError:
        raise SourceFileError("read_error") from None
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        raise SourceFileError("read_error") from None
    if len(data) > max_bytes:
        raise SourceFileError("too_large")
    label = safe_label(parts[-1])
    if label is None:
        raise SourceFileError("path_not_allowed")
    return label, data


__all__ = [
    "DocumentReader",
    "KnowledgePipelineReader",
    "ReadOutcome",
    "ReadRejection",
    "SourceDocument",
    "SourceFileError",
    "SourceSegment",
    "read_source_file",
    "safe_label",
]
