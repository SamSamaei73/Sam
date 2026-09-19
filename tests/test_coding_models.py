"""Tests for coding-agent domain models."""

import math
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sam.coding.models import (
    MAX_INSTRUCTION_LENGTH,
    MAX_PATH_LENGTH,
    CodingAuditEvent,
    CodingOperation,
    CodingOperationResult,
    CodingPlan,
    CodingProposal,
    CodingTask,
    ErrorCategory,
    ExecutionOutcome,
    FileChange,
    FileChangeKind,
    PermissionOutcomeSummary,
    PlanStep,
    ProviderName,
    RepositoryContext,
    ResolvedCommand,
)
from sam.permissions.models import Principal, PrincipalKind, RiskLevel

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _principal() -> Principal:
    return Principal(kind=PrincipalKind.USER, id="ali")


# --------------------------------------------------------------------- #
# RepositoryContext
# --------------------------------------------------------------------- #


def test_repository_context_valid() -> None:
    ctx = RepositoryContext(repository_id="sam-core", root="/tmp/sam")
    assert ctx.repository_id == "sam-core"


def test_repository_context_rejects_relative_root() -> None:
    with pytest.raises(ValidationError):
        RepositoryContext(repository_id="sam-core", root="relative/path")


def test_repository_context_rejects_null_byte_in_root() -> None:
    with pytest.raises(ValidationError):
        RepositoryContext(repository_id="sam-core", root="/tmp/\x00evil")


def test_repository_context_rejects_blank_repository_id() -> None:
    with pytest.raises(ValidationError):
        RepositoryContext(repository_id="   ", root="/tmp/sam")


def test_repository_context_allowed_paths_rejects_traversal() -> None:
    with pytest.raises(ValidationError):
        RepositoryContext(
            repository_id="sam-core", root="/tmp/sam", allowed_paths=("src/../etc",)
        )


def test_repository_context_allowed_paths_rejects_absolute() -> None:
    with pytest.raises(ValidationError):
        RepositoryContext(
            repository_id="sam-core", root="/tmp/sam", allowed_paths=("/etc",)
        )


def test_repository_context_is_frozen() -> None:
    ctx = RepositoryContext(repository_id="sam-core", root="/tmp/sam")
    with pytest.raises(ValidationError):
        ctx.repository_id = "other"


# --------------------------------------------------------------------- #
# CodingTask
# --------------------------------------------------------------------- #


def _task(**overrides: object) -> CodingTask:
    defaults: dict[str, object] = {
        "task_id": "t1",
        "principal": _principal(),
        "repository_id": "sam-core",
        "instruction": "fix the bug",
        "created_at": _NOW,
    }
    defaults.update(overrides)
    return CodingTask.model_validate(defaults)


def test_valid_task_constructs() -> None:
    task = _task()
    assert task.task_id == "t1"


def test_task_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        _task(created_at=datetime(2026, 1, 1))


def test_task_rejects_blank_instruction() -> None:
    with pytest.raises(ValidationError):
        _task(instruction="   ")


def test_task_rejects_oversized_instruction() -> None:
    with pytest.raises(ValidationError):
        _task(instruction="x" * (MAX_INSTRUCTION_LENGTH + 1))


def test_task_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CodingTask.model_validate(
            {
                "task_id": "t1",
                "principal": _principal(),
                "repository_id": "sam-core",
                "instruction": "fix it",
                "created_at": _NOW,
                "unexpected": "field",
            }
        )


def test_task_is_frozen() -> None:
    task = _task()
    with pytest.raises(ValidationError):
        task.instruction = "changed"


# --------------------------------------------------------------------- #
# FileChange
# --------------------------------------------------------------------- #


def test_file_change_create_requires_no_hash() -> None:
    change = FileChange(
        path="src/x.py", operation=FileChangeKind.CREATE, new_content="print(1)"
    )
    assert change.expected_original_hash is None


def test_file_change_create_rejects_hash() -> None:
    with pytest.raises(ValidationError):
        FileChange(
            path="src/x.py",
            operation=FileChangeKind.CREATE,
            expected_original_hash="a" * 64,
            new_content="print(1)",
        )


def test_file_change_modify_requires_hash() -> None:
    with pytest.raises(ValidationError):
        FileChange(path="src/x.py", operation=FileChangeKind.MODIFY, new_content="x")


def test_file_change_rejects_oversized_path() -> None:
    with pytest.raises(ValidationError):
        FileChange(
            path="x" * (MAX_PATH_LENGTH + 1),
            operation=FileChangeKind.CREATE,
            new_content="x",
        )


def test_file_change_is_frozen() -> None:
    change = FileChange(
        path="src/x.py", operation=FileChangeKind.CREATE, new_content="x"
    )
    with pytest.raises(ValidationError):
        change.new_content = "changed"


# --------------------------------------------------------------------- #
# ResolvedCommand — internal, never caller-constructed with bad values
# --------------------------------------------------------------------- #


