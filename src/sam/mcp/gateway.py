"""The MCP gateway: the single, finite, fail-closed path from a tool request
to an external side effect.

Lifecycle (each stage can only stop the request, never widen it):

    1. parse the canonical tool id            -> REJECTED
    2. resolve exactly one trusted registry entry
    3. tool enabled?  (extra restriction only) -> REJECTED
    4. duplicate request id?                   -> REJECTED
    5. validate arguments against Sam's schema -> REJECTED
    6. build deterministic scope + PermissionRequest  (policy; builds, never decides)
    7. PermissionEngine.evaluate()   <- the ONLY authorization authority
         DENY -> DENIED;  CONFIRM_REQUIRED -> CONFIRMATION_REQUIRED (nothing runs)
    8. (ALLOW only) resolve the credential reference at the execution boundary
    9. reserve the request id, then make AT MOST ONE transport call
   10. enforce Sam's timeout; validate size/shape; reject credential echo
   11. verify where the registry entry requires it
   12. emit one content-free audit event
   13. return a normalized ``MCPExecutionResult``

``execute`` never raises: an unexpected failure anywhere becomes ``FAILED``
(internal_error), never an implicit success. There are no retries — a
timeout means the outcome is unknown, so nothing is ever re-sent; a caller
must issue a new request explicitly.

Tool output is data. Nothing in this module reads a result to decide
anything; a follow-up tool call must enter ``execute`` again and pass the
whole pipeline (including the PermissionEngine) from stage 1.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Protocol

from sam.mcp import policy
from sam.mcp.audit import MCPAuditSink
from sam.mcp.credentials import Credential, CredentialProvider
from sam.mcp.errors import (
    MCPError,
    MCPInputValidationError,
    MCPPolicyError,
    MCPToolNotFoundError,
)
from sam.mcp.execution import MCPExecutor
from sam.mcp.models import (
    MAX_TRACKED_REQUEST_IDS,
    MCPAuditEvent,
    MCPErrorCategory,
    MCPExecutionContext,
    MCPExecutionResult,
    MCPExecutionStatus,
    MCPToolCall,
    MCPToolDescriptor,
    MCPToolId,
    MCPToolRequest,
    MCPToolResult,
    MCPVerificationResult,
    PermissionOutcomeSummary,
    VerificationStatus,
    canonical_json,
    new_id,
    utc_now,
)
from sam.mcp.registry import ADMIN_CAPABILITY_NAMES, MCPRegistryReader
from sam.mcp.transport import MCPTransport
from sam.mcp.validation import validate_arguments
from sam.mcp.verification import MCPResultVerifier
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    DecisionOutcome,
    DenialReason,
    PermissionDecision,
    RiskLevel,
)

_REASON_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")


class MCPToolInvoker(Protocol):
    """The typed boundary AgentCore (or any caller) depends on."""

    def execute(
        self, request: MCPToolRequest, *, confirmation_id: str | None = None
    ) -> MCPExecutionResult: ...


@dataclass
class _Run:
    """Per-request bookkeeping used only to build the result and the audit
    event. Holds no argument values, results, or credentials."""

    execution_id: str
    started: float
    descriptor: MCPToolDescriptor | None = None
    tool_id_text: str | None = None
    scope_text: str | None = None
    risk: RiskLevel | None = None
    permission_outcome: PermissionOutcomeSummary | None = None
    input_size: int | None = None
    output_size: int | None = None
    attempted: bool = False
    verification: MCPVerificationResult | None = None


class MCPGateway:
    def __init__(
        self,
        *,
        registry: MCPRegistryReader,
        permission_engine: PermissionEngine,
        transports: Mapping[str, MCPTransport],
        credential_provider: CredentialProvider,
        verifiers: Mapping[str, MCPResultVerifier] | None = None,
        audit_sink: MCPAuditSink | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        exposed = sorted(n for n in ADMIN_CAPABILITY_NAMES if hasattr(registry, n))
        if exposed:
            # Structural guard: the runtime path must never hold a registry
            # capability that can mutate trusted state.
            raise TypeError("MCPGateway requires a read-only registry")
        self._registry = registry
        self._permission_engine = permission_engine
        self._executor = MCPExecutor(transports)
        self._credentials = credential_provider
        self._verifiers: dict[str, MCPResultVerifier] = dict(verifiers or {})
        self._audit = audit_sink
        self._clock = clock
        self._executed: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._executed_lock = RLock()

    # ------------------------------------------------------------------ #
    # execute
    # ------------------------------------------------------------------ #

    def execute(
        self, request: MCPToolRequest, *, confirmation_id: str | None = None
    ) -> MCPExecutionResult:
        now = self._clock()
        run = _Run(execution_id=new_id(), started=time.monotonic())
        try:
            result = self._execute_unsafe(
                request, confirmation_id=confirmation_id, run=run, now=now
            )
        except Exception:
            result = self._result(
                request,
                run,
                now,
                MCPExecutionStatus.FAILED,
                MCPErrorCategory.INTERNAL_ERROR,
            )
        self._record_audit(request, run, result, now)
        return result

    def _execute_unsafe(
        self,
        request: MCPToolRequest,
        *,
        confirmation_id: str | None,
        run: _Run,
        now: datetime,
    ) -> MCPExecutionResult:
        rejected = MCPExecutionStatus.REJECTED

        # 1-2. canonical identity -> exactly one trusted entry
        try:
            tool_id = MCPToolId.parse(request.tool_id)
        except ValueError:
            return self._result(
                request, run, now, rejected, MCPErrorCategory.INVALID_TOOL_ID
            )
        run.tool_id_text = str(tool_id)
        try:
            descriptor = self._registry.get_tool(tool_id)
        except MCPToolNotFoundError:
            return self._result(
                request, run, now, rejected, MCPErrorCategory.UNKNOWN_TOOL
            )
        run.descriptor = descriptor

        # 3. enabled flag: an additional restriction, never a grant
        if not descriptor.enabled:
            return self._result(
                request, run, now, rejected, MCPErrorCategory.TOOL_DISABLED
            )

        # 4. duplicate request id (peek only — nothing is consumed yet)
        key = (request.principal.kind.value, request.principal.id, request.request_id)
        if self._already_executed(key):
            return self._result(
                request, run, now, rejected, MCPErrorCategory.DUPLICATE_REQUEST
            )

        # 5. validate against Sam's trusted schema
        try:
            arguments = validate_arguments(request.arguments, descriptor)
        except MCPInputValidationError as error:
            return self._result(request, run, now, rejected, error.category)
        run.input_size = len(canonical_json(arguments).encode("utf-8"))

        # 6. policy: build (never decide) the PermissionRequest
        try:
            permission_request = policy.build_permission_request(
                descriptor, request, arguments
            )
        except MCPPolicyError as error:
            return self._result(request, run, now, rejected, error.category)
        run.scope_text = permission_request.scope.as_text()

        # 7. the sole authorization authority
        decision = self._permission_engine.evaluate(
            permission_request, confirmation_id=confirmation_id
        )
        run.risk = decision.risk
        gate = self._gate(decision, request, run, now)
        if gate is not None:
            return gate

        # 8. credential, resolved only now, only for this server/tool
        credential: Credential | None = None
        if descriptor.credential is not None:
            try:
                credential = self._credentials.resolve(
                    descriptor.credential,
                    server_id=descriptor.server_id,
                    tool_name=descriptor.tool_name,
                )
            except Exception:
                return self._result(
                    request,
                    run,
                    now,
                    MCPExecutionStatus.FAILED,
                    MCPErrorCategory.CREDENTIAL_ERROR,
                )

        # 9. reserve the request id, then at most one transport call
        if not self._reserve(key):
            return self._result(
                request, run, now, rejected, MCPErrorCategory.DUPLICATE_REQUEST
            )
        run.attempted = True
        call = MCPToolCall(
            server_id=descriptor.server_id,
            tool_name=descriptor.tool_name,
            arguments=arguments,
        )
        context = MCPExecutionContext(
            request_id=request.request_id,
            execution_id=run.execution_id,
            principal=request.principal,
            timeout_seconds=descriptor.timeout_seconds,
        )

        # 10. execute under Sam's timeout; validate the untrusted result
        try:
            content_json, size = self._executor.execute(
                descriptor, call, context, credential
            )
        except MCPError as error:
            return self._result(
                request, run, now, MCPExecutionStatus.FAILED, error.category
            )
        run.output_size = size

        # 11. verification, only if the trusted entry requires it
        run.verification = self._verify(descriptor, call, context, content_json)
        tool_result = MCPToolResult(
            server_id=descriptor.server_id,
            tool_id=str(descriptor.tool_id),
            execution_id=run.execution_id,
            content_json=content_json,
            size_bytes=size,
        )
        status = run.verification.status
        if status is VerificationStatus.FAILED:
            return self._result(
                request,
                run,
                now,
                MCPExecutionStatus.FAILED,
                MCPErrorCategory.VERIFICATION_FAILED,
            )
        if status is VerificationStatus.UNVERIFIED:
            return self._result(
                request,
                run,
                now,
                MCPExecutionStatus.UNVERIFIED,
                result=tool_result,
                permission=PermissionOutcomeSummary.ALLOW,
            )
        return self._result(
            request,
            run,
            now,
            MCPExecutionStatus.SUCCEEDED,
            result=tool_result,
            permission=PermissionOutcomeSummary.ALLOW,
        )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _gate(
        self,
        decision: PermissionDecision,
        request: MCPToolRequest,
        run: _Run,
        now: datetime,
    ) -> MCPExecutionResult | None:
        if decision.outcome is DecisionOutcome.DENY:
            run.permission_outcome = PermissionOutcomeSummary.DENY
            category = (
                MCPErrorCategory.CONFIRMATION_INVALID
                if decision.reason is DenialReason.CONFIRMATION_INVALID
                else MCPErrorCategory.PERMISSION_DENIED
            )
            return self._result(request, run, now, MCPExecutionStatus.DENIED, category)
        if decision.outcome is DecisionOutcome.CONFIRM_REQUIRED:
            run.permission_outcome = PermissionOutcomeSummary.CONFIRM_REQUIRED
            pending = (
                decision.confirmation.confirmation_id if decision.confirmation else None
            )
            return self._result(
                request,
                run,
                now,
                MCPExecutionStatus.CONFIRMATION_REQUIRED,
                MCPErrorCategory.CONFIRMATION_REQUIRED,
                confirmation_id=pending,
            )
        run.permission_outcome = PermissionOutcomeSummary.ALLOW
        return None

    def _verify(
        self,
        descriptor: MCPToolDescriptor,
        call: MCPToolCall,
        context: MCPExecutionContext,
        content_json: str,
    ) -> MCPVerificationResult:
        if not descriptor.verification_required:
            return MCPVerificationResult(status=VerificationStatus.NOT_REQUIRED)
        verifier = self._verifiers.get(str(descriptor.tool_id))
        if verifier is None:
            return MCPVerificationResult(
                status=VerificationStatus.UNVERIFIED, reason_code="no_verifier"
            )
        try:
            verdict = verifier.verify(descriptor, call, context, content_json)
        except Exception:
            return MCPVerificationResult(
                status=VerificationStatus.FAILED, reason_code="verifier_error"
            )
        reason = (
            verdict.reason_code
            if verdict.reason_code and _REASON_CODE_RE.match(verdict.reason_code)
            else None
        )
        if verdict.status is VerificationStatus.NOT_REQUIRED:
            # Required but the verifier claims it isn't: never upgrade.
            return MCPVerificationResult(
                status=VerificationStatus.UNVERIFIED, reason_code="inconclusive"
            )
        return MCPVerificationResult(status=verdict.status, reason_code=reason)

    def _already_executed(self, key: tuple[str, str, str]) -> bool:
        with self._executed_lock:
            return key in self._executed

    def _reserve(self, key: tuple[str, str, str]) -> bool:
        with self._executed_lock:
            if key in self._executed:
                return False
            self._executed[key] = None
            while len(self._executed) > MAX_TRACKED_REQUEST_IDS:
                self._executed.popitem(last=False)
            return True

    def _result(
        self,
        request: MCPToolRequest,
        run: _Run,
        now: datetime,
        status: MCPExecutionStatus,
        category: MCPErrorCategory | None = None,
        *,
        result: MCPToolResult | None = None,
        permission: PermissionOutcomeSummary | None = None,
        confirmation_id: str | None = None,
    ) -> MCPExecutionResult:
        return MCPExecutionResult(
            request_id=request.request_id,
            execution_id=run.execution_id,
            principal=request.principal,
            tool_id=run.tool_id_text,
            status=status,
            permission_outcome=permission or run.permission_outcome,
            error_category=category,
            confirmation_id=confirmation_id,
            result=result,
            verification=run.verification,
            execution_attempted=run.attempted,
            duration_ms=int((time.monotonic() - run.started) * 1000),
            created_at=now,
        )

    def _record_audit(
        self,
        request: MCPToolRequest,
        run: _Run,
        result: MCPExecutionResult,
        now: datetime,
    ) -> None:
        if self._audit is None:
            return
        descriptor = run.descriptor
        try:
            event = MCPAuditEvent(
                event_id=new_id(),
                occurred_at=now,
                request_id=request.request_id,
                execution_id=run.execution_id,
                principal=request.principal,
                server_id=descriptor.server_id if descriptor else None,
                tool_id=run.tool_id_text,
                permission_action=descriptor.binding.action if descriptor else None,
                permission_resource=descriptor.binding.resource if descriptor else None,
                risk=run.risk,
                scope=run.scope_text,
                authorization_outcome=result.permission_outcome,
                execution_status=result.status,
                verification_status=(
                    result.verification.status if result.verification else None
                ),
                error_category=result.error_category,
                execution_attempted=result.execution_attempted,
                duration_ms=result.duration_ms,
                input_size=run.input_size,
                output_size=run.output_size,
            )
            self._audit.record(event)
        except Exception:
            # Best-effort observability, not an authorization gate — the
            # decision and any side effect already happened; a failing sink
            # can neither create an allow nor undo a deny.
            return


__all__ = ["MCPGateway", "MCPToolInvoker"]
