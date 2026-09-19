"""Typed, immutable domain models for the Memory Engine.

Depends only on the standard library, Pydantic, and
``sam.permissions.models.Principal`` — reused rather than duplicated, since
memory ownership and permission-grant ownership are the same identity
concept. Nothing here depends on FastAPI, a database driver, or any
storage implementation: that keeps ``MemoryEngine`` swappable between the
in-memory Phase 4 store and a future cloud store without changing this
module. No cross-dependency runs the other way — ``sam.permissions`` does
not import from ``sam.memory``.

Design note, mirroring ``sam.permissions.models``: ``MemoryType`` and
``MemorySource`` are closed enums, not free strings, so an invalid value
cannot be constructed into a candidate at all.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sam.permissions.models import Principal

_MAX_CONTENT_LENGTH = 2_000
_MAX_REASON_LENGTH = 500
_MAX_TAGS = 10
_MAX_TAG_LENGTH = 40
_MAX_METADATA_KEYS = 20
_MAX_METADATA_KEY_LENGTH = 50
_MAX_METADATA_VALUE_LENGTH = 200
_MAX_PROJECT_ID_LENGTH = 100
_MAX_SOURCE_LENGTH = 100
_MAX_ID_LENGTH = 100
_MAX_WORKING_KEY_LENGTH = 100
_MIN_MEANINGFUL_CONTENT_LENGTH = 3

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def utc_now() -> datetime:
    """The single clock function used throughout the memory domain."""

    return datetime.now(UTC)


def _strip_control_characters(value: str) -> str:
    return _CONTROL_CHARS.sub("", value)


def normalize_for_comparison(text: str) -> str:
    """A deterministic normal form used for exact-duplicate and
    exact-match comparisons: Unicode NFC, control characters stripped,
    lowercased, internal whitespace collapsed, and trimmed.

    This is *not* semantic normalization — two paraphrases of the same
    fact will not normalize to the same string. See
    ``docs/memory.md`` for why Phase 4 stays deterministic rather than
    pretending to do semantic matching.
    """

    normalized = unicodedata.normalize("NFC", text)
    normalized = _strip_control_characters(normalized).lower()
    return " ".join(normalized.split())


class MemoryType(StrEnum):
    """The four memory categories Phase 4 implements."""

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROJECT = "project"


class MemorySource(StrEnum):
    """Where a memory candidate originated.

    Source alone never determines trust — see ``MemoryConfidence`` and
    ``sam.memory.policy``. An ``AGENT``-sourced candidate is never
    automatically treated as an authoritative user preference.
    """

    USER_EXPLICIT = "user_explicit"
    USER_CONVERSATION = "user_conversation"
    SYSTEM = "system"
    AGENT = "agent"
    TOOL = "tool"
    IMPORT = "import"


class MemoryConfidence(StrEnum):
    """The minimal trust distinction Phase 4 makes: stated vs. inferred.

    Deliberately not a numeric confidence score or a verification-state
    machine — "do not invent an unnecessarily complicated epistemic
    system" (Phase 4 task). This is the one distinction that actually
    drives a policy decision: only ``EXPLICIT`` claims from a
    ``USER_EXPLICIT`` source are eligible for automatic storage.
    """

    EXPLICIT = "explicit"
    INFERRED = "inferred"


class MemoryImportance(StrEnum):
    """A closed, deterministic importance tier used in retrieval ranking."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


_IMPORTANCE_WEIGHT: dict[MemoryImportance, float] = {
    MemoryImportance.LOW: 0.0,
    MemoryImportance.NORMAL: 0.5,
    MemoryImportance.HIGH: 1.0,
}


def importance_weight(importance: MemoryImportance) -> float:
    """The deterministic 0..1 weight used by retrieval ranking."""

    return _IMPORTANCE_WEIGHT[importance]


class MemoryStatus(StrEnum):
    """Lifecycle state of a stored, long-term memory.

    Deletion (see ``MemoryStore.delete``) removes a record outright —
    unlike a Phase 3 permission grant, a memory the user asked Sam to
    "forget" must actually be forgotten, not merely marked inactive.
    ``ARCHIVED`` is a separate, optional lifecycle step for memories that
    should stop appearing in normal retrieval without being deleted.
    """

    ACTIVE = "active"
    ARCHIVED = "archived"


