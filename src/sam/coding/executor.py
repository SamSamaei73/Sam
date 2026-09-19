"""The Coding Executor.

Owns nothing except orchestration; every real decision is delegated:

    LLM / caller
       │
       ▼
    CodingOperationRequest
       │
       ▼
    sam.coding.policy.build_request()   (pure mapping — no decision)
       │
       ▼
    sam.permissions.engine.PermissionEngine.evaluate()   (the only authority)
       │
       ├─ DENY ─────────────────────────► stop, backend never called
       ├─ CONFIRM_REQUIRED ───────────────► stop, backend never called
       └─ ALLOW
            │
            ▼
       CodingExecutor._dispatch()
            │
            ▼
       RepositoryBackend

There is no shortcut anywhere in this module that calls
``RepositoryBackend`` without first obtaining ``ALLOW`` from
``PermissionEngine.evaluate`` — ``_execute_unsafe`` is the only caller of
``evaluate``, and ``_dispatch`` (the only caller of any
``RepositoryBackend`` method) is only ever reached from its ``ALLOW``
branch. ``execute`` never raises for a well-formed request: an
unexpected internal failure (a raising backend, a raising permission
engine, a bug) is caught and converted into a ``FAILED`` result — the
same fail-closed discipline as every prior phase's engine/controller.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sam.coding.audit import CodingAuditSink
from sam.coding.errors import (
    CodingEngineError,
    CommandPolicyError,
    CommandTimeoutError,
    ConcurrentModificationError,
    RepositoryBoundaryError,
    RepositoryNotFoundError,
    UnsafeOperationError,
)
from sam.coding.models import (
    CodingAuditEvent,
    CodingConfirmationOutcome,
    CodingOperation,
    CodingOperationResult,
    CommandExecutionResult,
    DirectoryListing,
    ErrorCategory,
    ExecutionOutcome,
    FileReadResult,
    FileWriteResult,
    GitDiffResult,
    GitStatusResult,
    PermissionOutcomeSummary,
    RiskLevel,
    VerificationStatus,
    utc_now,
)
from sam.coding.operations import (
    CodingOperationRequest,
    CreateFileOperation,
    GetGitDiffOperation,
    GetGitStatusOperation,
    InspectRepositoryOperation,
    ListFilesOperation,
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
from sam.coding.repository import RepositoryBackend
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import DecisionOutcome, DenialReason


@dataclass(frozen=True)
class _BackendPayload:
    """The typed, optional data a successful dispatch may carry — the
    same pattern ``sam.computer.controller`` uses, avoiding a loosely
    typed ``dict[str, object]`` result."""

    directory_listing: DirectoryListing | None = None
    file_read: FileReadResult | None = None
    git_status: GitStatusResult | None = None
    git_diff: GitDiffResult | None = None
    file_write: FileWriteResult | None = None
    command_result: CommandExecutionResult | None = None
    verification_status: VerificationStatus | None = None


class CodingExecutor:
    """Ties policy, the Permission Engine, one repository backend, and
    audit together. No global state: construct one per repository/task
    (or per test) with explicit collaborators — the same discipline as
    ``PermissionEngine``, ``MemoryEngine``, and ``ComputerController``.
    """

    def __init__(
        self,
        *,
        repository: RepositoryBackend,
        permission_engine: PermissionEngine,
        audit_sink: CodingAuditSink | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._permission_engine = permission_engine
        self._audit = audit_sink
        self._clock = clock

    @property
    def repository_id(self) -> str:
        return self._repository.context.repository_id

    def execute(
        self, request: CodingOperationRequest, *, confirmation_id: str | None = None
    ) -> CodingOperationResult:
        """Authorize, then (only on ALLOW) dispatch one operation. Never
        raises for a well-formed request."""

        now = self._clock()
        operation_id = uuid4().hex
        risk = RiskLevel.CRITICAL
        try:
            operation = operation_for(request)
            risk = risk_for(operation)
            result = self._execute_unsafe(
                request,
                operation=operation,
                operation_id=operation_id,
                confirmation_id=confirmation_id,
                now=now,
            )
        except Exception:
            # Fail closed: an unexpected failure anywhere above (a
            # raising permission engine, a raising backend, a bug) must
            # never be reported as success. The exception's own text is
            # never included — it may carry filesystem or provider
            # internals.
            result = CodingOperationResult(
                operation_id=operation_id,
                operation=CodingOperation.INSPECT_REPOSITORY,
                principal=request.principal,
                repository_id=self.repository_id,
                outcome=ExecutionOutcome.FAILED,
                permission_outcome=PermissionOutcomeSummary.DENY,
                created_at=now,
                error_category=ErrorCategory.INTERNAL_ERROR,
            )
        self._record_audit(result, risk=risk, now=now)
        return result

    def _execute_unsafe(
        self,
        request: CodingOperationRequest,
        *,
        operation: CodingOperation,
        operation_id: str,
        confirmation_id: str | None,
        now: datetime,
    ) -> CodingOperationResult:
        permission_request = build_request(request, repository_id=self.repository_id)
        decision = self._permission_engine.evaluate(
            permission_request, confirmation_id=confirmation_id
        )

        if decision.outcome is DecisionOutcome.DENY:
            error_category = (
                ErrorCategory.CONFIRMATION_INVALID
                if decision.reason is DenialReason.CONFIRMATION_INVALID
                else ErrorCategory.PERMISSION_DENIED
            )
            return self._result(
                operation_id,
                operation,
                request,
                now,
                outcome=ExecutionOutcome.FAILED,
                permission_outcome=PermissionOutcomeSummary.DENY,
                error_category=error_category,
            )

        if decision.outcome is DecisionOutcome.CONFIRM_REQUIRED:
            pending_id = (
                decision.confirmation.confirmation_id if decision.confirmation else None
            )
            return self._result(
                operation_id,
                operation,
                request,
                now,
                outcome=ExecutionOutcome.FAILED,
                permission_outcome=PermissionOutcomeSummary.CONFIRM_REQUIRED,
                error_category=ErrorCategory.CONFIRMATION_REQUIRED,
                confirmation_id=pending_id,
            )

        # ALLOW — and only ALLOW — reaches the repository backend.
        return self._dispatch(request, operation, operation_id=operation_id, now=now)

    def _dispatch(
        self,
        request: CodingOperationRequest,
        operation: CodingOperation,
        *,
        operation_id: str,
        now: datetime,
    ) -> CodingOperationResult:
        try:
            payload = self._call_backend(request, operation)
        except CodingEngineError as error:
            category = _error_category_for(error)
            return self._result(
                operation_id,
                operation,
                request,
                now,
                outcome=ExecutionOutcome.FAILED,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                error_category=category,
            )
        return self._result(
            operation_id,
            operation,
            request,
            now,
            outcome=ExecutionOutcome.SUCCESS,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
            payload=payload,
        )

    def _call_backend(
        self, request: CodingOperationRequest, operation: CodingOperation
    ) -> _BackendPayload:
        repo = self._repository
        if isinstance(request, InspectRepositoryOperation):
            return _BackendPayload(directory_listing=repo.list_files(None))
        if isinstance(request, ReadFileOperation):
            return _BackendPayload(
                file_read=self._read_file_with_secret_protection(request.path)
            )
        if isinstance(request, ListFilesOperation):
            return _BackendPayload(directory_listing=repo.list_files(request.path))
        if isinstance(request, GetGitStatusOperation):
            return _BackendPayload(git_status=repo.git_status())
        if isinstance(request, GetGitDiffOperation):
            return _BackendPayload(git_diff=repo.git_diff())
        if isinstance(request, CreateFileOperation):
            return _BackendPayload(file_write=repo.create_file(request.change))
        if isinstance(request, ModifyFileOperation):
            return _BackendPayload(file_write=repo.modify_file(request.change))
        verification_operation_types = (
            RunTestsOperation,
            RunLintOperation,
            RunTypecheckOperation,
            VerifyDiffOperation,
        )
        if isinstance(request, verification_operation_types):
            return self._verification_payload(operation)
        raise TypeError(f"unhandled coding operation request type: {type(request)!r}")

    def _read_file_with_secret_protection(self, path: str) -> FileReadResult:
        """Secret protection: never hand secret-looking content back to a
        caller (which may forward it to an external provider, Memory, or
        an audit trail) — see docs/coding-agent.md's "Secret protection"
        section. A best-effort, documented-as-non-exhaustive check."""

        result = self._repository.read_file(path)
        if result.is_binary or result.restricted:
            return result
        content = result.content or ""
        if is_restricted_filename(path) or is_restricted_content(content):
            return result.model_copy(update={"content": None, "restricted": True})
        return result

    def _verification_payload(self, operation: CodingOperation) -> _BackendPayload:
        from sam.coding.verifier import status_from_command_result

        command_result = self._repository.run_command(operation)
        status = status_from_command_result(operation, command_result)
        return _BackendPayload(
            command_result=command_result, verification_status=status
        )

    def _result(
        self,
        operation_id: str,
        operation: CodingOperation,
        request: CodingOperationRequest,
        now: datetime,
        *,
        outcome: ExecutionOutcome,
        permission_outcome: PermissionOutcomeSummary,
        error_category: ErrorCategory | None = None,
        confirmation_id: str | None = None,
        payload: _BackendPayload | None = None,
    ) -> CodingOperationResult:
        payload = payload or _BackendPayload()
        return CodingOperationResult(
            operation_id=operation_id,
            operation=operation,
            principal=request.principal,
            repository_id=self.repository_id,
            outcome=outcome,
            permission_outcome=permission_outcome,
            directory_listing=payload.directory_listing,
            file_read=payload.file_read,
            git_status=payload.git_status,
            git_diff=payload.git_diff,
            file_write=payload.file_write,
            command_result=payload.command_result,
            verification_status=payload.verification_status,
            created_at=now,
            error_category=error_category,
            confirmation_id=confirmation_id,
        )

    def _record_audit(
        self, result: CodingOperationResult, *, risk: RiskLevel, now: datetime
    ) -> None:
        if self._audit is None:
            return
        confirmation_outcome = None
        if result.permission_outcome is PermissionOutcomeSummary.DENY:
            confirmation_outcome = (
                CodingConfirmationOutcome.CONFIRM_REQUIRED
                if result.error_category is ErrorCategory.CONFIRMATION_INVALID
                else CodingConfirmationOutcome.DENIED
            )
        elif result.permission_outcome is PermissionOutcomeSummary.CONFIRM_REQUIRED:
            confirmation_outcome = CodingConfirmationOutcome.CONFIRM_REQUIRED

        affected: tuple[str, ...] = ()
        if result.file_write is not None:
            affected = (result.file_write.path,)
        elif result.file_read is not None:
            affected = (result.file_read.path,)

        event = CodingAuditEvent(
            event_id=uuid4().hex,
            occurred_at=now,
            operation_id=result.operation_id,
            principal=result.principal,
            repository_id=result.repository_id,
            operation=result.operation,
            risk=risk,
            permission_outcome=result.permission_outcome,
            confirmation_outcome=confirmation_outcome,
            execution_outcome=result.outcome,
            error_category=result.error_category,
            affected_paths=affected,
            verification_status=result.verification_status,
        )
        try:
            self._audit.record(event)
        except Exception:
            # Best-effort observability, not an authorization gate — the
            # same trade-off as every prior phase's engine/controller.
            return


def _error_category_for(error: CodingEngineError) -> ErrorCategory:
    """Map the *actual* exception raised by the repository backend to a
    closed ``ErrorCategory`` — never the exception's own message, only
    its type."""

    if isinstance(error, RepositoryBoundaryError):
        return ErrorCategory.REPOSITORY_BOUNDARY
    if isinstance(error, RepositoryNotFoundError):
        return ErrorCategory.FILE_NOT_FOUND
    if isinstance(error, ConcurrentModificationError):
        return ErrorCategory.CONCURRENT_MODIFICATION
    if isinstance(error, UnsafeOperationError):
        return ErrorCategory.VALIDATION_ERROR
    if isinstance(error, CommandPolicyError):
        return ErrorCategory.COMMAND_UNAVAILABLE
    if isinstance(error, CommandTimeoutError):
        return ErrorCategory.COMMAND_TIMEOUT
    return ErrorCategory.COMMAND_ERROR


__all__ = ["CodingExecutor"]
