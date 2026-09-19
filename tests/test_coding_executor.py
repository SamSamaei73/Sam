"""Tests for CodingExecutor: authorization, dispatch, audit, fail-closed."""

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sam.coding.audit import FailingCodingAuditSink, InMemoryCodingAuditSink
from sam.coding.executor import CodingExecutor
from sam.coding.models import (
    ErrorCategory,
    ExecutionOutcome,
    FileChange,
    FileChangeKind,
    PermissionOutcomeSummary,
    RepositoryContext,
)
from sam.coding.operations import (
    CreateFileOperation,
    GetGitStatusOperation,
    ModifyFileOperation,
    ReadFileOperation,
    RunTestsOperation,
)
from sam.coding.repository import FakeCommandRunner, RepositoryBackend
from sam.permissions.audit import InMemoryAuditSink as InMemoryPermissionAuditSink
from sam.permissions.confirmation import InMemoryConfirmationProvider
from sam.permissions.engine import PermissionEngine
from sam.permissions.errors import PermissionStoreError
from sam.permissions.models import (
    PermissionAction,
    PermissionGrant,
    PermissionResource,
    PermissionScope,
    Principal,
    PrincipalKind,
)
from sam.permissions.store import InMemoryPermissionStore

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _principal(value: str = "ali") -> Principal:
    return Principal(kind=PrincipalKind.USER, id=value)


def _read_op(path: str = "src/main.py") -> ReadFileOperation:
    return ReadFileOperation(principal=_principal(), path=path)


def _init_git(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)


class _Harness:
    def __init__(self, tmp_path: Path, *, clock: datetime = _T0) -> None:
        self.store = InMemoryPermissionStore()
        self.confirmations = InMemoryConfirmationProvider()
        self.perm_audit = InMemoryPermissionAuditSink()
        self._clock_value = clock
        self.engine = PermissionEngine(
            store=self.store,
            confirmation_provider=self.confirmations,
            audit_sink=self.perm_audit,
            clock=lambda: self._clock_value,
        )
        ctx = RepositoryContext(repository_id="sam-core", root=str(tmp_path))
        self.command_runner = FakeCommandRunner()
        self.repository = RepositoryBackend(
            context=ctx, command_runner=self.command_runner
        )
        self.audit = InMemoryCodingAuditSink()
        self.executor = CodingExecutor(
            repository=self.repository,
            permission_engine=self.engine,
            audit_sink=self.audit,
            clock=lambda: self._clock_value,
        )

    def advance(self, delta: timedelta) -> None:
        self._clock_value = self._clock_value + delta

    def grant(
        self,
        *,
        resource: PermissionResource,
        action: PermissionAction,
        scope: str,
        scope_is_path: bool = False,
        principal: Principal | None = None,
        grant_id: str | None = None,
    ) -> None:
        self.store.create_grant(
            PermissionGrant(
                grant_id=grant_id or f"g-{resource.value}-{action.value}-{scope}",
                principal=principal or _principal(),
                resource=resource,
                action=action,
                scope=(
                    PermissionScope.from_path(scope)
                    if scope_is_path
                    else PermissionScope.identifier(scope)
                ),
                created_at=_T0,
                updated_at=_T0,
            )
        )

    def grant_code(self, *, action: PermissionAction, path: str) -> None:
        self.grant(
            resource=PermissionResource.CODE,
            action=action,
            scope=f"sam-core/{path}",
            scope_is_path=True,
        )

    def grant_code_op(self, *, action: PermissionAction, segment: str) -> None:
        self.grant(
            resource=PermissionResource.CODE, action=action, scope=f"sam-core:{segment}"
        )

    def grant_git(self, *, segment: str) -> None:
        self.grant(
            resource=PermissionResource.GIT,
            action=PermissionAction.READ,
            scope=f"sam-core:{segment}",
        )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n")
    _init_git(tmp_path)
    return tmp_path


# --------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------- #