def _sanitize_text(value: str | None, *, max_length: int) -> str | None:
    """Clean and truncate free-text *display-only* fields (e.g. ``reason``).

    Truncation is safe here because these fields are never used for
    identity, equality, or isolation checks — only shown to a human.
    """

    if value is None:
        return None
    cleaned = _strip_control_characters(value).strip()
    if not cleaned:
        return None
    return cleaned[:max_length]


def _clean_required_text(value: str) -> str:
    """Strip control characters/whitespace without truncating.

    Used for fields whose length is enforced by a hard ``Field`` bound
    instead (content, identifiers): silently truncating an identifier
    could collide two distinct values, and silently truncating a stored
    fact could quietly corrupt it. Both must fail loudly instead.
    """

    return _strip_control_characters(value).strip()


def _clean_optional_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _strip_control_characters(value).strip()
    return cleaned or None


def _clean_tags(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(value) > _MAX_TAGS:
        raise ValueError(f"at most {_MAX_TAGS} tags are allowed")
    cleaned: list[str] = []
    for tag in value:
        stripped = _strip_control_characters(tag).strip()
        if not stripped:
            raise ValueError("tags must not be blank")
        if len(stripped) > _MAX_TAG_LENGTH:
            raise ValueError(f"a tag must be at most {_MAX_TAG_LENGTH} chars")
        cleaned.append(stripped)
    return tuple(cleaned)


def _clean_metadata(value: dict[str, str]) -> dict[str, str]:
    if len(value) > _MAX_METADATA_KEYS:
        raise ValueError(f"at most {_MAX_METADATA_KEYS} metadata keys allowed")
    for key, val in value.items():
        if not key.strip() or len(key) > _MAX_METADATA_KEY_LENGTH:
            raise ValueError("metadata keys must be non-blank and bounded")
        if len(val) > _MAX_METADATA_VALUE_LENGTH:
            raise ValueError("metadata values must be bounded")
    return value


class Memory(BaseModel):
    """One immutable, stored long-term memory record.

    Immutable by replacement: ``MemoryStore.update``/``.delete`` never
    mutate a ``Memory`` instance in place, they replace or remove the
    stored record — the same discipline as ``sam.permissions.models``.
    """

    model_config = ConfigDict(frozen=True)

    memory_id: str = Field(min_length=1, max_length=_MAX_ID_LENGTH)
    memory_type: MemoryType
    principal: Principal
    content: str = Field(min_length=1, max_length=_MAX_CONTENT_LENGTH)
    source: MemorySource
    confidence: MemoryConfidence
    project_id: str | None = Field(default=None, max_length=_MAX_PROJECT_ID_LENGTH)
    tags: tuple[str, ...] = Field(default_factory=tuple)
    metadata: dict[str, str] = Field(default_factory=dict)
    importance: MemoryImportance = MemoryImportance.NORMAL
    status: MemoryStatus = MemoryStatus.ACTIVE
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("content", mode="before")
    @classmethod
    def _clean_content(cls, value: str) -> str:
        return _clean_required_text(value)

    @field_validator("project_id", mode="before")
    @classmethod
    def _clean_project_id(cls, value: str | None) -> str | None:
        return _clean_optional_identifier(value)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_tags(value)

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _clean_metadata(value)

    @field_validator("created_at", "updated_at", "last_accessed_at", "expires_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("memory timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _validate_project_scoping(self) -> Self:
        if self.memory_type is MemoryType.PROJECT and self.project_id is None:
            raise ValueError("project memory requires a project_id")
        if self.memory_type is MemoryType.WORKING:
            raise ValueError(
                "working memory is never represented as a Memory record; "
                "use WorkingMemoryEntry / WorkingMemoryStore instead"
            )
        return self

    def is_usable(self, *, now: datetime) -> bool:
        """True only if ACTIVE and not expired as of ``now``."""

        if self.status is not MemoryStatus.ACTIVE:
            return False
        if self.expires_at is not None and self.expires_at <= now:
            return False
        return True

    def normalized_content(self) -> str:
        return normalize_for_comparison(self.content)


class MemoryCandidate(BaseModel):
    """What a caller (a future AgentCore, a user command) proposes.

    Not yet a ``Memory`` — ``MemoryPolicy.decide`` and ``MemoryEngine``
    stand between a candidate and anything actually being persisted. See
    ``docs/memory.md`` for the full LLM → candidate → policy → engine →
    store flow.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: Principal
    memory_type: MemoryType
    content: str = Field(min_length=1, max_length=_MAX_CONTENT_LENGTH)
    source: MemorySource
    confidence: MemoryConfidence
    project_id: str | None = Field(default=None, max_length=_MAX_PROJECT_ID_LENGTH)
    tags: tuple[str, ...] = Field(default_factory=tuple)
    metadata: dict[str, str] = Field(default_factory=dict)
    importance: MemoryImportance = MemoryImportance.NORMAL
    expires_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=_MAX_REASON_LENGTH)

    @field_validator("content", mode="before")
    @classmethod
    def _clean_content(cls, value: str) -> str:
        return _clean_required_text(value)

    @field_validator("project_id", mode="before")
    @classmethod
    def _clean_project_id(cls, value: str | None) -> str | None:
        return _clean_optional_identifier(value)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_tags(value)

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _clean_metadata(value)

    @field_validator("reason", mode="before")
    @classmethod
    def _sanitize_reason(cls, value: str | None) -> str | None:
        return _sanitize_text(value, max_length=_MAX_REASON_LENGTH)

    @field_validator("expires_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _validate_project_scoping(self) -> Self:
        if self.memory_type is MemoryType.PROJECT and self.project_id is None:
            raise ValueError("project memory requires a project_id")
        return self

    def normalized_content(self) -> str:
        return normalize_for_comparison(self.content)


class PolicyOutcome(StrEnum):
    """The Memory Policy's three, unambiguous results — never a bare bool."""

    STORE = "store"
    REJECT = "reject"
    REQUIRES_REVIEW = "requires_review"


class PolicyReason(StrEnum):
    """Closed set of policy reasons — informative without leaking content."""

    SECRET_LIKE_CONTENT = "secret_like_content"
    CONTENT_TOO_SHORT = "content_too_short"
    NOT_EXPLICITLY_CONFIRMED = "not_explicitly_confirmed"
    INVALID_CANDIDATE = "invalid_candidate"


class PolicyDecision(BaseModel):
    """The policy's answer for one candidate."""

    model_config = ConfigDict(frozen=True)

    outcome: PolicyOutcome
    candidate: MemoryCandidate
    decided_at: datetime
    reason: PolicyReason | None = None

    @field_validator("decided_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("decided_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.outcome is PolicyOutcome.STORE and self.reason is not None:
            raise ValueError("a STORE decision must not carry a reason")
        if self.outcome is not PolicyOutcome.STORE and self.reason is None:
            raise ValueError("a non-STORE decision must carry a PolicyReason")
        return self


class MemoryOutcome(BaseModel):
    """What ``MemoryEngine.remember`` returns: the policy decision, plus
    the stored (or touched-duplicate) ``Memory`` if and only if the
    decision was ``STORE``."""

    model_config = ConfigDict(frozen=True)

    decision: PolicyDecision
    memory: Memory | None = None
    was_duplicate: bool = False

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.decision.outcome is PolicyOutcome.STORE and self.memory is None:
            raise ValueError("a STORE outcome must carry the stored memory")
        if self.decision.outcome is not PolicyOutcome.STORE and self.memory is not None:
            raise ValueError("a non-STORE outcome must not carry a memory")
        return self


class WorkingMemoryEntry(BaseModel):
    """One slot of short-lived, explicitly-not-permanent working memory.

    A distinct type from ``Memory`` on purpose — a ``WorkingMemoryEntry``
    cannot be passed to ``MemoryStore.save`` because it is a different
    Python type, not merely a different ``memory_type`` value. That makes
    "working memory must not accidentally become permanent memory" a
    static-typing guarantee, not just a runtime check.
    """

    model_config = ConfigDict(frozen=True)

    principal: Principal
    key: str = Field(min_length=1, max_length=_MAX_WORKING_KEY_LENGTH)
    content: str = Field(min_length=1, max_length=_MAX_CONTENT_LENGTH)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None

    @field_validator("key", mode="before")
    @classmethod
    def _clean_key(cls, value: str) -> str:
        cleaned = _strip_control_characters(value).strip()
        if not cleaned:
            raise ValueError("working memory key must not be blank")
        return cleaned

    @field_validator("content", mode="before")
    @classmethod
    def _clean_content(cls, value: str) -> str:
        return _strip_control_characters(value).strip()

    @field_validator("created_at", "updated_at", "expires_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("working memory timestamps must be timezone-aware")
        return value

    def is_usable(self, *, now: datetime) -> bool:
        return self.expires_at is None or self.expires_at > now


class RetrievalQuery(BaseModel):
    """A bounded, principal-scoped, deterministic retrieval request.

    ``principal`` is required — there is no way to construct a query that
    is not scoped to exactly one principal. Project-type memories are
    only ever included when ``project_id`` is given explicitly; an
    unscoped query silently excludes them rather than mixing every
    project's facts together. See ``sam.memory.retrieval`` for ranking.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: Principal
    text: str | None = Field(default=None, max_length=_MAX_CONTENT_LENGTH)
    memory_types: tuple[MemoryType, ...] | None = None
    project_id: str | None = Field(default=None, max_length=_MAX_PROJECT_ID_LENGTH)
    tags: tuple[str, ...] = Field(default_factory=tuple)
    limit: int = Field(default=20, gt=0, le=200)

    @field_validator("text", mode="before")
    @classmethod
    def _clean_text_query(cls, value: str | None) -> str | None:
        return _sanitize_text(value, max_length=_MAX_CONTENT_LENGTH)

    @field_validator("memory_types")
    @classmethod
    def _validate_types(
        cls, value: tuple[MemoryType, ...] | None
    ) -> tuple[MemoryType, ...] | None:
        if value is not None and MemoryType.WORKING in value:
            raise ValueError(
                "working memory is retrieved via WorkingMemoryStore, "
                "not RetrievalQuery"
            )
        return value

    @field_validator("project_id", mode="before")
    @classmethod
    def _clean_project_id(cls, value: str | None) -> str | None:
        # Never truncate an identifier used for equality/isolation
        # filtering — Field(max_length=...) below rejects an overlong one
        # outright instead of silently matching the wrong project.
        return _clean_optional_identifier(value)


class RetrievedMemory(BaseModel):
    """One ranked result: the memory plus its deterministic score."""

    model_config = ConfigDict(frozen=True)

    memory: Memory
    score: float = Field(ge=0.0, le=1.0)


class RetrievalResult(BaseModel):
    """A bounded, ordered retrieval result. Never unbounded."""

    model_config = ConfigDict(frozen=True)

    items: tuple[RetrievedMemory, ...]
    query: RetrievalQuery


class MemoryOperation(StrEnum):
    """Closed set of memory operations recorded to the audit sink."""

    REMEMBER = "remember"
    GET = "get"
    UPDATE = "update"
    DELETE = "delete"
    RETRIEVE = "retrieve"


class MemoryAuditOutcome(StrEnum):
    """Closed set of audit outcomes — mirrors, but is distinct from,
    ``sam.permissions.models.DecisionOutcome``: memory operations are not
    permission decisions."""

    SUCCESS = "success"
    NOT_STORED = "not_stored"
    NOT_FOUND = "not_found"
    DENIED = "denied"
    ERROR = "error"


class MemoryAuditEvent(BaseModel):
    """One immutable, content-free record of a memory operation.

    Mirrors the design of ``sam.permissions.models.AuditEvent`` (closed
    enums, ids, and bounded metadata only — never free-text content) but
    is its own type: memory operations do not fit the permission
    decision vocabulary (risk level, grant id, confirmation status), and
    duplicating that model would force an awkward mapping. See
    ``docs/memory.md`` for why this does not modify
    ``sam.permissions.audit``.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=_MAX_ID_LENGTH)
    occurred_at: datetime
    operation: MemoryOperation
    principal: Principal
    memory_type: MemoryType | None = None
    memory_id: str | None = Field(default=None, max_length=_MAX_ID_LENGTH)
    project_id: str | None = Field(default=None, max_length=_MAX_PROJECT_ID_LENGTH)
    outcome: MemoryAuditOutcome
    policy_reason: PolicyReason | None = None
    result_count: int | None = Field(default=None, ge=0)

    @field_validator("occurred_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("audit timestamps must be timezone-aware")
        return value


__all__ = [
    "Memory",
    "MemoryAuditEvent",
    "MemoryAuditOutcome",
    "MemoryCandidate",
    "MemoryConfidence",
    "MemoryImportance",
    "MemoryOperation",
    "MemoryOutcome",
    "MemorySource",
    "MemoryStatus",
    "MemoryType",
    "PolicyDecision",
    "PolicyOutcome",
    "PolicyReason",
    "Principal",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrievedMemory",
    "WorkingMemoryEntry",
    "importance_weight",
    "normalize_for_comparison",
    "utc_now",
]
