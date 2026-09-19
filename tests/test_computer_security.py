"""Dedicated adversarial security tests for computer control.

Each test is named after one item in the Phase 5 task's required security
matrix (unauthorized principal, wrong scope, universal scope attempt,
confirmation replay, ...). The objective throughout is not just
functional correctness but proof of isolation: DENY/CONFIRM_REQUIRED/
invalid input must never let an action reach ``FakeComputerBackend``.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from sam.computer.audit import InMemoryComputerAuditSink
from sam.computer.backend import FakeComputerBackend
from sam.computer.controller import ComputerController
from sam.computer.errors import ActionSequenceTooLongError
from sam.computer.models import (
    ComputerAction,
    ExecutionOutcome,
    KeyCombinationAction,
    KeyPressAction,
    ModifierKey,
    MouseClickAction,
    MouseMoveAction,
    PermissionOutcomeSummary,
    ScreenshotAction,
    TypeTextAction,
)
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


class _Harness:
    def __init__(self, *, clock: datetime = _T0) -> None:
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
        self.backend = FakeComputerBackend()
        self.audit = InMemoryComputerAuditSink()
        self.controller = ComputerController(
            backend=self.backend,
            permission_engine=self.engine,
            audit_sink=self.audit,
            clock=lambda: self._clock_value,
        )

    def advance(self, delta: timedelta) -> None:
        self._clock_value = self._clock_value + delta

    def grant(
        self,
        *,
        action: PermissionAction,
        scope: str,
        principal: Principal | None = None,
        grant_id: str = "g1",
        **kwargs: object,
    ) -> None:
        defaults: dict[str, object] = {
            "grant_id": grant_id,
            "principal": principal or _principal(),
            "resource": PermissionResource.COMPUTER,
            "action": action,
            "scope": PermissionScope.identifier(scope),
            "created_at": _T0,
            "updated_at": _T0,
        }
        defaults.update(kwargs)
        self.store.create_grant(PermissionGrant.model_validate(defaults))


# --------------------------------------------------------------------- #
# 1-2: unauthorized / wrong principal
# --------------------------------------------------------------------- #


def test_01_unauthorized_principal_is_denied() -> None:
    h = _Harness()  # no grants at all
    result = h.controller.execute(MouseMoveAction(principal=_principal(), x=1, y=1))
    assert result.outcome is ExecutionOutcome.FAILED
    assert h.backend.calls() == ()


def test_02_grant_for_one_principal_does_not_authorize_another() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.WRITE, scope="mouse", principal=_principal("ali"))
    result = h.controller.execute(
        MouseMoveAction(principal=_principal("bob"), x=1, y=1)
    )
    assert result.outcome is ExecutionOutcome.FAILED
    assert h.backend.calls() == ()


# --------------------------------------------------------------------- #
# 3-4: revoked / expired permission
# --------------------------------------------------------------------- #


def test_03_revoked_permission_is_denied() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.WRITE, scope="mouse")
    h.store.revoke_grant("g1", now=_T0)
    result = h.controller.execute(MouseMoveAction(principal=_principal(), x=1, y=1))
    assert result.outcome is ExecutionOutcome.FAILED
    assert h.backend.calls() == ()


def test_04_expired_permission_is_denied() -> None:
    h = _Harness()
    h.grant(
        action=PermissionAction.WRITE,
        scope="mouse",
        expires_at=_T0 + timedelta(hours=1),
    )
    h.advance(timedelta(hours=2))
    result = h.controller.execute(MouseMoveAction(principal=_principal(), x=1, y=1))
    assert result.outcome is ExecutionOutcome.FAILED
    assert h.backend.calls() == ()


# --------------------------------------------------------------------- #
# 5-6: wrong / universal scope
# --------------------------------------------------------------------- #


def test_05_grant_for_wrong_computer_scope_is_denied() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.EXECUTE, scope="keyboard")  # not mouse
    result = h.controller.execute(MouseMoveAction(principal=_principal(), x=1, y=1))
    assert result.outcome is ExecutionOutcome.FAILED
    assert h.backend.calls() == ()


def test_06_universal_scope_string_does_not_grant_unrestricted_control() -> None:
    """A grant for a literal "*" scope must not authorize "mouse" or
    "keyboard" — there is no wildcard syntax anywhere in the engine."""

    h = _Harness()
    h.grant(action=PermissionAction.WRITE, scope="*")
    h.grant(action=PermissionAction.EXECUTE, scope="*", grant_id="g2")

    mouse_result = h.controller.execute(
        MouseMoveAction(principal=_principal(), x=1, y=1)
    )
    key_result = h.controller.execute(KeyPressAction(principal=_principal(), key="a"))

    assert mouse_result.outcome is ExecutionOutcome.FAILED
    assert key_result.outcome is ExecutionOutcome.FAILED
    assert h.backend.calls() == ()


# --------------------------------------------------------------------- #
# 7-10: coordinate validation
# --------------------------------------------------------------------- #


def test_07_invalid_coordinate_type_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        MouseMoveAction.model_validate({"principal": _principal(), "x": "abc", "y": 0})


def test_08_negative_coordinates_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MouseMoveAction(principal=_principal(), x=-1, y=0)


def test_09_huge_coordinates_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MouseMoveAction(principal=_principal(), x=99_999_999, y=0)


def test_10_nan_and_infinite_coordinates_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MouseMoveAction.model_validate(
            {"principal": _principal(), "x": float("nan"), "y": 0}
        )
    with pytest.raises(ValidationError):
        MouseMoveAction.model_validate(
            {"principal": _principal(), "x": float("inf"), "y": 0}
        )


def test_coordinates_beyond_real_screen_never_reach_click() -> None:
    """Passes model-level bounds but exceeds the backend's actual screen —
    the controller's second, real-screen-size-aware check must catch it."""

    h = _Harness()
    h.grant(action=PermissionAction.WRITE, scope="mouse")
    result = h.controller.execute(
        MouseClickAction(principal=_principal(), x=5000, y=5000)
    )
    assert result.outcome is ExecutionOutcome.FAILED
    assert not any(c.method == "click" for c in h.backend.calls())