def test_resolved_command_rejects_zero_timeout() -> None:
    with pytest.raises(ValidationError):
        ResolvedCommand(executable_name="git", arguments=(), timeout_seconds=0)


def test_resolved_command_rejects_negative_timeout() -> None:
    with pytest.raises(ValidationError):
        ResolvedCommand(executable_name="git", arguments=(), timeout_seconds=-1)


def test_resolved_command_rejects_nan_timeout() -> None:
    with pytest.raises(ValidationError):
        ResolvedCommand.model_validate(
            {"executable_name": "git", "arguments": (), "timeout_seconds": math.nan}
        )


def test_resolved_command_rejects_infinite_timeout() -> None:
    with pytest.raises(ValidationError):
        ResolvedCommand.model_validate(
            {"executable_name": "git", "arguments": (), "timeout_seconds": math.inf}
        )


def test_resolved_command_rejects_excessive_timeout() -> None:
    with pytest.raises(ValidationError):
        ResolvedCommand(executable_name="git", arguments=(), timeout_seconds=100_000)


def test_resolved_command_is_frozen() -> None:
    command = ResolvedCommand(executable_name="git", arguments=(), timeout_seconds=5)
    with pytest.raises(ValidationError):
        command.executable_name = "evil"


# --------------------------------------------------------------------- #
# CodingOperationResult
# --------------------------------------------------------------------- #


def _result(**overrides: object) -> CodingOperationResult:
    defaults: dict[str, object] = {
        "operation_id": "op1",
        "operation": CodingOperation.READ_FILE,
        "principal": _principal(),
        "repository_id": "sam-core",
        "outcome": ExecutionOutcome.SUCCESS,
        "permission_outcome": PermissionOutcomeSummary.ALLOW,
        "created_at": _NOW,
    }
    defaults.update(overrides)
    return CodingOperationResult.model_validate(defaults)


def test_success_result_must_not_carry_error_category() -> None:
    with pytest.raises(ValidationError):
        _result(error_category=ErrorCategory.COMMAND_ERROR)


def test_failed_result_requires_error_category() -> None:
    with pytest.raises(ValidationError):
        _result(outcome=ExecutionOutcome.FAILED)


def test_result_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        _result(created_at=datetime(2026, 1, 1))


def test_result_is_frozen() -> None:
    result = _result()
    with pytest.raises(ValidationError):
        result.outcome = ExecutionOutcome.FAILED


# --------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------- #


def test_plan_step_path_control_characters_stripped() -> None:
    step = PlanStep(operation=CodingOperation.READ_FILE, path="src/\x00x.py")
    assert step.path is not None
    assert "\x00" not in step.path


def test_plan_rejects_too_many_steps() -> None:
    steps = tuple(
        PlanStep(operation=CodingOperation.READ_FILE, path=f"f{i}.py")
        for i in range(25)
    )
    with pytest.raises(ValidationError):
        CodingPlan(task_id="t1", steps=steps, affected_paths=(), created_at=_NOW)


def test_plan_is_frozen() -> None:
    plan = CodingPlan(task_id="t1", steps=(), affected_paths=(), created_at=_NOW)
    with pytest.raises(ValidationError):
        plan.task_id = "other"


# --------------------------------------------------------------------- #
# CodingProposal
# --------------------------------------------------------------------- #


def test_proposal_rejects_too_many_steps() -> None:
    steps = tuple(
        PlanStep(operation=CodingOperation.READ_FILE, path=f"f{i}.py")
        for i in range(25)
    )
    with pytest.raises(ValidationError):
        CodingProposal(provider=ProviderName.SAM_INTERNAL, summary="x", steps=steps)


# --------------------------------------------------------------------- #
# CodingAuditEvent — never carries content
# --------------------------------------------------------------------- #


def test_audit_event_has_no_field_for_file_content_or_command_output() -> None:
    field_names = set(CodingAuditEvent.model_fields)
    assert "content" not in field_names
    assert "stdout" not in field_names
    assert "stderr" not in field_names
    assert "diff" not in field_names
    assert "new_content" not in field_names


def test_audit_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        CodingAuditEvent(
            event_id="e1",
            occurred_at=datetime(2026, 1, 1),
            operation_id="op1",
            principal=_principal(),
            repository_id="sam-core",
            operation=CodingOperation.READ_FILE,
            risk=RiskLevel.LOW,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
        )


def test_audit_event_bounds_affected_paths() -> None:
    with pytest.raises(ValidationError):
        CodingAuditEvent(
            event_id="e1",
            occurred_at=_NOW,
            operation_id="op1",
            principal=_principal(),
            repository_id="sam-core",
            operation=CodingOperation.READ_FILE,
            risk=RiskLevel.LOW,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
            affected_paths=tuple(f"f{i}.py" for i in range(25)),
        )
