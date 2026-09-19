"""Typed, immutable domain models for the Knowledge Layer.

Depends only on the standard library, Pydantic, and
``sam.permissions.models`` (``Principal``, ``RiskLevel`` — reused, not
duplicated, the same choice every prior phase makes). Nothing here
performs I/O, parsing, or process execution.

This module deliberately never imports ``sam.memory`` anything. The
Knowledge Layer is not Memory (see ``docs/knowledge.md``): a
``KnowledgeResource`` never becomes a ``MemoryCandidate``, automatically
or otherwise.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sam.permissions.models import Principal, RiskLevel

# --------------------------------------------------------------------- #
# Bounds — every unbounded operation the task calls out has an explicit,
# validated limit here. See docs/knowledge.md for the rationale behind
# each value.
# --------------------------------------------------------------------- #

MAX_COLLECTION_ID_LENGTH = 200
MAX_RESOURCE_ID_LENGTH = 100
MAX_NAME_LENGTH = 300
MAX_SOURCE_LABEL_LENGTH = 500
MAX_REASON_LENGTH = 500
MAX_TITLE_LENGTH = 300
MAX_AUTHOR_LENGTH = 300
MAX_LANGUAGE_LENGTH = 20
MAX_METADATA_ENTRIES = 20
MAX_METADATA_KEY_LENGTH = 100
MAX_METADATA_VALUE_LENGTH = 500
MAX_SECTION_TITLE_LENGTH = 300

MAX_CHUNK_CHARACTERS_LIMIT = 8_000
MIN_CHUNK_CHARACTERS = 50
MAX_OVERLAP_CHARACTERS_LIMIT = 4_000
MAX_CHUNKS_PER_RESOURCE = 5_000

MAX_RESOURCE_SIZE_BYTES = 20_000_000
MAX_EXTRACTED_TEXT_CHARS = 2_000_000

MAX_RETRIEVAL_RESULTS = 50
MAX_QUERY_LENGTH = 1_000
MAX_HASH_LENGTH = 64  # sha256 hex digest length

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def utc_now() -> datetime:
    """The single clock function used throughout the Knowledge domain."""

    return datetime.now(UTC)


def new_id() -> str:
    return uuid4().hex


def sanitize_display_text(value: str | None, *, max_length: int) -> str | None:
    """Clean and truncate free-text *display-only* fields — never used
    for identity, deduplication, or authorization decisions."""

    if value is None:
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()
    if not cleaned:
        return None
    return cleaned[:max_length]


def _clean_required_text(value: str) -> str:
    return _CONTROL_CHARS.sub("", value).strip()


def _require_tz(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value


# --------------------------------------------------------------------- #
# Resource identity
# --------------------------------------------------------------------- #


class ResourceType(StrEnum):
    """The closed set of formats Phase 7 knows how to parse.

    An unlisted type cannot even be requested — see
    ``sam.knowledge.sources`` for how a caller's declared type is further
    cross-checked against what the content actually looks like.
    """

    PDF = "pdf"
    TXT = "txt"
    MARKDOWN = "markdown"
    JSON = "json"
    CSV = "csv"


class ResourceSourceKind(StrEnum):
    """How the raw bytes were provided — descriptive only, never a fetch
    instruction. Nothing in this package ever fetches a URL or reads an
    arbitrary filesystem path on its own initiative; the caller always
    supplies the bytes directly."""

    UPLOAD = "upload"
    LOCAL_FILE = "local_file"
    INLINE_TEXT = "inline_text"


class KnowledgeOperation(StrEnum):
    """The closed set of operations this phase implements — exactly the
    five methods on ``KnowledgeEngine``, no others."""

    INGEST_RESOURCE = "ingest_resource"
    GET_RESOURCE = "get_resource"
    LIST_RESOURCES = "list_resources"
    RETRIEVE = "retrieve"
    REMOVE_RESOURCE = "remove_resource"


class KnowledgeCollection(BaseModel):
    """A namespace resources are isolated by. Created implicitly on first
    use — see ``sam.knowledge.store`` — never assumed to be one of a
    hard-coded set of categories."""

    model_config = ConfigDict(frozen=True)

    collection_id: str = Field(min_length=1, max_length=MAX_COLLECTION_ID_LENGTH)
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    created_at: datetime

    @field_validator("collection_id", "name", mode="before")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _clean_required_text(value)

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _require_tz(value)


class DocumentMetadata(BaseModel):
    """Caller-supplied descriptive metadata. Never a place to put secrets
    — ``sam.knowledge.metadata`` rejects ingestion outright if any value
    here looks like a secret, the same discipline as document content."""

    model_config = ConfigDict(frozen=True)

    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    author: str | None = Field(default=None, max_length=MAX_AUTHOR_LENGTH)
    language: str | None = Field(default=None, max_length=MAX_LANGUAGE_LENGTH)
    custom: dict[str, str] = Field(default_factory=dict)

    @field_validator("title", "author", "language", mode="before")
    @classmethod
    def _clean_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_display_text(value, max_length=MAX_TITLE_LENGTH)

    @field_validator("custom")
    @classmethod
    def _validate_custom(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_METADATA_ENTRIES:
            raise ValueError(f"metadata must not exceed {MAX_METADATA_ENTRIES} entries")
        cleaned: dict[str, str] = {}
        for key, entry in value.items():
            clean_key = _clean_required_text(key)[:MAX_METADATA_KEY_LENGTH]
            clean_value = _clean_required_text(entry)[:MAX_METADATA_VALUE_LENGTH]
            if not clean_key:
                raise ValueError("metadata keys must not be blank")
            cleaned[clean_key] = clean_value
        return cleaned


class KnowledgeResource(BaseModel):
    """One ingested document's identity and descriptive metadata — never
    its content, which lives only in its chunks (see ``DocumentChunk``)."""

    model_config = ConfigDict(frozen=True)

    resource_id: str = Field(min_length=1, max_length=MAX_RESOURCE_ID_LENGTH)
    collection_id: str = Field(min_length=1, max_length=MAX_COLLECTION_ID_LENGTH)
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    resource_type: ResourceType
    source: str = Field(min_length=1, max_length=MAX_SOURCE_LABEL_LENGTH)
    source_kind: ResourceSourceKind
    size_bytes: int = Field(ge=0)
    checksum: str = Field(min_length=1, max_length=MAX_HASH_LENGTH)
    metadata: DocumentMetadata
    chunk_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime

    @field_validator("resource_id", "collection_id", "name", "source", mode="before")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _clean_required_text(value)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _require_tz(value)


# --------------------------------------------------------------------- #
# Parsing — see sam.knowledge.parser
# --------------------------------------------------------------------- #


class ParsedSegment(BaseModel):
    """One structural unit a ``DocumentParser`` produces: a PDF page, a
    Markdown section, a JSON array element, a CSV row, or (for TXT) the
    whole document. Never invented — a field is only ever set when the
    source format genuinely provides it; see ``sam.knowledge.chunker``."""

    model_config = ConfigDict(frozen=True)

    text: str
    page_number: int | None = Field(default=None, ge=1)
    section_title: str | None = Field(default=None, max_length=MAX_SECTION_TITLE_LENGTH)
    paragraph_index: int | None = Field(default=None, ge=0)

    @field_validator("section_title", mode="before")
    @classmethod
    def _clean_section_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_display_text(value, max_length=MAX_SECTION_TITLE_LENGTH)


class ParsedDocument(BaseModel):
    """Structured parser output — produced before any chunking or
    indexing decision. ``has_extractable_text`` is computed, never
    trusted from the parser directly, so a parser cannot itself claim
    success for a document with no real content."""

    model_config = ConfigDict(frozen=True)

    resource_type: ResourceType
    segments: tuple[ParsedSegment, ...]

    @property
    def has_extractable_text(self) -> bool:
        return any(segment.text.strip() for segment in self.segments)


# --------------------------------------------------------------------- #
# Chunking — see sam.knowledge.chunker
# --------------------------------------------------------------------- #


class ChunkLocation(BaseModel):
    """Everything known about where a chunk came from. Every field is
    independently optional — a format that cannot supply a given field
    (a JSON document has no ``page_number``) must leave it ``None``,
    never a fabricated value."""

    model_config = ConfigDict(frozen=True)

    page_number: int | None = Field(default=None, ge=1)
    section_title: str | None = Field(default=None, max_length=MAX_SECTION_TITLE_LENGTH)
    paragraph_index: int | None = Field(default=None, ge=0)
    character_start: int | None = Field(default=None, ge=0)
    character_end: int | None = Field(default=None, ge=0)

    @field_validator("section_title", mode="before")
    @classmethod
    def _clean_section_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_display_text(value, max_length=MAX_SECTION_TITLE_LENGTH)

    @model_validator(mode="after")
    def _validate_offsets(self) -> Self:
        if (
            self.character_start is not None
            and self.character_end is not None
            and self.character_end < self.character_start
        ):
            raise ValueError("character_end must not be before character_start")
        return self


class DocumentChunk(BaseModel):
    """One bounded, indexed unit of a resource's extracted text.
    ``chunk_id`` is deterministic (``f"{resource_id}:{sequence_index}"``)
    — never random — so re-ingesting identical content with identical
    chunking configuration reproduces identical chunk ids."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(min_length=1, max_length=200)
    resource_id: str = Field(min_length=1, max_length=MAX_RESOURCE_ID_LENGTH)
    collection_id: str = Field(min_length=1, max_length=MAX_COLLECTION_ID_LENGTH)
    sequence_index: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=MAX_CHUNK_CHARACTERS_LIMIT)
    location: ChunkLocation
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _require_tz(value)