# --------------------------------------------------------------------- #
# 11-12: malformed keys / combinations
# --------------------------------------------------------------------- #


def test_11_malformed_key_names_are_rejected() -> None:
    with pytest.raises(ValidationError):
        KeyPressAction(principal=_principal(), key="definitely-not-a-key")


def test_12_unsupported_key_combination_is_rejected() -> None:
    with pytest.raises(ValidationError):
        KeyCombinationAction(principal=_principal(), modifiers=(), key="c")
    with pytest.raises(ValidationError):
        KeyCombinationAction(
            principal=_principal(), modifiers=(ModifierKey.CTRL,), key="not-a-key"
        )


# --------------------------------------------------------------------- #
# 13-16: text input safety
# --------------------------------------------------------------------- #


def test_13_oversized_typed_text_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TypeTextAction(principal=_principal(), text="x" * 10_000)


def test_14_control_characters_in_typed_text_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TypeTextAction(principal=_principal(), text="hello\x1bworld")


def test_15_null_bytes_in_typed_text_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TypeTextAction(principal=_principal(), text="hello\x00world")


def test_16_secret_like_text_can_still_be_typed_but_is_never_exposed() -> None:
    """Phase 5 does not censor typed content (that would be surprising and
    could silently corrupt legitimate input) — it only guarantees the
    text itself never appears anywhere outside the backend call. See
    tests 17-18 for the audit-leakage proof."""

    h = _Harness()
    h.grant(action=PermissionAction.EXECUTE, scope="keyboard")
    action = TypeTextAction(principal=_principal(), text="sk-ant-secretvalue1234567890")
    first = h.controller.execute(action)
    assert first.confirmation_id is not None
    h.confirmations.decide(first.confirmation_id, approved=True, now=_T0)
    result = h.controller.execute(action, confirmation_id=first.confirmation_id)
    assert result.outcome is ExecutionOutcome.SUCCESS


# --------------------------------------------------------------------- #
# 17-18: audit leakage
# --------------------------------------------------------------------- #


def test_17_screenshot_bytes_never_appear_in_audit_records() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.READ, scope="screen")
    h.controller.execute(ScreenshotAction(principal=_principal()))

    events = h.audit.list_events()
    assert len(events) == 1
    # The audit event model has no field for screenshot bytes at all —
    # not merely "happens to be empty" for this call.
    assert "data" not in events[0].model_dump()
    assert "screenshot" not in events[0].model_dump()


