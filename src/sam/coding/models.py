"""Typed, immutable domain models for the Coding Agent.

Depends only on the standard library, Pydantic, and
``sam.permissions.models`` (``Principal``, ``RiskLevel`` — reused, not
duplicated, the same choice every prior phase makes). Nothing here
performs I/O, path resolution, or process execution — that lives in
``sam.coding.repository``. No model in this module can express a
shell/terminal/arbitrary-command capability: command execution is always
one of a closed set of ``CodingOperation`` values (see
``sam.coding.operations``), never a free-form string.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    field_validator,
    model_validator,
)

from sam.permissions.models import Principal, RiskLevel

# --------------------------------------------------------------------- #
# Bounds — every one exists because Phase 6 explicitly requires it;
# see docs/coding-agent.md for the rationale behind each value.
# --------------------------------------------------------------------- #

MAX_REPOSITORY_ID_LENGTH = 200
MAX_INSTRUCTION_LENGTH = 5_000
MAX_PATH_LENGTH = 1_000
MAX_REASON_LENGTH = 500
MAX_FILE_READ_BYTES = 500_000
MAX_FILE_WRITE_BYTES = 500_000
MAX_FILES_PER_LISTING = 1_000
MAX_DIRECTORY_DEPTH = 12
MAX_COMMAND_OUTPUT_CHARS = 50_000
MAX_TIMEOUT_SECONDS = 300.0
MAX_PLAN_STEPS = 20
MAX_AFFECTED_PATHS = 20
MAX_DESCRIPTION_LENGTH = 500
MAX_HASH_LENGTH = 64  # sha256 hex digest length

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def utc_now() -> datetime:
    """The single clock function used throughout the coding-agent domain."""

    return datetime.now(UTC)


def new_operation_id() -> str:
    return uuid4().hex


def new_task_id() -> str:
    return uuid4().hex


def sanitize_display_text(value: str | None, *, max_length: int) -> str | None:
    """Clean and truncate free-text *display-only* fields — never used
    for identity, path resolution, or authorization decisions."""

    if value is None:
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()
    if not cleaned:
        return None
    return cleaned[:max_length]


def _clean_required_text(value: str) -> str:
    """Strip control characters without truncating — used for fields
    whose length is enforced by a hard ``Field`` bound instead (paths,
    identifiers): silently truncating a path could resolve to a
    different file than the caller intended."""

    return _CONTROL_CHARS.sub("", value).strip()


def validate_path_text(value: str) -> str:
    """Reject (never silently strip) a control character — including a
    null byte — in a repository-relative path field. Unlike free text,
    silently removing a character from a *path* could change which file
    it resolves to (``"src/\\x00evil.py"`` quietly becoming the very
    real ``"src/evil.py"``), which is worse than rejecting outright.
    ``sam.coding.repository.resolve_within_repository`` re-validates
    this independently — this is the earliest, not the only, check."""

    if _CONTROL_CHARS.search(value):
        raise ValueError("path must not contain control characters")
    return value


class CodingOperation(StrEnum):
    """The closed set of operations Phase 6 implements, and no others.

    Deliberately excludes ``ARBITRARY_COMMAND``/``SHELL``/
    ``EXECUTE_SCRIPT``/``GIT_PUSH``/``GIT_FORCE_PUSH``/``GIT_RESET``/
    ``GIT_CLEAN``/``GIT_RESTORE_ALL``/``INSTALL_PACKAGE``/
    ``CHANGE_SYSTEM_CONFIG``/``DELETE_FILE`` — none of those exist
    anywhere in this package, not merely "unused".
    """

    INSPECT_REPOSITORY = "inspect_repository"
    READ_FILE = "read_file"
    LIST_FILES = "list_files"
    GET_GIT_STATUS = "get_git_status"
    GET_GIT_DIFF = "get_git_diff"
    CREATE_FILE = "create_file"
    MODIFY_FILE = "modify_file"
    RUN_TESTS = "run_tests"
    RUN_LINT = "run_lint"
    RUN_TYPECHECK = "run_typecheck"
    VERIFY_DIFF = "verify_diff"


class RepositoryContext(BaseModel):
    """The explicit, single repository an executor/backend is bound to.

    ``root`` must be an absolute path; existence and directory-ness are
    checked by ``sam.coding.repository`` (I/O), not here — this model
    stays pure data, testable without a real filesystem. ``allowed_paths``
    is an optional structural allow-list (which top-level areas of the
    repository are even conceptually in scope) — a second, coarser layer
    on top of per-request Permission Engine scoping, not a replacement
    for it.
    """

    model_config = ConfigDict(frozen=True)

    repository_id: str = Field(min_length=1, max_length=MAX_REPOSITORY_ID_LENGTH)
    root: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)
    allowed_paths: tuple[str, ...] | None = None

    @field_validator("repository_id", mode="before")
    @classmethod
    def _clean_repository_id(cls, value: str) -> str:
        cleaned = _clean_required_text(value)
        if not cleaned:
            raise ValueError("repository_id must not be blank")
        return cleaned

    @field_validator("root")
    @classmethod
    def _validate_root(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("root must not contain a null byte")
        if not value.startswith("/"):
            raise ValueError("repository root must be an absolute path")
        return value

    @field_validator("allowed_paths")
    @classmethod
    def _validate_allowed_paths(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for path in value:
            stripped = _clean_required_text(path)
            if not stripped or stripped.startswith("/") or ".." in stripped.split("/"):
                raise ValueError(f"invalid allowed_paths entry: {path!r}")
            cleaned.append(stripped)
        return tuple(cleaned)


class CodingTask(BaseModel):
    """The outer task a caller (a future AgentCore, a user command) gives
    Sam. Not itself an authorization — every individual operation still
    passes through the Permission Engine independently; a task only
    groups related operations for planning and audit correlation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1, max_length=100)
    principal: Principal
    repository_id: str = Field(min_length=1, max_length=MAX_REPOSITORY_ID_LENGTH)
    instruction: str = Field(min_length=1, max_length=MAX_INSTRUCTION_LENGTH)
    created_at: datetime

    @field_validator("task_id", "repository_id", mode="before")
    @classmethod
    def _clean_ids(cls, value: str) -> str:
        return _clean_required_text(value)

    @field_validator("instruction", mode="before")
    @classmethod
    def _clean_instruction(cls, value: str) -> str:
        return _clean_required_text(value)

    @field_validator("created_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return value


class FileChangeKind(StrEnum):
    """Only creation and modification exist. There is no ``DELETE`` —
    deletion is intentionally unimplemented in Phase 6; see
    docs/coding-agent.md."""

    CREATE = "create"
    MODIFY = "modify"


class FileChange(BaseModel):
    """One proposed, bounded file write, with optimistic concurrency.

    ``expected_original_hash`` is the caller's proof that it read the
    file (or knows it does not exist) before proposing this change — see
    ``sam.coding.repository.RepositoryBackend`` for how it is verified
    immediately before the write, never trusted at face value.
    """

    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)
    operation: FileChangeKind
    expected_original_hash: str | None = Field(default=None, max_length=MAX_HASH_LENGTH)
    new_content: str = Field(max_length=MAX_FILE_WRITE_BYTES)

    @field_validator("path")
    @classmethod
    def _clean_path(cls, value: str) -> str:
        return validate_path_text(value)

    @model_validator(mode="after")
    def _validate_hash_presence(self) -> Self:
        is_modify_without_hash = (
            self.operation is FileChangeKind.MODIFY
            and self.expected_original_hash is None
        )
        if is_modify_without_hash:
            raise ValueError("modify requires expected_original_hash")
        if (
            self.operation is FileChangeKind.CREATE
            and self.expected_original_hash is not None
        ):
            raise ValueError("create must not specify expected_original_hash")
        return self