# --------------------------------------------------------------------- #
# Retrieval — see sam.knowledge.retrieval
# --------------------------------------------------------------------- #


class RetrievalQuery(BaseModel):
    """One bounded lexical retrieval request. Pure data — no I/O."""

    model_config = ConfigDict(frozen=True)

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    collection_id: str | None = Field(default=None, max_length=MAX_COLLECTION_ID_LENGTH)
    resource_id: str | None = Field(default=None, max_length=MAX_RESOURCE_ID_LENGTH)
    top_k: int = Field(default=10, ge=1, le=MAX_RETRIEVAL_RESULTS)

    @field_validator("query", mode="before")
    @classmethod
    def _clean_query(cls, value: str) -> str:
        cleaned = _clean_required_text(value)
        if not cleaned:
            raise ValueError("query must not be blank")
        return cleaned

    @field_validator("collection_id", "resource_id", mode="before")
    @classmethod
    def _clean_optional_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _clean_required_text(value)
        return cleaned or None


class RetrievalResult(BaseModel):
    """A retrieved chunk, always carrying its score and full source
    identity — never raw text alone. See docs/knowledge.md's citation
    model."""

    model_config = ConfigDict(frozen=True)

    chunk: DocumentChunk
    score: float = Field(ge=0)
    resource: KnowledgeResource
    location: ChunkLocation


