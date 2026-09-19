"""Tests for verification status interpretation and the pipeline."""

from datetime import UTC, datetime

import pytest

from sam.coding.errors import CommandTimeoutError
from sam.coding.executor import CodingExecutor
from sam.coding.models import (
    CodingOperation,
    CommandExecutionResult,
    VerificationStatus,
)
from sam.coding.operations import RunLintOperation, RunTestsOperation
from sam.coding.repository import FakeCommandRunner, RepositoryBackend
from sam.coding.verifier import (
    DEFAULT_VERIFICATION_STEPS,
    run_verification_pipeline,
    status_from_command_result,
)
from sam.permissions.audit import InMemoryAuditSink
from sam.permissions.confirmation import InMemoryConfirmationProvider
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    PermissionAction,
    PermissionGrant,
    PermissionResource,
    PermissionScope,
    Principal,
    PrincipalKind,
)
from sam.permissions.store import InMemoryPermissionStore

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _principal() -> Principal:
    return Principal(kind=PrincipalKind.USER, id="ali")


def _command_result(**overrides: object) -> CommandExecutionResult:
    defaults: dict[str, object] = {
        "stdout": "",
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "duration_seconds": 0.1,
    }
    defaults.update(overrides)
    return CommandExecutionResult.model_validate(defaults)


# --------------------------------------------------------------------- #
# status_from_command_result
# --------------------------------------------------------------------- #


def test_zero_exit_code_is_passed() -> None:
    result = _command_result(exit_code=0)
    assert status_from_command_result(CodingOperation.RUN_TESTS, result) is (
        VerificationStatus.PASSED
    )


def test_nonzero_exit_code_is_failed() -> None:
    result = _command_result(exit_code=1)
    assert status_from_command_result(CodingOperation.RUN_TESTS, result) is (
        VerificationStatus.FAILED
    )


def test_timeout_raises_rather_than_returning_a_status() -> None:
    result = _command_result(timed_out=True, exit_code=None)
    with pytest.raises(CommandTimeoutError):
        status_from_command_result(CodingOperation.RUN_TESTS, result)


def test_not_run_is_never_silently_promoted_to_passed() -> None:
    """A step that never executed cannot become PASSED through this
    function — it can only be PASSED, FAILED, or raise (never
    default-constructed to PASSED for a missing/None exit code, except
    the one explicit `exit_code == 0` branch)."""

    result = _command_result(exit_code=None, timed_out=False)
    assert status_from_command_result(CodingOperation.RUN_TESTS, result) is (
        VerificationStatus.FAILED
    )


# --------------------------------------------------------------------- #
# run_verification_pipeline
# --------------------------------------------------------------------- #


class _Harness:
    def __init__(self, *, clock: datetime = _T0) -> None:
        self.store = InMemoryPermissionStore()
        self.confirmations = InMemoryConfirmationProvider()
        self._clock_value = clock
        self.engine = PermissionEngine(
            store=self.store,
            confirmation_provider=self.confirmations,
            audit_sink=InMemoryAuditSink(),
            clock=lambda: self._clock_value,
        )
        self.command_runner = FakeCommandRunner()

    def backend(self, tmp_path: object) -> RepositoryBackend:
        from sam.coding.models import RepositoryContext

        ctx = RepositoryContext(repository_id="sam-core", root=str(tmp_path))
        return RepositoryBackend(context=ctx, command_runner=self.command_runner)

    def grant(self, *, action: PermissionAction, scope: str) -> None:
        self.store.create_grant(
            PermissionGrant(
                grant_id=f"g-{scope}-{action.value}",
                principal=_principal(),
                resource=PermissionResource.CODE,
                action=action,
                scope=PermissionScope.identifier(scope),
                created_at=_T0,
                updated_at=_T0,
            )
        )

    def grant_git(self, *, scope: str) -> None:
        self.store.create_grant(
            PermissionGrant(
                grant_id=f"g-git-{scope}",
                principal=_principal(),
                resource=PermissionResource.GIT,
                action=PermissionAction.READ,
                scope=PermissionScope.identifier(scope),
                created_at=_T0,
                updated_at=_T0,
            )
        )


def test_pipeline_reports_blocked_when_no_grants_exist(tmp_path: object) -> None:
    h = _Harness()
    backend = h.backend(tmp_path)
    executor = CodingExecutor(
        repository=backend, permission_engine=h.engine, clock=lambda: _T0
    )

    report = run_verification_pipeline(
        executor, principal=_principal(), steps=[CodingOperation.RUN_TESTS]
    )

    assert report.overall is VerificationStatus.BLOCKED
    assert report.steps[0].status is VerificationStatus.BLOCKED


