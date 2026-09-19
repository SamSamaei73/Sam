"""Tests for the CodingOperation -> PermissionRequest mapping."""

import pytest

from sam.coding.models import CodingOperation, FileChange, FileChangeKind
from sam.coding.operations import (
    CreateFileOperation,
    GetGitDiffOperation,
    GetGitStatusOperation,
    InspectRepositoryOperation,
    ModifyFileOperation,
    ReadFileOperation,
    RunLintOperation,
    RunTestsOperation,
    RunTypecheckOperation,
    VerifyDiffOperation,
)
from sam.coding.policy import (
    build_request,
    is_restricted_content,
    is_restricted_filename,
    operation_for,
    risk_for,
)
from sam.permissions.models import (
    PermissionAction,
    PermissionResource,
    Principal,
    PrincipalKind,
    RiskLevel,
)


def _principal() -> Principal:
    return Principal(kind=PrincipalKind.USER, id="ali")


def test_read_file_maps_to_code_read() -> None:
    request = build_request(
        ReadFileOperation(principal=_principal(), path="src/x.py"),
        repository_id="sam-core",
    )
    assert request.resource is PermissionResource.CODE
    assert request.action is PermissionAction.READ
    assert request.scope.as_text() == "sam-core/src/x.py"


def test_write_operations_map_to_code_write() -> None:
    change = FileChange(path="x.py", operation=FileChangeKind.CREATE, new_content="x")
    request = build_request(
        CreateFileOperation(principal=_principal(), change=change),
        repository_id="sam-core",
    )
    assert request.resource is PermissionResource.CODE
    assert request.action is PermissionAction.WRITE


def test_modify_uses_the_change_path_for_scope() -> None:
    change = FileChange(
        path="src/y.py",
        operation=FileChangeKind.MODIFY,
        expected_original_hash="a" * 64,
        new_content="x",
    )
    request = build_request(
        ModifyFileOperation(principal=_principal(), change=change),
        repository_id="sam-core",
    )
    assert request.scope.as_text() == "sam-core/src/y.py"


def test_git_status_reuses_the_existing_git_resource() -> None:
    """GET_GIT_STATUS/GET_GIT_DIFF map onto the pre-existing GIT resource
    — not a new one — reusing its already-classified READ/LOW row."""

    request = build_request(
        GetGitStatusOperation(principal=_principal()), repository_id="sam-core"
    )
    assert request.resource is PermissionResource.GIT
    assert request.action is PermissionAction.READ


def test_git_diff_reuses_the_existing_git_resource() -> None:
    request = build_request(
        GetGitDiffOperation(principal=_principal()), repository_id="sam-core"
    )
    assert request.resource is PermissionResource.GIT


def test_verification_operations_map_to_code_execute() -> None:
    for op_class in (RunTestsOperation, RunLintOperation, RunTypecheckOperation):
        request = build_request(
            op_class(principal=_principal()), repository_id="sam-core"
        )
        assert request.resource is PermissionResource.CODE
        assert request.action is PermissionAction.EXECUTE


def test_verify_diff_maps_to_code_read() -> None:
    request = build_request(
        VerifyDiffOperation(principal=_principal()), repository_id="sam-core"
    )
    assert request.resource is PermissionResource.CODE
    assert request.action is PermissionAction.READ


def test_different_repositories_get_different_scopes() -> None:
    request_a = build_request(
        ReadFileOperation(principal=_principal(), path="x.py"), repository_id="repo-a"
    )
    request_b = build_request(
        ReadFileOperation(principal=_principal(), path="x.py"), repository_id="repo-b"
    )
    assert request_a.scope != request_b.scope
    assert not request_a.scope.contains(request_b.scope)
    assert not request_b.scope.contains(request_a.scope)


def test_tests_and_lint_use_different_scopes() -> None:
    tests_request = build_request(
        RunTestsOperation(principal=_principal()), repository_id="sam-core"
    )
    lint_request = build_request(
        RunLintOperation(principal=_principal()), repository_id="sam-core"
    )
    assert tests_request.scope != lint_request.scope


def test_operation_for_matches_build_request_mapping() -> None:
    op = ReadFileOperation(principal=_principal(), path="x.py")
    assert operation_for(op) is CodingOperation.READ_FILE


def test_every_operation_has_a_classified_risk() -> None:
    for operation in CodingOperation:
        risk = risk_for(operation)
        assert risk in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)


def test_read_operations_are_low_risk() -> None:
    assert risk_for(CodingOperation.READ_FILE) is RiskLevel.LOW
    assert risk_for(CodingOperation.LIST_FILES) is RiskLevel.LOW
    assert risk_for(CodingOperation.INSPECT_REPOSITORY) is RiskLevel.LOW
    assert risk_for(CodingOperation.GET_GIT_STATUS) is RiskLevel.LOW
    assert risk_for(CodingOperation.GET_GIT_DIFF) is RiskLevel.LOW
    assert risk_for(CodingOperation.VERIFY_DIFF) is RiskLevel.LOW


def test_write_operations_are_medium_risk() -> None:
    assert risk_for(CodingOperation.CREATE_FILE) is RiskLevel.MEDIUM
    assert risk_for(CodingOperation.MODIFY_FILE) is RiskLevel.MEDIUM


def test_execute_operations_are_high_risk() -> None:
    assert risk_for(CodingOperation.RUN_TESTS) is RiskLevel.HIGH
    assert risk_for(CodingOperation.RUN_LINT) is RiskLevel.HIGH
    assert risk_for(CodingOperation.RUN_TYPECHECK) is RiskLevel.HIGH


def test_no_operation_is_critical_risk() -> None:
    for operation in CodingOperation:
        assert risk_for(operation) is not RiskLevel.CRITICAL


def test_inspect_repository_builds_a_valid_request() -> None:
    request = build_request(
        InspectRepositoryOperation(principal=_principal()), repository_id="sam-core"
    )
    assert request.scope.as_text() == "sam-core:repository"


# --------------------------------------------------------------------- #
# Secret protection helpers
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".env.production",
        "id_rsa",
        "server.pem",
        "config.key",
        "credentials.json",
        "secrets.yaml",
        "secret.txt",
        "path/to/.env",
    ],
)
def test_restricted_filenames_are_detected(path: str) -> None:
    assert is_restricted_filename(path) is True


@pytest.mark.parametrize(
    "path", ["src/main.py", "README.md", "pyproject.toml", "tests/test_x.py"]
)
def test_ordinary_filenames_are_not_restricted(path: str) -> None:
    assert is_restricted_filename(path) is False


def test_restricted_content_reuses_memory_sanitization() -> None:
    assert is_restricted_content("my API key is sk-ant-abcdefghijklmnop1234567890")
    assert not is_restricted_content("print('hello world')")