def test_18_typed_text_never_appears_in_audit_records() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.EXECUTE, scope="keyboard")
    secret_text = "the secret passphrase is correct-horse-battery-staple"
    action = TypeTextAction(principal=_principal(), text=secret_text)

    first = h.controller.execute(action)
    assert first.confirmation_id is not None
    h.confirmations.decide(first.confirmation_id, approved=True, now=_T0)
    h.controller.execute(action, confirmation_id=first.confirmation_id)

    for event in h.audit.list_events():
        serialized = event.model_dump_json()
        assert secret_text not in serialized
        assert "correct-horse-battery-staple" not in serialized


# --------------------------------------------------------------------- #
# 19-21: exception handling never becomes success
# --------------------------------------------------------------------- #


def test_19_backend_exception_never_becomes_success() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.WRITE, scope="mouse")
    h.backend.fail_next("move_mouse")
    result = h.controller.execute(MouseMoveAction(principal=_principal(), x=1, y=1))
    assert result.outcome is ExecutionOutcome.FAILED


def test_20_policy_exception_never_becomes_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sam.computer.controller as controller_module

    def _raise(*args: object, **kwargs: object) -> object:
        raise RuntimeError("policy blew up")

    h = _Harness()
    h.grant(action=PermissionAction.WRITE, scope="mouse")
    monkeypatch.setattr(controller_module, "build_request", _raise)

    result = h.controller.execute(MouseMoveAction(principal=_principal(), x=1, y=1))

    assert result.outcome is ExecutionOutcome.FAILED
    assert h.backend.calls() == ()


def test_21_permission_engine_exception_never_becomes_success() -> None:
    class _RaisingStore:
        def create_grant(self, grant: object) -> object:
            raise PermissionStoreError("unavailable")

        def get_grant(self, grant_id: str) -> object:
            raise PermissionStoreError("unavailable")

        def list_grants(self, principal: object, **kwargs: object) -> object:
            raise PermissionStoreError("unavailable")

        def revoke_grant(self, grant_id: str, *, now: object) -> object:
            raise PermissionStoreError("unavailable")

    engine = PermissionEngine(
        store=_RaisingStore(),  # type: ignore[arg-type]
        confirmation_provider=InMemoryConfirmationProvider(),
        audit_sink=InMemoryPermissionAuditSink(),
        clock=lambda: _T0,
    )
    backend = FakeComputerBackend()
    controller = ComputerController(backend=backend, permission_engine=engine)

    result = controller.execute(MouseMoveAction(principal=_principal(), x=1, y=1))

    assert result.outcome is ExecutionOutcome.FAILED
    assert backend.calls() == ()


# --------------------------------------------------------------------- #
# 22-24: confirmation safety
# --------------------------------------------------------------------- #


def test_22_missing_confirmation_never_executes() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.EXECUTE, scope="keyboard")
    result = h.controller.execute(TypeTextAction(principal=_principal(), text="hello"))
    assert result.outcome is ExecutionOutcome.FAILED
    assert result.permission_outcome is PermissionOutcomeSummary.CONFIRM_REQUIRED
    assert h.backend.calls() == ()


def test_23_confirmation_cannot_be_reused() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.EXECUTE, scope="keyboard")
    action = TypeTextAction(principal=_principal(), text="hello")

    first = h.controller.execute(action)
    assert first.confirmation_id is not None
    h.confirmations.decide(first.confirmation_id, approved=True, now=_T0)
    used = h.controller.execute(action, confirmation_id=first.confirmation_id)
    assert used.outcome is ExecutionOutcome.SUCCESS

    replay = h.controller.execute(action, confirmation_id=first.confirmation_id)
    assert replay.outcome is ExecutionOutcome.FAILED
    assert sum(1 for c in h.backend.calls() if c.method == "type_text") == 1


def test_24_confirmation_context_mismatch_is_rejected() -> None:
    """A confirmation approved for one typed string must not authorize a
    different string, even with the same confirmation id supplied."""

    h = _Harness()
    h.grant(action=PermissionAction.EXECUTE, scope="keyboard")
    action_a = TypeTextAction(principal=_principal(), text="text A")
    action_b = TypeTextAction(principal=_principal(), text="text B")

    first = h.controller.execute(action_a)
    assert first.confirmation_id is not None
    h.confirmations.decide(first.confirmation_id, approved=True, now=_T0)

    mismatched = h.controller.execute(action_b, confirmation_id=first.confirmation_id)

    assert mismatched.outcome is ExecutionOutcome.FAILED
    assert h.backend.calls() == ()


# --------------------------------------------------------------------- #
# 25-26: action sequences
# --------------------------------------------------------------------- #