# --------------------------------------------------------------------- #
# Result payloads
# --------------------------------------------------------------------- #


class FileReadResult(BaseModel):
    """``content`` is withheld (``None``) for two distinct reasons —
    ``is_binary`` (undecodable content) and ``restricted`` (a
    secret-looking filename or a secret-like content match; see
    ``sam.coding.executor``'s secret-protection check) — kept as
    separate flags rather than conflated so a caller can tell which
    happened."""

    model_config = ConfigDict(frozen=True)

    path: str
    content: str | None
    content_hash: str = Field(max_length=MAX_HASH_LENGTH)
    size_bytes: int = Field(ge=0)
    is_binary: bool
    truncated: bool
    restricted: bool = False


class DirectoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    is_directory: bool
    size_bytes: int | None = Field(default=None, ge=0)


class DirectoryListing(BaseModel):
    model_config = ConfigDict(frozen=True)

    entries: tuple[DirectoryEntry, ...]
    truncated: bool


class FileWriteResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    operation: FileChangeKind
    new_hash: str = Field(max_length=MAX_HASH_LENGTH)
    bytes_written: int = Field(ge=0)


class CommandExecutionResult(BaseModel):
    """Bounded output of one allowlisted command invocation. Never
    contains more than ``MAX_COMMAND_OUTPUT_CHARS`` of stdout/stderr —
    truncation is always flagged, never hidden."""

    model_config = ConfigDict(frozen=True)

    stdout: str = Field(max_length=MAX_COMMAND_OUTPUT_CHARS)
    stderr: str = Field(max_length=MAX_COMMAND_OUTPUT_CHARS)
    exit_code: int | None
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    duration_seconds: float = Field(ge=0)


class GitStatusResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw: str = Field(max_length=MAX_COMMAND_OUTPUT_CHARS)
    truncated: bool


class GitDiffResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw: str = Field(max_length=MAX_COMMAND_OUTPUT_CHARS)
    truncated: bool


class ResolvedCommand(BaseModel):
    """An internal, code-defined command specification.

    Never constructed from caller/LLM input — see
    ``sam.coding.repository``'s static command registry, the only place
    that builds these. ``executable_name`` is a logical name ("uv",
    "git"), resolved to a trusted absolute path at invocation time — see
    ``sam.coding.repository._resolve_executable``.
    """

    model_config = ConfigDict(frozen=True)

    executable_name: str = Field(min_length=1, max_length=50)
    arguments: tuple[str, ...] = Field(max_length=20)
    timeout_seconds: FiniteFloat = Field(gt=0, le=MAX_TIMEOUT_SECONDS)


class VerificationStatus(StrEnum):
    """Closed status vocabulary — ``NOT_RUN`` must never become
    ``PASSED``; see ``sam.coding.verifier``."""

    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"


class ExecutionOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class PermissionOutcomeSummary(StrEnum):
    """A minimal echo of ``sam.permissions.models.DecisionOutcome``, kept
    as its own type for the same reason ``sam.computer.models`` keeps
    one: this module never needs to import the full permission-decision
    vocabulary just to describe three words back to a caller."""

    ALLOW = "allow"
    CONFIRM_REQUIRED = "confirm_required"
    DENY = "deny"


class ErrorCategory(StrEnum):
    """Closed set of failure categories — never a raw exception message."""

    PERMISSION_DENIED = "permission_denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_INVALID = "confirmation_invalid"
    VALIDATION_ERROR = "validation_error"
    REPOSITORY_BOUNDARY = "repository_boundary"
    FILE_NOT_FOUND = "file_not_found"
    BINARY_FILE_REJECTED = "binary_file_rejected"
    SECRET_RESTRICTED = "secret_restricted"
    CONCURRENT_MODIFICATION = "concurrent_modification"
    UNEXPECTED_CHANGES = "unexpected_changes"
    COMMAND_UNAVAILABLE = "command_unavailable"
    COMMAND_ERROR = "command_error"
    COMMAND_TIMEOUT = "command_timeout"
    PLAN_TOO_LARGE = "plan_too_large"
    INTERNAL_ERROR = "internal_error"


