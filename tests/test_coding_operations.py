"""Tests for per-operation request models."""

import pytest
from pydantic import ValidationError

from sam.coding.models import FileChange, FileChangeKind
from sam.coding.operations import (
    CreateFileOperation,
    ListFilesOperation,
    ModifyFileOperation,
    ReadFileOperation,
)
from sam.permissions.models import Principal, PrincipalKind


def _principal() -> Principal:
    return Principal(kind=PrincipalKind.USER, id="ali")


def test_read_file_operation_valid() -> None:
    op = ReadFileOperation(principal=_principal(), path="src/x.py")
    assert op.path == "src/x.py"


def test_list_files_operation_optional_path() -> None:
    op = ListFilesOperation(principal=_principal())
    assert op.path is None


def test_operations_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ReadFileOperation.model_validate(
            {"principal": _principal(), "path": "x.py", "unexpected": "field"}
        )


def test_create_file_operation_requires_create_change() -> None:
    with pytest.raises(ValidationError):
        CreateFileOperation(
            principal=_principal(),
            change=FileChange(
                path="x.py",
                operation=FileChangeKind.MODIFY,
                expected_original_hash="a" * 64,
                new_content="x",
            ),
        )


def test_modify_file_operation_requires_modify_change() -> None:
    with pytest.raises(ValidationError):
        ModifyFileOperation(
            principal=_principal(),
            change=FileChange(
                path="x.py", operation=FileChangeKind.CREATE, new_content="x"
            ),
        )


def test_create_file_operation_accepts_matching_change() -> None:
    op = CreateFileOperation(
        principal=_principal(),
        change=FileChange(
            path="x.py", operation=FileChangeKind.CREATE, new_content="x"
        ),
    )
    assert op.change.path == "x.py"


def test_operations_are_frozen() -> None:
    op = ReadFileOperation(principal=_principal(), path="src/x.py")
    with pytest.raises(ValidationError):
        op.path = "other.py"


def test_operation_reason_is_sanitized() -> None:
    op = ReadFileOperation(
        principal=_principal(), path="src/x.py", reason="check\x00status"
    )
    assert op.reason is not None
    assert "\x00" not in op.reason