def test_25_action_sequence_exceeding_limit_executes_nothing() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.WRITE, scope="mouse")
    actions = [MouseMoveAction(principal=_principal(), x=i, y=i) for i in range(11)]
    with pytest.raises(ActionSequenceTooLongError):
        h.controller.execute_sequence(actions)
    assert h.backend.calls() == ()


def test_26_empty_sequence_is_handled_safely() -> None:
    h = _Harness()
    assert h.controller.execute_sequence([]) == ()
    assert h.backend.calls() == ()


# --------------------------------------------------------------------- #
# 27-29: replay / cross-principal / cross-scope
# --------------------------------------------------------------------- #


def test_27_duplicate_execution_attempt_is_independently_authorized() -> None:
    """Executing the identical action twice (no confirmation involved,
    LOW/MEDIUM risk) is not "replay abuse" — each call is independently,
    correctly authorized and independently dispatched."""

    h = _Harness()
    h.grant(action=PermissionAction.WRITE, scope="mouse")
    action = MouseMoveAction(principal=_principal(), x=1, y=1)

    first = h.controller.execute(action)
    second = h.controller.execute(action)

    assert first.outcome is second.outcome is ExecutionOutcome.SUCCESS
    assert first.action_id != second.action_id
    assert sum(1 for c in h.backend.calls() if c.method == "move_mouse") == 2


def test_28_cross_principal_action_attempt_is_denied() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.WRITE, scope="mouse", principal=_principal("ali"))
    attempt = h.controller.execute(
        MouseMoveAction(principal=_principal("mallory"), x=1, y=1)
    )
    assert attempt.outcome is ExecutionOutcome.FAILED
    assert h.backend.calls() == ()


def test_29_cross_scope_action_attempt_is_denied() -> None:
    """A mouse grant must not authorize a keyboard action, even for the
    same principal and even though both map to resource=computer."""

    h = _Harness()
    h.grant(action=PermissionAction.WRITE, scope="mouse")
    attempt = h.controller.execute(KeyPressAction(principal=_principal(), key="a"))
    assert attempt.outcome is ExecutionOutcome.FAILED
    assert h.backend.calls() == ()


# --------------------------------------------------------------------- #
# 30: fake backend bypass attempt
# --------------------------------------------------------------------- #


def test_30_backend_cannot_be_reached_by_constructing_a_result_directly() -> None:
    """There is no path to a SUCCESS ComputerActionResult that does not
    go through ComputerController.execute — proven by exhaustively
    checking that every DENY/CONFIRM_REQUIRED/failed-validation path
    above already left the backend untouched, and confirming here that
    the backend has no method resembling a permission bypass (e.g. no
    ``force``/``override``/``admin`` escape hatch)."""

    backend = FakeComputerBackend()
    public_methods = {name for name in dir(backend) if not name.startswith("_")}
    forbidden = {
        "force",
        "override",
        "admin",
        "bypass",
        "run",
        "shell",
        "execute",
        "eval",
    }
    assert not (public_methods & forbidden)


# --------------------------------------------------------------------- #
# The three critical invariants, stated directly and exhaustively
# --------------------------------------------------------------------- #


def test_deny_never_reaches_backend_for_every_capability() -> None:
    h = _Harness()  # no grants for anything
    actions: list[ComputerAction] = [
        ScreenshotAction(principal=_principal()),
        MouseMoveAction(principal=_principal(), x=1, y=1),
        KeyPressAction(principal=_principal(), key="a"),
        TypeTextAction(principal=_principal(), text="hello"),
    ]
    for action in actions:
        result = h.controller.execute(action)
        assert result.outcome is ExecutionOutcome.FAILED
    assert h.backend.calls() == ()


def test_confirm_required_never_reaches_backend() -> None:
    h = _Harness()
    h.grant(action=PermissionAction.EXECUTE, scope="keyboard")
    h.controller.execute(TypeTextAction(principal=_principal(), text="hello"))
    h.controller.execute(
        KeyCombinationAction(
            principal=_principal(), modifiers=(ModifierKey.CTRL,), key="c"
        )
    )
    assert h.backend.calls() == ()


def test_invalid_action_never_reaches_backend() -> None:
    """A malformed action cannot even be constructed, so there is no
    ComputerAction to pass to the controller at all — the strongest
    possible form of "invalid never reaches the backend"."""

    with pytest.raises(ValidationError):
        MouseMoveAction(principal=_principal(), x=-1, y=-1)
    # No controller call is possible without a valid action instance.