# --------------------------------------------------------------------- #
# Outcomes — shared vocabulary across every KnowledgeEngine method
# --------------------------------------------------------------------- #


class ExecutionOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class PermissionOutcomeSummary(StrEnum):
    """A minimal echo of ``sam.permissions.models.DecisionOutcome`` — its
    own type for the same reason every prior phase keeps one."""

    ALLOW = "allow"
    CONFIRM_REQUIRED = "confirm_required"
    DENY = "deny"


class KnowledgeErrorCategory(StrEnum):
    """Closed set of failure categories — never a raw exception message."""

    PERMISSION_DENIED = "permission_denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_INVALID = "confirmation_invalid"
    VALIDATION_ERROR = "validation_error"
    UNSUPPORTED_RESOURCE_TYPE = "unsupported_resource_type"
    RESOURCE_TOO_LARGE = "resource_too_large"
    PARSING_ERROR = "parsing_error"
    EXTRACTION_ERROR = "extraction_error"
    CHUNKING_ERROR = "chunking_error"
    SECRET_DETECTED = "secret_detected"
    RESOURCE_NOT_FOUND = "resource_not_found"
    INDEXING_ERROR = "indexing_error"
    STORAGE_ERROR = "storage_error"
    RETRIEVAL_ERROR = "retrieval_error"
    DUPLICATE_RESOURCE = "duplicate_resource"
    INTERNAL_ERROR = "internal_error"


class IngestionStatus(StrEnum):
    SUCCESS = "success"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    FAILED = "failed"