def test_read_file_success(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code(action=PermissionAction.READ, path="src")
    result = h.executor.execute(_read_op())
    assert result.outcome is ExecutionOutcome.SUCCESS
    assert result.file_read is not None
    assert result.file_read.content == "print('hi')\n"


def test_create_file_success(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code(action=PermissionAction.WRITE, path="new.py")
    change = FileChange(
        path="new.py", operation=FileChangeKind.CREATE, new_content="x = 1\n"
    )
    result = h.executor.execute(
        CreateFileOperation(principal=_principal(), change=change)
    )
    assert result.outcome is ExecutionOutcome.SUCCESS
    assert result.file_write is not None


def test_modify_file_success(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code(action=PermissionAction.READ, path="src")
    h.grant_code(action=PermissionAction.WRITE, path="src")
    read = h.executor.execute(_read_op())
    assert read.file_read is not None
    change = FileChange(
        path="src/main.py",
        operation=FileChangeKind.MODIFY,
        expected_original_hash=read.file_read.content_hash,
        new_content="print('updated')\n",
    )
    result = h.executor.execute(
        ModifyFileOperation(principal=_principal(), change=change)
    )
    assert result.outcome is ExecutionOutcome.SUCCESS


def test_git_status_success(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_git(segment="status")
    result = h.executor.execute(GetGitStatusOperation(principal=_principal()))
    assert result.outcome is ExecutionOutcome.SUCCESS
    assert result.git_status is not None


# --------------------------------------------------------------------- #
# Permission boundary
# --------------------------------------------------------------------- #


def test_read_without_grant_is_denied(repo: Path) -> None:
    h = _Harness(repo)
    result = h.executor.execute(_read_op())
    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error_category is ErrorCategory.PERMISSION_DENIED


def test_write_grant_does_not_authorize_read(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code(action=PermissionAction.WRITE, path="src")
    result = h.executor.execute(_read_op())
    assert result.outcome is ExecutionOutcome.FAILED


def test_revoked_grant_denies(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code(action=PermissionAction.READ, path="src")
    h.store.revoke_grant("g-code-read-sam-core/src", now=_T0)
    result = h.executor.execute(_read_op())
    assert result.outcome is ExecutionOutcome.FAILED


def test_expired_grant_denies(repo: Path) -> None:
    h = _Harness(repo)
    h.store.create_grant(
        PermissionGrant(
            grant_id="g1",
            principal=_principal(),
            resource=PermissionResource.CODE,
            action=PermissionAction.READ,
            scope=PermissionScope.from_path("sam-core/src"),
            created_at=_T0,
            updated_at=_T0,
            expires_at=_T0 + timedelta(hours=1),
        )
    )
    h.advance(timedelta(hours=2))
    result = h.executor.execute(_read_op())
    assert result.outcome is ExecutionOutcome.FAILED


# --------------------------------------------------------------------- #
# Confirmation
# --------------------------------------------------------------------- #


def test_run_tests_requires_confirmation(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code_op(action=PermissionAction.EXECUTE, segment="tests")
    result = h.executor.execute(RunTestsOperation(principal=_principal()))
    assert result.permission_outcome is PermissionOutcomeSummary.CONFIRM_REQUIRED
    assert result.confirmation_id is not None


def test_run_tests_succeeds_after_confirmation(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code_op(action=PermissionAction.EXECUTE, segment="tests")
    first = h.executor.execute(RunTestsOperation(principal=_principal()))
    assert first.confirmation_id is not None
    h.confirmations.decide(first.confirmation_id, approved=True, now=_T0)
    second = h.executor.execute(
        RunTestsOperation(principal=_principal()), confirmation_id=first.confirmation_id
    )
    assert second.outcome is ExecutionOutcome.SUCCESS
    assert second.verification_status is not None


def test_confirmation_replay_is_rejected(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code_op(action=PermissionAction.EXECUTE, segment="tests")
    first = h.executor.execute(RunTestsOperation(principal=_principal()))
    assert first.confirmation_id is not None
    h.confirmations.decide(first.confirmation_id, approved=True, now=_T0)
    used = h.executor.execute(
        RunTestsOperation(principal=_principal()), confirmation_id=first.confirmation_id
    )
    assert used.outcome is ExecutionOutcome.SUCCESS
    replay = h.executor.execute(
        RunTestsOperation(principal=_principal()), confirmation_id=first.confirmation_id
    )
    assert replay.outcome is ExecutionOutcome.FAILED
    assert replay.error_category is ErrorCategory.CONFIRMATION_INVALID


# --------------------------------------------------------------------- #
# Optimistic concurrency
# --------------------------------------------------------------------- #


def test_stale_hash_write_is_rejected(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code(action=PermissionAction.READ, path="src")
    h.grant_code(action=PermissionAction.WRITE, path="src")
    read = h.executor.execute(_read_op())
    assert read.file_read is not None
    (repo / "src" / "main.py").write_text("print('concurrent change')\n")
    change = FileChange(
        path="src/main.py",
        operation=FileChangeKind.MODIFY,
        expected_original_hash=read.file_read.content_hash,
        new_content="print('my change')\n",
    )
    result = h.executor.execute(
        ModifyFileOperation(principal=_principal(), change=change)
    )
    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error_category is ErrorCategory.CONCURRENT_MODIFICATION


# --------------------------------------------------------------------- #
# Fail-closed
# --------------------------------------------------------------------- #


class _RaisingStore:
    def create_grant(self, grant: object) -> object:
        raise PermissionStoreError("unavailable")

    def get_grant(self, grant_id: str) -> object:
        raise PermissionStoreError("unavailable")

    def list_grants(self, principal: object, **kwargs: object) -> Sequence[object]:
        raise PermissionStoreError("unavailable")

    def revoke_grant(self, grant_id: str, *, now: object) -> object:
        raise PermissionStoreError("unavailable")


def test_permission_engine_failure_fails_closed(repo: Path) -> None:
    """A raising permission store never becomes ALLOW — note the
    Permission Engine itself already fails closed internally (see
    ``sam.permissions.engine.PermissionEngine.evaluate``), returning a
    DENY decision rather than propagating an exception; the outer
    ``CodingExecutor`` layer is defense in depth on top of that, covered
    by the next test using a raising repository instead."""

    h = _Harness(repo)
    engine = PermissionEngine(
        store=_RaisingStore(),  # type: ignore[arg-type]
        confirmation_provider=InMemoryConfirmationProvider(),
        audit_sink=InMemoryPermissionAuditSink(),
        clock=lambda: _T0,
    )
    executor = CodingExecutor(repository=h.repository, permission_engine=engine)
    result = executor.execute(_read_op())
    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error_category is ErrorCategory.PERMISSION_DENIED


def test_unexpected_executor_level_failure_fails_closed(repo: Path) -> None:
    """A bug/unexpected exception surfacing directly inside
    ``CodingExecutor`` (not already absorbed by the Permission Engine)
    is still converted to a safe FAILED/INTERNAL_ERROR result, never
    ALLOW/SUCCESS."""

    h = _Harness(repo)
    h.grant_code(action=PermissionAction.READ, path="src")

    class _BrokenExecutor(CodingExecutor):
        def _call_backend(self, request: object, operation: object) -> object:  # type: ignore[override]
            raise RuntimeError("boom - not a CodingEngineError")

    executor = _BrokenExecutor(
        repository=h.repository, permission_engine=h.engine, audit_sink=h.audit
    )
    result = executor.execute(_read_op())
    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error_category is ErrorCategory.INTERNAL_ERROR


def test_no_secret_leaks_via_exception_details(repo: Path) -> None:
    h = _Harness(repo)
    engine = PermissionEngine(
        store=_RaisingStore(),  # type: ignore[arg-type]
        confirmation_provider=InMemoryConfirmationProvider(),
        audit_sink=InMemoryPermissionAuditSink(),
        clock=lambda: _T0,
    )
    executor = CodingExecutor(repository=h.repository, permission_engine=engine)
    result = executor.execute(_read_op())
    serialized = result.model_dump_json()
    assert "unavailable" not in serialized
    assert "PermissionStoreError" not in serialized


# --------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------- #


def test_every_execution_generates_exactly_one_audit_event(repo: Path) -> None:
    h = _Harness(repo)
    h.executor.execute(_read_op())
    assert len(h.audit.list_events()) == 1


def test_audit_failure_does_not_change_the_returned_result(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code(action=PermissionAction.READ, path="src")
    executor = CodingExecutor(
        repository=h.repository,
        permission_engine=h.engine,
        audit_sink=FailingCodingAuditSink(),
        clock=lambda: _T0,
    )
    result = executor.execute(_read_op())
    assert result.outcome is ExecutionOutcome.SUCCESS


def test_engine_works_without_an_audit_sink(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code(action=PermissionAction.READ, path="src")
    executor = CodingExecutor(repository=h.repository, permission_engine=h.engine)
    result = executor.execute(_read_op())
    assert result.outcome is ExecutionOutcome.SUCCESS


# --------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------- #


def test_two_independent_executors_do_not_share_state(repo: Path) -> None:
    h1 = _Harness(repo)
    h2 = _Harness(repo)
    h1.grant_code(action=PermissionAction.READ, path="src")

    r1 = h1.executor.execute(_read_op())
    r2 = h2.executor.execute(_read_op())

    assert r1.outcome is ExecutionOutcome.SUCCESS
    assert r2.outcome is ExecutionOutcome.FAILED