class CodingOperationResult(BaseModel):
    """What ``CodingExecutor.execute`` always returns. Never claims
    ``SUCCESS`` unless the underlying operation actually completed —
    the same structural invariant as
    ``sam.computer.models.ComputerActionResult``."""

    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(min_length=1, max_length=100)
    operation: CodingOperation
    principal: Principal
    repository_id: str = Field(max_length=MAX_REPOSITORY_ID_LENGTH)
    outcome: ExecutionOutcome
    permission_outcome: PermissionOutcomeSummary
    created_at: datetime
    error_category: ErrorCategory | None = None
    confirmation_id: str | None = None
    file_read: FileReadResult | None = None
    directory_listing: DirectoryListing | None = None
    git_status: GitStatusResult | None = None
    git_diff: GitDiffResult | None = None
    file_write: FileWriteResult | None = None
    command_result: CommandExecutionResult | None = None
    verification_status: VerificationStatus | None = None

    @field_validator("created_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.outcome is ExecutionOutcome.FAILED and self.error_category is None:
            raise ValueError("a FAILED result must carry an error_category")
        if self.outcome is ExecutionOutcome.SUCCESS and self.error_category is not None:
            raise ValueError("a SUCCESS result must not carry an error_category")
        return self


# --------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------- #


class PlanStep(BaseModel):
    """One bounded, already-decided unit of work — the planner validates
    and bounds a caller-supplied list of these; it never generates them
    itself. ``description`` is display-only, sanitized, never used for
    any decision."""

    model_config = ConfigDict(frozen=True)

    operation: CodingOperation
    path: str | None = Field(default=None, max_length=MAX_PATH_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)

    @field_validator("path", mode="before")
    @classmethod
    def _clean_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _clean_required_text(value)
        return cleaned or None

    @field_validator("description", mode="before")
    @classmethod
    def _clean_description(cls, value: str | None) -> str | None:
        return sanitize_display_text(value, max_length=MAX_DESCRIPTION_LENGTH)


class CodingPlan(BaseModel):
    """A finite, bounded plan — see ``sam.coding.planner.CodingPlanner``,
    which is the only place these are constructed, always from an
    already-finite, caller-supplied step list."""

    model_config = ConfigDict(frozen=True)

    task_id: str = Field(min_length=1, max_length=100)
    steps: tuple[PlanStep, ...] = Field(max_length=MAX_PLAN_STEPS)
    affected_paths: tuple[str, ...] = Field(max_length=MAX_AFFECTED_PATHS)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return value


# --------------------------------------------------------------------- #
# Provider abstraction — see sam.coding.planner.CodingProvider
# --------------------------------------------------------------------- #


class ProviderName(StrEnum):
    """A closed set of provider identities for audit/display — adding a
    real external provider means adding a name here deliberately, never
    inferring one from caller input."""

    SAM_INTERNAL = "sam_internal"
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


class CodingProposal(BaseModel):
    """What a ``CodingProvider`` returns: a bounded *proposal*, never a
    direct repository mutation. Feeds into
    ``sam.coding.planner.CodingPlanner.build_plan`` — the same
    validation/bounding a hand-written step list would go through."""

    model_config = ConfigDict(frozen=True)

    provider: ProviderName
    summary: str = Field(max_length=MAX_DESCRIPTION_LENGTH)
    steps: tuple[PlanStep, ...] = Field(max_length=MAX_PLAN_STEPS)

    @field_validator("summary", mode="before")
    @classmethod
    def _clean_summary(cls, value: str) -> str:
        return sanitize_display_text(value, max_length=MAX_DESCRIPTION_LENGTH) or ""


# --------------------------------------------------------------------- #
# Changeset reporting
# --------------------------------------------------------------------- #


class ChangesetFileStatus(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    UNEXPECTED = "unexpected"


class ChangesetEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    status: ChangesetFileStatus


class ChangesetReport(BaseModel):
    """The result of comparing actual changed files (from ``git status``)
    against the set of paths the executor actually wrote to. Any file
    outside that expected set is reported as ``UNEXPECTED``, never
    silently reverted — see docs/coding-agent.md's "Changeset boundary"
    section."""

    model_config = ConfigDict(frozen=True)

    expected_paths: tuple[str, ...]
    entries: tuple[ChangesetEntry, ...]
    has_unexpected_changes: bool


# --------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------- #


class CodingConfirmationOutcome(StrEnum):
    """Mirrors the pattern in ``sam.computer.models.ComputerAuditOutcome``
    — its own type, not a reuse of a permission-decision outcome."""

    DENIED = "denied"
    CONFIRM_REQUIRED = "confirm_required"


class CodingAuditEvent(BaseModel):
    """One immutable, content-free record of a coding operation.

    Never carries file contents, patch/diff text, command stdout/stderr,
    or any raw exception message — only closed enums, ids, and bounded
    path lists. See docs/coding-agent.md.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=100)
    occurred_at: datetime
    operation_id: str = Field(min_length=1, max_length=100)
    principal: Principal
    repository_id: str = Field(max_length=MAX_REPOSITORY_ID_LENGTH)
    operation: CodingOperation
    risk: RiskLevel
    permission_outcome: PermissionOutcomeSummary
    confirmation_outcome: CodingConfirmationOutcome | None = None
    execution_outcome: ExecutionOutcome | None = None
    error_category: ErrorCategory | None = None
    affected_paths: tuple[str, ...] = Field(default=(), max_length=MAX_AFFECTED_PATHS)
    verification_status: VerificationStatus | None = None

    @field_validator("occurred_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("audit timestamps must be timezone-aware")
        return value



__all__ = [
    "ChangesetEntry",
    "ChangesetFileStatus",
    "ChangesetReport",
    "CodingAuditEvent",
    "CodingConfirmationOutcome",
    "CodingOperation",
    "CodingOperationResult",
    "CodingPlan",
    "CodingProposal",
    "CodingTask",
    "CommandExecutionResult",
    "DirectoryEntry",
    "DirectoryListing",
    "ErrorCategory",
    "ExecutionOutcome",
    "FileChange",
    "FileChangeKind",
    "FileReadResult",
    "FileWriteResult",
    "GitDiffResult",
    "GitStatusResult",
    "PermissionOutcomeSummary",
    "PlanStep",
    "Principal",
    "ProviderName",
    "RepositoryContext",
    "ResolvedCommand",
    "RiskLevel",
    "VerificationStatus",
    "new_operation_id",
    "new_task_id",
    "sanitize_display_text",
    "utc_now",
    "validate_path_text",
]
