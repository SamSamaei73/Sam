"""Tests for ComputerController: happy paths, sequences, audit, results."""

from datetime import UTC, datetime, timedelta

import pytest

from sam.computer.audit import InMemoryComputerAuditSink
from sam.computer.backend import FakeComputerBackend
from sam.computer.controller import ComputerController
from sam.computer.errors import ActionSequenceTooLongError, InvalidComputerActionError
from sam.computer.models import (
    ActiveWindow,
    ComputerAction,
    ComputerAuditOutcome,
    ExecutionOutcome,
    GetActiveWindowAction,
    GetScreenSizeAction,
    KeyCombinationAction,
    KeyPressAction,
    ModifierKey,
    MouseMoveAction,
    PermissionOutcomeSummary,
    ScreenshotAction,
    ScreenSize,
    TypeTextAction,
    WindowBounds,
)
from sam.permissions.audit import InMemoryAuditSink as InMemoryPermissionAuditSink
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

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _principal(value: str = "ali") -> Principal:
    return Principal(kind=PrincipalKind.USER, id=value)


class _Harness:
    """A fully wired, isolated controller plus its collaborators."""

    def __init__(
        self,
        *,
        clock: datetime = _T0,
        screen_size: ScreenSize | None = None,
        active_window: ActiveWindow | None = None,
        max_sequence_length: int = 10,
    ) -> None:
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
        self.backend = FakeComputerBackend(
            screen_size=screen_size, active_window=active_window
        )
        self.audit = InMemoryComputerAuditSink()
        self.controller = ComputerController(
            backend=self.backend,
            permission_engine=self.engine,
            audit_sink=self.audit,
            clock=lambda: self._clock_value,
            max_sequence_length=max_sequence_length,
        )

    def advance(self, delta: timedelta) -> None:
        self._clock_value = self._clock_value + delta

    def grant(
        self,
        *,
        action: PermissionAction,
        scope: str,
        principal: Principal | None = None,
        grant_id: str | None = None,
    ) -> None:
        self.store.create_grant(
            PermissionGrant(
                grant_id=grant_id or f"g-{scope}-{action.value}",
                principal=principal or _principal(),
                resource=PermissionResource.COMPUTER,
                action=action,
                scope=PermissionScope.identifier(scope),
                created_at=_T0,
                updated_at=_T0,
            )
        )


# --------------------------------------------------------------------- #
# Happy paths per capability
# --------------------------------------------------------------------- #


def test_screenshot_success_returns_result_payload() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.READ, scope="screen")

    result = h.controller.execute(ScreenshotAction(principal=_principal()))

    assert result.outcome is ExecutionOutcome.SUCCESS
    assert result.screenshot is not None
    assert result.screenshot.width == 1920


def test_get_screen_size_success() -> None:
    h = _Harness(screen_size=ScreenSize(width=2560, height=1440))
    h.grant(action=PermissionAction.READ, scope="screen")

    result = h.controller.execute(GetScreenSizeAction(principal=_principal()))

    assert result.outcome is ExecutionOutcome.SUCCESS
    assert result.screen_size == ScreenSize(width=2560, height=1440)


def test_get_active_window_success() -> None:
    window = ActiveWindow(
        application="Terminal",
        title="zsh",
        bounds=WindowBounds(x=0, y=0, width=800, height=600),
    )
    h = _Harness(active_window=window)
    h.grant(action=PermissionAction.READ, scope="window")

    result = h.controller.execute(GetActiveWindowAction(principal=_principal()))

    assert result.outcome is ExecutionOutcome.SUCCESS
    assert result.active_window == window


def test_mouse_move_success() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.WRITE, scope="mouse")

    result = h.controller.execute(MouseMoveAction(principal=_principal(), x=100, y=100))

    assert result.outcome is ExecutionOutcome.SUCCESS
    move_calls = [c for c in h.backend.calls() if c.method == "move_mouse"]
    assert len(move_calls) == 1
    assert move_calls[0].x == 100


def test_key_press_after_confirmation_succeeds() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.EXECUTE, scope="keyboard")
    action = KeyPressAction(principal=_principal(), key="enter")

    first = h.controller.execute(action)
    assert first.permission_outcome is PermissionOutcomeSummary.CONFIRM_REQUIRED
    assert first.confirmation_id is not None
    h.confirmations.decide(first.confirmation_id, approved=True, now=_T0)

    second = h.controller.execute(action, confirmation_id=first.confirmation_id)
    assert second.outcome is ExecutionOutcome.SUCCESS
    assert any(c.method == "press_key" for c in h.backend.calls())


