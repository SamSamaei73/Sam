"""The Computer Controller.

Owns policy and safety; a backend only performs the OS call it is given.
Every action follows exactly one path:

    LLM / caller
       │
       ▼
    ComputerAction
       │
       ▼
    sam.computer.policy.build_request()   (pure mapping, no decision)
       │
       ▼
    sam.permissions.engine.PermissionEngine.evaluate()   (the only authority)
       │
       ▼
    ALLOW ──────────────────────────────┐
    CONFIRM_REQUIRED → stop, never dispatch
    DENY → stop, never dispatch          │
                                         ▼
                                  ComputerBackend

There is no shortcut anywhere in this module that calls a backend method
without first obtaining an ``ALLOW`` from ``PermissionEngine.evaluate`` —
see ``_execute_unsafe``, the only place ``_dispatch`` is called from, and
``_dispatch``, the only place any ``ComputerBackend`` method is called
from.

``execute`` never raises for a well-formed action: an unexpected internal
failure (a raising backend, a raising permission engine, a bug) is caught
and converted into a ``FAILED`` result — the same fail-closed discipline
as ``sam.permissions.engine.PermissionEngine.evaluate`` and
``sam.memory.engine.MemoryEngine``. A ``SUCCESS`` outcome is only ever
returned once the backend call itself has returned without raising.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sam.computer.audit import ComputerAuditSink
from sam.computer.backend import ComputerBackend
from sam.computer.errors import (
    ActionSequenceTooLongError,
    BackendError,
    BackendUnavailableError,
    InvalidComputerActionError,
    OutOfBoundsCoordinateError,
)
from sam.computer.models import (
    ActiveWindow,
    ComputerAction,
    ComputerActionResult,
    ComputerAuditEvent,
    ComputerAuditOutcome,
    ErrorCategory,
    ExecutionOutcome,
    GetActiveWindowAction,
    GetScreenSizeAction,
    KeyCombinationAction,
    KeyPressAction,
    MouseClickAction,
    MouseDoubleClickAction,
    MouseMoveAction,
    MouseScrollAction,
    PermissionOutcomeSummary,
    ScreenshotAction,
    ScreenshotResult,
    ScreenSize,
    TypeTextAction,
    utc_now,
)
from sam.computer.policy import build_request, risk_for
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import DecisionOutcome, DenialReason, RiskLevel

DEFAULT_MAX_SEQUENCE_LENGTH = 10


@dataclass(frozen=True)
class _BackendPayload:
    """The typed, optional data a successful dispatch may carry."""

    screen_size: ScreenSize | None = None
    active_window: ActiveWindow | None = None
    screenshot: ScreenshotResult | None = None


class ComputerController:
    """Ties policy, the Permission Engine, a backend, and audit together.

    No global state: construct one per application (or per test) with
    explicit collaborators — the same discipline as ``PermissionEngine``
    and ``MemoryEngine``.
    """

    def __init__(
        self,
        *,
        backend: ComputerBackend,
        permission_engine: PermissionEngine,
        audit_sink: ComputerAuditSink | None = None,
        clock: Callable[[], datetime] = utc_now,
        max_sequence_length: int = DEFAULT_MAX_SEQUENCE_LENGTH,
    ) -> None:
        if max_sequence_length <= 0:
            raise ValueError("max_sequence_length must be positive")
        self._backend = backend
        self._permission_engine = permission_engine
        self._audit = audit_sink
        self._clock = clock
        self._max_sequence_length = max_sequence_length

    def execute(
        self,
        action: ComputerAction,
        *,
        confirmation_id: str | None = None,
        reason: str | None = None,
    ) -> ComputerActionResult:
        """Authorize, then (only on ALLOW) dispatch one action. Never raises."""

        now = self._clock()
        action_id = uuid4().hex
        risk = RiskLevel.CRITICAL
        try:
            risk = risk_for(action.capability)
            result = self._execute_unsafe(
                action,
                action_id=action_id,
                confirmation_id=confirmation_id,
                reason=reason,
                now=now,
            )
        except Exception:
            # Fail closed: an unexpected failure anywhere above (a
            # raising permission engine, a bug) must never be reported as
            # success. The exception's own text is never included — it
            # may carry backend- or provider-specific internals.
            result = ComputerActionResult(
                action_id=action_id,
                capability=action.capability,
                principal=action.principal,
                outcome=ExecutionOutcome.FAILED,
                permission_outcome=PermissionOutcomeSummary.DENY,
                created_at=now,
                error_category=ErrorCategory.INTERNAL_ERROR,
            )
        self._record_audit(result, risk=risk, now=now)
        return result

    def _execute_unsafe(
        self,
        action: ComputerAction,
        *,
        action_id: str,
        confirmation_id: str | None,
        reason: str | None,
        now: datetime,
    ) -> ComputerActionResult:
        request = build_request(action, reason=reason)
        decision = self._permission_engine.evaluate(
            request, confirmation_id=confirmation_id
        )

        if decision.outcome is DecisionOutcome.DENY:
            error_category = (
                ErrorCategory.CONFIRMATION_INVALID
                if decision.reason is DenialReason.CONFIRMATION_INVALID
                else ErrorCategory.PERMISSION_DENIED
            )
            return ComputerActionResult(
                action_id=action_id,
                capability=action.capability,
                principal=action.principal,
                outcome=ExecutionOutcome.FAILED,
                permission_outcome=PermissionOutcomeSummary.DENY,
                created_at=now,
                error_category=error_category,
            )

        if decision.outcome is DecisionOutcome.CONFIRM_REQUIRED:
            pending_id = (
                decision.confirmation.confirmation_id if decision.confirmation else None
            )
            return ComputerActionResult(
                action_id=action_id,
                capability=action.capability,
                principal=action.principal,
                outcome=ExecutionOutcome.FAILED,
                permission_outcome=PermissionOutcomeSummary.CONFIRM_REQUIRED,
                created_at=now,
                error_category=ErrorCategory.CONFIRMATION_REQUIRED,
                confirmation_id=pending_id,
            )

        # ALLOW — and only ALLOW — reaches the backend.
        return self._dispatch(action, action_id=action_id, now=now)

    def _dispatch(
        self, action: ComputerAction, *, action_id: str, now: datetime
    ) -> ComputerActionResult:
        try:
            payload = self._call_backend(action)
        except OutOfBoundsCoordinateError:
            return self._failed(
                action,
                action_id=action_id,
                now=now,
                category=ErrorCategory.OUT_OF_BOUNDS,
            )
        except BackendUnavailableError:
            return self._failed(
                action,
                action_id=action_id,
                now=now,
                category=ErrorCategory.BACKEND_UNAVAILABLE,
            )
        except BackendError:
            return self._failed(
                action,
                action_id=action_id,
                now=now,
                category=ErrorCategory.BACKEND_ERROR,
            )
        return ComputerActionResult(
            action_id=action_id,
            capability=action.capability,
            principal=action.principal,
            outcome=ExecutionOutcome.SUCCESS,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
            created_at=now,
            screen_size=payload.screen_size,
            active_window=payload.active_window,
            screenshot=payload.screenshot,
        )

    def _failed(
        self,
        action: ComputerAction,
        *,
        action_id: str,
        now: datetime,
        category: ErrorCategory,
    ) -> ComputerActionResult:
        return ComputerActionResult(
            action_id=action_id,
            capability=action.capability,
            principal=action.principal,
            outcome=ExecutionOutcome.FAILED,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
            created_at=now,
            error_category=category,
        )

    def _call_backend(self, action: ComputerAction) -> _BackendPayload:
        if isinstance(action, ScreenshotAction):
            return _BackendPayload(screenshot=self._backend.screenshot())
        if isinstance(action, GetActiveWindowAction):
            return _BackendPayload(active_window=self._backend.get_active_window())
        if isinstance(action, GetScreenSizeAction):
            return _BackendPayload(screen_size=self._backend.get_screen_size())
        if isinstance(action, MouseMoveAction):
            self._check_bounds(action.x, action.y)
            self._backend.move_mouse(x=action.x, y=action.y)
            return _BackendPayload()
        if isinstance(action, MouseClickAction):
            self._check_bounds(action.x, action.y)
            self._backend.click(x=action.x, y=action.y, button=action.button)
            return _BackendPayload()
        if isinstance(action, MouseDoubleClickAction):
            self._check_bounds(action.x, action.y)
            self._backend.double_click(x=action.x, y=action.y, button=action.button)
            return _BackendPayload()
        if isinstance(action, MouseScrollAction):
            self._check_bounds(action.x, action.y)
            self._backend.scroll(
                x=action.x, y=action.y, direction=action.direction, amount=action.amount
            )
            return _BackendPayload()
        if isinstance(action, KeyPressAction):
            self._backend.press_key(key=action.key)
            return _BackendPayload()
        if isinstance(action, KeyCombinationAction):
            self._backend.hotkey(modifiers=action.modifiers, key=action.key)
            return _BackendPayload()
        if isinstance(action, TypeTextAction):
            self._backend.type_text(text=action.text)
            return _BackendPayload()
        raise InvalidComputerActionError(f"unhandled action type: {type(action)!r}")

    def _check_bounds(self, x: int, y: int) -> None:
        """Validate against the *real* screen size — the second, precise
        layer of coordinate defense on top of each action model's static
        ``Field`` bounds. Propagates whatever ``BackendError`` the size
        query itself raises; a size that cannot be determined means the
        coordinate cannot be safely validated, so the action fails
        closed rather than proceeding unchecked."""

        size = self._backend.get_screen_size()
        if not (0 <= x < size.width) or not (0 <= y < size.height):
            raise OutOfBoundsCoordinateError(
                f"({x}, {y}) is outside the {size.width}x{size.height} screen"
            )

    def execute_sequence(
        self,
        actions: Sequence[ComputerAction],
        *,
        confirmation_ids: Sequence[str | None] | None = None,
        reason: str | None = None,
    ) -> tuple[ComputerActionResult, ...]:
        """Execute a finite, bounded, pre-validated sequence.

        Rejected outright — before any action executes — if longer than
        ``max_sequence_length``. Stops at the first non-``SUCCESS`` result
        rather than continuing past a failure. This method only iterates
        the given, already-finite list exactly once: nothing here can
        generate, retry, or recurse into more actions.
        """

        if len(actions) > self._max_sequence_length:
            raise ActionSequenceTooLongError(
                f"sequence of {len(actions)} exceeds max_actions="
                f"{self._max_sequence_length}"
            )
        ids: Sequence[str | None] = (
            confirmation_ids if confirmation_ids is not None else [None] * len(actions)
        )
        if len(ids) != len(actions):
            raise InvalidComputerActionError(
                "confirmation_ids length must match actions length"
            )

        results: list[ComputerActionResult] = []
        for action, confirmation_id in zip(actions, ids, strict=True):
            result = self.execute(
                action, confirmation_id=confirmation_id, reason=reason
            )
            results.append(result)
            if result.outcome is not ExecutionOutcome.SUCCESS:
                break
        return tuple(results)

    def _record_audit(
        self, result: ComputerActionResult, *, risk: RiskLevel, now: datetime
    ) -> None:
        if self._audit is None:
            return
        confirmation_outcome = None
        if result.permission_outcome is PermissionOutcomeSummary.DENY:
            confirmation_outcome = (
                ComputerAuditOutcome.CONFIRM_REQUIRED
                if result.error_category is ErrorCategory.CONFIRMATION_INVALID
                else ComputerAuditOutcome.DENIED
            )
        elif result.permission_outcome is PermissionOutcomeSummary.CONFIRM_REQUIRED:
            confirmation_outcome = ComputerAuditOutcome.CONFIRM_REQUIRED
        event = ComputerAuditEvent(
            event_id=uuid4().hex,
            occurred_at=now,
            action_id=result.action_id,
            principal=result.principal,
            capability=result.capability,
            risk=risk,
            permission_outcome=result.permission_outcome,
            confirmation_outcome=confirmation_outcome,
            execution_outcome=result.outcome,
            error_category=result.error_category,
        )
        try:
            self._audit.record(event)
        except Exception:
            # Best-effort observability, not an authorization gate — the
            # same trade-off as sam.permissions.engine / sam.memory.engine.
            return
