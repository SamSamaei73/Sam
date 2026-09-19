"""Verification status interpretation and the bounded verification pipeline.

**Verification is separate from execution** (see the Phase 6 task): a
``RUN_TESTS``/``RUN_LINT``/``RUN_TYPECHECK``/``VERIFY_DIFF`` operation
*executing* successfully means the command ran to completion — it says
nothing about whether the check itself passed. ``status_from_command_result``
is the one place that turns a raw ``CommandExecutionResult`` into the
closed ``VerificationStatus`` vocabulary (``PASSED``/``FAILED``/
``NOT_RUN``/``BLOCKED``/``TIMEOUT``); ``NOT_RUN`` is never silently
promoted to ``PASSED`` — a step that was never executed (denied,
confirmation-required, or simply not included in a pipeline) is always
reported as ``NOT_RUN``, distinct from a step that ran and failed.

``run_verification_pipeline`` is a thin orchestrator: it calls
``CodingExecutor.execute`` for a small, fixed, ordered sequence of
verification operations — never a caller-expandable list of arbitrary
operations — and aggregates their statuses. Every step still passes
through the full policy → Permission Engine → executor path; this module
adds no authorization logic and never talks to ``RepositoryBackend``
directly. There is no import cycle with ``sam.coding.executor``: that
module imports ``status_from_command_result`` from here lazily (inside
the one method that needs it), so this module can import
``CodingExecutor`` normally for type-checking purposes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from sam.coding.errors import CommandTimeoutError
from sam.coding.executor import CodingExecutor
from sam.coding.models import (
    MAX_PLAN_STEPS,
    CodingOperation,
    CodingOperationResult,
    CommandExecutionResult,
    ErrorCategory,
    ExecutionOutcome,
    PermissionOutcomeSummary,
    VerificationStatus,
)
from sam.coding.operations import (
    CodingOperationRequest,
    RunLintOperation,
    RunTestsOperation,
    RunTypecheckOperation,
    VerifyDiffOperation,
)
from sam.permissions.models import Principal

# The canonical, fixed verification pipeline the Phase 6 task specifies.
# A caller may select any *subset* of these (never a step outside this
# tuple) via `steps=` on `run_verification_pipeline`.
DEFAULT_VERIFICATION_STEPS: tuple[CodingOperation, ...] = (
    CodingOperation.VERIFY_DIFF,
    CodingOperation.RUN_TESTS,
    CodingOperation.RUN_LINT,
    CodingOperation.RUN_TYPECHECK,
)

_RequestBuilder = Callable[..., CodingOperationRequest]

_REQUEST_BUILDERS: dict[CodingOperation, _RequestBuilder] = {
    CodingOperation.RUN_TESTS: RunTestsOperation,
    CodingOperation.RUN_LINT: RunLintOperation,
    CodingOperation.RUN_TYPECHECK: RunTypecheckOperation,
    CodingOperation.VERIFY_DIFF: VerifyDiffOperation,
}


def status_from_command_result(
    operation: CodingOperation, result: CommandExecutionResult
) -> VerificationStatus:
    """The one place a ``CommandExecutionResult`` becomes a
    ``VerificationStatus``. A timed-out command is never silently
    treated as anything but a raised ``CommandTimeoutError`` (caught by
    ``CodingExecutor``, which reports the operation itself as FAILED —
    see its module docstring); a nonzero exit code is always ``FAILED``,
    never coerced to ``PASSED``."""

    if result.timed_out:
        raise CommandTimeoutError(f"{operation.value} timed out")
    if result.exit_code == 0:
        return VerificationStatus.PASSED
    return VerificationStatus.FAILED


class VerificationStepResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation: CodingOperation
    status: VerificationStatus
    permission_outcome: PermissionOutcomeSummary


class VerificationReport(BaseModel):
    """The aggregated result of a bounded verification pipeline run.
    ``overall`` is ``PASSED`` only if every executed step passed;
    ``TIMEOUT``/``BLOCKED``/``FAILED``/``NOT_RUN`` otherwise, in that
    priority order — never silently treated as passing."""

    model_config = ConfigDict(frozen=True)

    steps: tuple[VerificationStepResult, ...] = Field(max_length=MAX_PLAN_STEPS)
    overall: VerificationStatus


def run_verification_pipeline(
    executor: CodingExecutor,
    *,
    principal: Principal,
    steps: Sequence[CodingOperation] = DEFAULT_VERIFICATION_STEPS,
    confirmation_ids: dict[CodingOperation, str] | None = None,
) -> VerificationReport:
    """Run a bounded, fixed set of verification operations through the
    full authorization path. Every step is attempted independently
    (unlike a mutating action sequence, these are read-only checks with
    no interdependent side effects, so one step failing does not stop
    the others — the caller gets a complete picture) and reported
    without ever converting ``NOT_RUN``/``BLOCKED`` into ``PASSED``.

    ``confirmation_ids`` lets a caller who already obtained an approved
    confirmation for a HIGH-risk step (RUN_TESTS/RUN_LINT/RUN_TYPECHECK)
    supply it — the same one-time, context-bound confirmation as any
    other operation; a missing or invalid entry simply leaves that step
    ``CONFIRM_REQUIRED``/``BLOCKED``, never silently skipped as passing.
    """

    invalid = set(steps) - set(DEFAULT_VERIFICATION_STEPS)
    if invalid:
        raise ValueError(f"not a verification step: {invalid}")
    if len(steps) > MAX_PLAN_STEPS:
        raise ValueError("verification pipeline exceeds the maximum step count")

    ids = confirmation_ids or {}
    results: list[VerificationStepResult] = []
    for operation in steps:
        request = _REQUEST_BUILDERS[operation](principal=principal)
        outcome = executor.execute(request, confirmation_id=ids.get(operation))
        results.append(
            VerificationStepResult(
                operation=operation,
                status=_status_from_result(outcome),
                permission_outcome=outcome.permission_outcome,
            )
        )
    return VerificationReport(steps=tuple(results), overall=_aggregate(results))


def _status_from_result(outcome: CodingOperationResult) -> VerificationStatus:
    if outcome.permission_outcome in (
        PermissionOutcomeSummary.DENY,
        PermissionOutcomeSummary.CONFIRM_REQUIRED,
    ):
        return VerificationStatus.BLOCKED
    if outcome.outcome is ExecutionOutcome.FAILED:
        if outcome.error_category is ErrorCategory.COMMAND_TIMEOUT:
            return VerificationStatus.TIMEOUT
        return VerificationStatus.NOT_RUN
    if outcome.verification_status is not None:
        return outcome.verification_status
    return VerificationStatus.NOT_RUN


def _aggregate(results: Sequence[VerificationStepResult]) -> VerificationStatus:
    if not results:
        return VerificationStatus.NOT_RUN
    statuses = {step.status for step in results}
    for priority in (
        VerificationStatus.TIMEOUT,
        VerificationStatus.BLOCKED,
        VerificationStatus.FAILED,
        VerificationStatus.NOT_RUN,
    ):
        if priority in statuses:
            return priority
    return VerificationStatus.PASSED


__all__ = [
    "DEFAULT_VERIFICATION_STEPS",
    "VerificationReport",
    "VerificationStepResult",
    "run_verification_pipeline",
    "status_from_command_result",
]