def test_key_combination_after_confirmation_succeeds() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.EXECUTE, scope="keyboard")
    action = KeyCombinationAction(
        principal=_principal(), modifiers=(ModifierKey.CTRL,), key="c"
    )

    first = h.controller.execute(action)
    assert first.confirmation_id is not None
    h.confirmations.decide(first.confirmation_id, approved=True, now=_T0)
    second = h.controller.execute(action, confirmation_id=first.confirmation_id)

    assert second.outcome is ExecutionOutcome.SUCCESS
    hotkey_calls = [c for c in h.backend.calls() if c.method == "hotkey"]
    assert hotkey_calls[0].modifiers == (ModifierKey.CTRL,)


# --------------------------------------------------------------------- #
# Confirmation flow specifics
# --------------------------------------------------------------------- #


def test_confirm_required_never_touches_backend() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.EXECUTE, scope="keyboard")

    result = h.controller.execute(TypeTextAction(principal=_principal(), text="hello"))

    assert result.permission_outcome is PermissionOutcomeSummary.CONFIRM_REQUIRED
    assert h.backend.calls() == ()


def test_denied_confirmation_prevents_execution() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.EXECUTE, scope="keyboard")
    action = TypeTextAction(principal=_principal(), text="hello")

    first = h.controller.execute(action)
    assert first.confirmation_id is not None
    h.confirmations.decide(first.confirmation_id, approved=False, now=_T0)
    second = h.controller.execute(action, confirmation_id=first.confirmation_id)

    assert second.outcome is ExecutionOutcome.FAILED
    assert h.backend.calls() == ()


# --------------------------------------------------------------------- #
# Sequences
# --------------------------------------------------------------------- #


def test_sequence_executes_all_actions_when_authorized() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.WRITE, scope="mouse")
    actions = [
        MouseMoveAction(principal=_principal(), x=1, y=1),
        MouseMoveAction(principal=_principal(), x=2, y=2),
        MouseMoveAction(principal=_principal(), x=3, y=3),
    ]

    results = h.controller.execute_sequence(actions)

    assert len(results) == 3
    assert all(r.outcome is ExecutionOutcome.SUCCESS for r in results)


def test_sequence_stops_at_first_failure() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.WRITE, scope="mouse")
    # No keyboard grant exists, so the middle action fails.
    actions: list[ComputerAction] = [
        MouseMoveAction(principal=_principal(), x=1, y=1),
        KeyPressAction(principal=_principal(), key="enter"),
        MouseMoveAction(principal=_principal(), x=3, y=3),
    ]

    results = h.controller.execute_sequence(actions)

    assert len(results) == 2
    assert results[0].outcome is ExecutionOutcome.SUCCESS
    assert results[1].outcome is ExecutionOutcome.FAILED


def test_empty_sequence_returns_empty_results() -> None:
    h = _Harness()
    assert h.controller.execute_sequence([]) == ()


def test_sequence_exceeding_limit_is_rejected_before_any_execution() -> None:
    h = _Harness(max_sequence_length=3)
    h.grant(action=PermissionAction.WRITE, scope="mouse")
    actions = [MouseMoveAction(principal=_principal(), x=i, y=i) for i in range(4)]

    with pytest.raises(ActionSequenceTooLongError):
        h.controller.execute_sequence(actions)

    assert h.backend.calls() == ()


def test_sequence_confirmation_ids_length_mismatch_is_rejected() -> None:
    h = _Harness()
    actions = [MouseMoveAction(principal=_principal(), x=1, y=1)]
    with pytest.raises(InvalidComputerActionError):
        h.controller.execute_sequence(actions, confirmation_ids=["a", "b"])


def test_controller_rejects_non_positive_max_sequence_length() -> None:
    h = _Harness()
    with pytest.raises(ValueError, match="positive"):
        ComputerController(
            backend=h.backend, permission_engine=h.engine, max_sequence_length=0
        )


# --------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------- #


def test_every_execution_generates_exactly_one_audit_event() -> None:
    h = _Harness()
    h.controller.execute(MouseMoveAction(principal=_principal(), x=1, y=1))
    assert len(h.audit.list_events()) == 1


def test_audit_event_records_denial() -> None:
    h = _Harness()
    h.controller.execute(MouseMoveAction(principal=_principal(), x=1, y=1))
    event = h.audit.list_events()[0]
    assert event.confirmation_outcome is ComputerAuditOutcome.DENIED
    assert event.execution_outcome is ExecutionOutcome.FAILED


def test_engine_works_without_an_audit_sink_configured() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.WRITE, scope="mouse")
    controller = ComputerController(backend=h.backend, permission_engine=h.engine)
    result = controller.execute(MouseMoveAction(principal=_principal(), x=1, y=1))
    assert result.outcome is ExecutionOutcome.SUCCESS