def test_pipeline_reports_passed_when_all_steps_pass(tmp_path: object) -> None:
    h = _Harness()
    h.grant(action=PermissionAction.EXECUTE, scope="sam-core:tests")
    h.grant(action=PermissionAction.EXECUTE, scope="sam-core:lint")
    backend = h.backend(tmp_path)
    executor = CodingExecutor(
        repository=backend, permission_engine=h.engine, clock=lambda: _T0
    )

    # Pre-obtain and approve a confirmation for each HIGH-risk step, the
    # same way a caller with a real approval flow would.
    tests_pending = executor.execute(_run_tests_request())
    assert tests_pending.confirmation_id is not None
    h.confirmations.decide(tests_pending.confirmation_id, approved=True, now=_T0)
    lint_pending = executor.execute(_run_lint_request())
    assert lint_pending.confirmation_id is not None
    h.confirmations.decide(lint_pending.confirmation_id, approved=True, now=_T0)

    report = run_verification_pipeline(
        executor,
        principal=_principal(),
        steps=[CodingOperation.RUN_TESTS, CodingOperation.RUN_LINT],
        confirmation_ids={
            CodingOperation.RUN_TESTS: tests_pending.confirmation_id,
            CodingOperation.RUN_LINT: lint_pending.confirmation_id,
        },
    )

    assert report.overall is VerificationStatus.PASSED
    assert all(step.status is VerificationStatus.PASSED for step in report.steps)


def test_pipeline_reports_failed_when_a_command_exits_nonzero(tmp_path: object) -> None:
    h = _Harness()
    h.grant(action=PermissionAction.EXECUTE, scope="sam-core:tests")
    h.command_runner.set_result(
        "uv", _command_result(exit_code=1, stdout="1 failed")
    )
    backend = h.backend(tmp_path)
    executor = CodingExecutor(
        repository=backend, permission_engine=h.engine, clock=lambda: _T0
    )

    pending = executor.execute(_run_tests_request())
    assert pending.confirmation_id is not None
    h.confirmations.decide(pending.confirmation_id, approved=True, now=_T0)

    report = run_verification_pipeline(
        executor,
        principal=_principal(),
        steps=[CodingOperation.RUN_TESTS],
        confirmation_ids={CodingOperation.RUN_TESTS: pending.confirmation_id},
    )

    assert report.overall is VerificationStatus.FAILED
    assert report.steps[0].status is VerificationStatus.FAILED


def test_pipeline_never_executes_more_than_the_canonical_steps(
    tmp_path: object,
) -> None:
    h = _Harness()
    backend = h.backend(tmp_path)
    executor = CodingExecutor(
        repository=backend, permission_engine=h.engine, clock=lambda: _T0
    )

    with pytest.raises(ValueError, match="not a verification step"):
        run_verification_pipeline(
            executor, principal=_principal(), steps=[CodingOperation.READ_FILE]
        )


def test_default_verification_steps_match_the_task_specified_pipeline() -> None:
    assert DEFAULT_VERIFICATION_STEPS == (
        CodingOperation.VERIFY_DIFF,
        CodingOperation.RUN_TESTS,
        CodingOperation.RUN_LINT,
        CodingOperation.RUN_TYPECHECK,
    )


def test_pipeline_continues_past_a_failed_step(tmp_path: object) -> None:
    """Unlike a mutating action sequence, verification steps are
    independent reads — one failing/blocked step does not stop the
    others from being attempted."""

    h = _Harness()
    h.grant(action=PermissionAction.EXECUTE, scope="sam-core:lint")
    backend = h.backend(tmp_path)
    executor = CodingExecutor(
        repository=backend, permission_engine=h.engine, clock=lambda: _T0
    )

    report = run_verification_pipeline(
        executor,
        principal=_principal(),
        steps=[CodingOperation.RUN_TESTS, CodingOperation.RUN_LINT],
    )

    assert len(report.steps) == 2
    assert report.steps[0].status is VerificationStatus.BLOCKED  # no grant
    assert report.steps[1].status is VerificationStatus.BLOCKED  # no confirmation


def _run_tests_request() -> RunTestsOperation:
    return RunTestsOperation(principal=_principal())


def _run_lint_request() -> RunLintOperation:
    return RunLintOperation(principal=_principal())

    return RunLintOperation(principal=_principal())