class IngestionResult(BaseModel):
    """What ``KnowledgeEngine.ingest`` always returns. ``SUCCESS`` implies
    a persisted ``resource`` with at least one chunk — never a vacuous
    success for content that produced nothing. Every other status carries
    no ``resource``: a failed ingestion is never reported as ingested.

    Persistence is not atomic. A failure after persistence begins triggers
    a *compensating* cleanup of both the index and the store; if that
    cleanup itself fails, ``rollback_incomplete`` is ``True`` and the
    engine quarantines the resource id so it stays invisible to
    get/list/retrieve (see ``sam.knowledge.engine``)."""

    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(min_length=1, max_length=100)
    principal: Principal
    permission_outcome: PermissionOutcomeSummary
    status: IngestionStatus
    resource: KnowledgeResource | None = None
    chunk_count: int = Field(default=0, ge=0)
    duplicate_of: str | None = Field(default=None, max_length=MAX_RESOURCE_ID_LENGTH)
    error_category: KnowledgeErrorCategory | None = None
    confirmation_id: str | None = None
    rollback_incomplete: bool = False
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _require_tz(value)

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.rollback_incomplete and self.status is not IngestionStatus.FAILED:
            raise ValueError("rollback_incomplete is only valid on a FAILED ingestion")
        if self.status is IngestionStatus.SUCCESS:
            if self.resource is None or self.chunk_count < 1:
                raise ValueError("a SUCCESS ingestion must carry a resource and chunks")
            if self.error_category is not None:
                raise ValueError("a SUCCESS ingestion must not carry an error_category")
        else:
            if self.resource is not None:
                raise ValueError("a non-SUCCESS ingestion must not carry a resource")
        if self.status is IngestionStatus.DUPLICATE and self.duplicate_of is None:
            raise ValueError("a DUPLICATE ingestion must name the existing resource")
        if self.status in (IngestionStatus.REJECTED, IngestionStatus.FAILED):
            if self.error_category is None:
                raise ValueError(
                    f"a {self.status} ingestion must carry an error_category"
                )
        return self


class ResourceQueryResult(BaseModel):
    """What ``KnowledgeEngine.get_resource`` always returns."""

    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(min_length=1, max_length=100)
    principal: Principal
    permission_outcome: PermissionOutcomeSummary
    outcome: ExecutionOutcome
    resource: KnowledgeResource | None = None
    error_category: KnowledgeErrorCategory | None = None
    confirmation_id: str | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _require_tz(value)

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.outcome is ExecutionOutcome.FAILED and self.error_category is None:
            raise ValueError("a FAILED result must carry an error_category")
        if self.outcome is ExecutionOutcome.SUCCESS and self.error_category is not None:
            raise ValueError("a SUCCESS result must not carry an error_category")
        return self


class ResourceListResult(BaseModel):
    """What ``KnowledgeEngine.list_resources`` always returns."""

    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(min_length=1, max_length=100)
    principal: Principal
    permission_outcome: PermissionOutcomeSummary
    outcome: ExecutionOutcome
    resources: tuple[KnowledgeResource, ...] = ()
    error_category: KnowledgeErrorCategory | None = None
    confirmation_id: str | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _require_tz(value)

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.outcome is ExecutionOutcome.FAILED and self.error_category is None:
            raise ValueError("a FAILED result must carry an error_category")
        if self.outcome is ExecutionOutcome.SUCCESS and self.error_category is not None:
            raise ValueError("a SUCCESS result must not carry an error_category")
        return self


class RetrievalOutcome(BaseModel):
    """What ``KnowledgeEngine.retrieve`` always returns."""

    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(min_length=1, max_length=100)
    principal: Principal
    permission_outcome: PermissionOutcomeSummary
    outcome: ExecutionOutcome
    results: tuple[RetrievalResult, ...] = ()
    error_category: KnowledgeErrorCategory | None = None
    confirmation_id: str | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _require_tz(value)

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.outcome is ExecutionOutcome.FAILED and self.error_category is None:
            raise ValueError("a FAILED result must carry an error_category")
        if self.outcome is ExecutionOutcome.SUCCESS and self.error_category is not None:
            raise ValueError("a SUCCESS result must not carry an error_category")
        return self


class RemovalResult(BaseModel):
    """What ``KnowledgeEngine.remove_resource`` always returns."""

    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(min_length=1, max_length=100)
    principal: Principal
    permission_outcome: PermissionOutcomeSummary
    outcome: ExecutionOutcome
    removed: bool = False
    error_category: KnowledgeErrorCategory | None = None
    confirmation_id: str | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _require_tz(value)

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.outcome is ExecutionOutcome.FAILED and self.error_category is None:
            raise ValueError("a FAILED result must carry an error_category")
        if self.outcome is ExecutionOutcome.SUCCESS and self.error_category is not None:
            raise ValueError("a SUCCESS result must not carry an error_category")
        if self.outcome is ExecutionOutcome.SUCCESS and not self.removed:
            raise ValueError("a SUCCESS removal must have actually removed something")
        return self


# --------------------------------------------------------------------- #
# Requests — one typed model per KnowledgeEngine method
# --------------------------------------------------------------------- #


