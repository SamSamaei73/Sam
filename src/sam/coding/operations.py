"""Typed, per-operation request models.

Each ``CodingOperation`` (see ``sam.coding.models``) has exactly one
corresponding request model here. None carries an ``operation_id`` or
``created_at`` — those are always assigned by ``CodingExecutor``, the
same discipline every prior phase's action/candidate models use. None
carries a raw command string, shell fragment, or free-form argument
list — ``RunTestsOperation``/``RunLintOperation``/``RunTypecheckOperation``/
``VerifyDiffOperation`` are bare markers; the actual command (executable
+ fixed arguments) is looked up from ``sam.coding.repository``'s internal
registry by operation type, never influenced by these models' fields.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sam.coding.models import (
    MAX_PATH_LENGTH,
    MAX_REASON_LENGTH,
    FileChange,
    FileChangeKind,
    Principal,
    sanitize_display_text,
    validate_path_text,
)


class _OperationBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: Principal
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)

    @field_validator("reason", mode="before")
    @classmethod
    def _sanitize_reason(cls, value: str | None) -> str | None:
        return sanitize_display_text(value, max_length=MAX_REASON_LENGTH)


class InspectRepositoryOperation(_OperationBase):
    """A bounded, top-level structural overview of the repository."""


class ReadFileOperation(_OperationBase):
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_path_text(value)


class ListFilesOperation(_OperationBase):
    path: str | None = Field(default=None, max_length=MAX_PATH_LENGTH)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str | None) -> str | None:
        return None if value is None else validate_path_text(value)


class GetGitStatusOperation(_OperationBase):
    pass


class GetGitDiffOperation(_OperationBase):
    pass


class CreateFileOperation(_OperationBase):
    change: FileChange

    @model_validator(mode="after")
    def _require_create_kind(self) -> Self:
        if self.change.operation is not FileChangeKind.CREATE:
            raise ValueError("CreateFileOperation requires a CREATE FileChange")
        return self


class ModifyFileOperation(_OperationBase):
    change: FileChange

    @model_validator(mode="after")
    def _require_modify_kind(self) -> Self:
        if self.change.operation is not FileChangeKind.MODIFY:
            raise ValueError("ModifyFileOperation requires a MODIFY FileChange")
        return self


class RunTestsOperation(_OperationBase):
    pass


class RunLintOperation(_OperationBase):
    pass


class RunTypecheckOperation(_OperationBase):
    pass


class VerifyDiffOperation(_OperationBase):
    pass


CodingOperationRequest = (
    InspectRepositoryOperation
    | ReadFileOperation
    | ListFilesOperation
    | GetGitStatusOperation
    | GetGitDiffOperation
    | CreateFileOperation
    | ModifyFileOperation
    | RunTestsOperation
    | RunLintOperation
    | RunTypecheckOperation
    | VerifyDiffOperation
)


__all__ = [
    "CodingOperationRequest",
    "CreateFileOperation",
    "GetGitDiffOperation",
    "GetGitStatusOperation",
    "InspectRepositoryOperation",
    "ListFilesOperation",
    "ModifyFileOperation",
    "ReadFileOperation",
    "RunLintOperation",
    "RunTestsOperation",
    "RunTypecheckOperation",
    "VerifyDiffOperation",
]