class _RequestBase(BaseModel):
    model_config = ConfigDict(frozen=True)

    principal: Principal
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)

    @field_validator("reason", mode="before")
    @classmethod
    def _clean_reason(cls, value: str | None) -> str | None:
        return sanitize_display_text(value, max_length=MAX_REASON_LENGTH)


class IngestResourceRequest(_RequestBase):
    collection_id: str = Field(min_length=1, max_length=MAX_COLLECTION_ID_LENGTH)
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    declared_resource_type: ResourceType
    source_kind: ResourceSourceKind
    source_label: str = Field(min_length=1, max_length=MAX_SOURCE_LABEL_LENGTH)
    content: bytes = Field(max_length=MAX_RESOURCE_SIZE_BYTES)
    metadata: DocumentMetadata = Field(default_factory=DocumentMetadata)

    @field_validator("collection_id", "name", "source_label", mode="before")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _clean_required_text(value)

    @field_validator("content")
    @classmethod
    def _require_content(cls, value: bytes) -> bytes:
        if not value:
            raise ValueError("content must not be empty")
        return value


class GetResourceRequest(_RequestBase):
    collection_id: str = Field(min_length=1, max_length=MAX_COLLECTION_ID_LENGTH)
    resource_id: str = Field(min_length=1, max_length=MAX_RESOURCE_ID_LENGTH)

    @field_validator("collection_id", "resource_id", mode="before")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _clean_required_text(value)


class ListResourcesRequest(_RequestBase):
    collection_id: str = Field(min_length=1, max_length=MAX_COLLECTION_ID_LENGTH)

    @field_validator("collection_id", mode="before")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _clean_required_text(value)


class RetrieveRequest(_RequestBase):
    query: RetrievalQuery


class RemoveResourceRequest(_RequestBase):
    collection_id: str = Field(min_length=1, max_length=MAX_COLLECTION_ID_LENGTH)
    resource_id: str = Field(min_length=1, max_length=MAX_RESOURCE_ID_LENGTH)

    @field_validator("collection_id", "resource_id", mode="before")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _clean_required_text(value)


KnowledgeOperationRequest = (
    IngestResourceRequest
    | GetResourceRequest
    | ListResourcesRequest
    | RetrieveRequest
    | RemoveResourceRequest
)


# --------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------- #


class KnowledgeConfirmationOutcome(StrEnum):
    DENIED = "denied"
    CONFIRM_REQUIRED = "confirm_required"


class KnowledgeAuditEvent(BaseModel):
    """One immutable, content-free record of a Knowledge operation. Never
    carries document text, chunk text, query text, or raw metadata
    values — only closed enums, ids, and bounded counts."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=100)
    occurred_at: datetime
    operation_id: str = Field(min_length=1, max_length=100)
    principal: Principal
    collection_id: str = Field(max_length=MAX_COLLECTION_ID_LENGTH)
    resource_id: str | None = Field(default=None, max_length=MAX_RESOURCE_ID_LENGTH)
    operation: KnowledgeOperation
    risk: RiskLevel
    permission_outcome: PermissionOutcomeSummary
    confirmation_outcome: KnowledgeConfirmationOutcome | None = None
    execution_outcome: ExecutionOutcome | None = None
    error_category: KnowledgeErrorCategory | None = None
    resource_type: ResourceType | None = None
    chunk_count: int | None = Field(default=None, ge=0)
    result_count: int | None = Field(default=None, ge=0)
    rollback_incomplete: bool = False

    @field_validator("occurred_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("audit timestamps must be timezone-aware")
        return value


__all__ = [
    "ChunkLocation",
    "DocumentChunk",
    "DocumentMetadata",
    "ExecutionOutcome",
    "GetResourceRequest",
    "IngestResourceRequest",
    "IngestionResult",
    "IngestionStatus",
    "KnowledgeAuditEvent",
    "KnowledgeCollection",
    "KnowledgeConfirmationOutcome",
    "KnowledgeErrorCategory",
    "KnowledgeOperation",
    "KnowledgeOperationRequest",
    "KnowledgeResource",
    "ListResourcesRequest",
    "ParsedDocument",
    "ParsedSegment",
    "PermissionOutcomeSummary",
    "Principal",
    "RemovalResult",
    "RemoveResourceRequest",
    "ResourceListResult",
    "ResourceQueryResult",
    "ResourceSourceKind",
    "ResourceType",
    "RetrievalOutcome",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrieveRequest",
    "RiskLevel",
    "new_id",
    "sanitize_display_text",
    "utc_now",
]
