"""Tests for computer-control domain models."""

import math
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sam.computer.models import (
    ActiveWindow,
    ComputerActionResult,
    ComputerAuditEvent,
    ComputerCapability,
    ErrorCategory,
    ExecutionOutcome,
    KeyCombinationAction,
    KeyPressAction,
    ModifierKey,
    MouseMoveAction,
    MouseScrollAction,
    PermissionOutcomeSummary,
    RiskLevel,
    ScreenshotResult,
    ScreenSize,
    ScrollDirection,
    TypeTextAction,
    WindowBounds,
)
from sam.permissions.models import Principal, PrincipalKind

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _principal() -> Principal:
    return Principal(kind=PrincipalKind.USER, id="ali")


# --------------------------------------------------------------------- #
# Coordinates
# --------------------------------------------------------------------- #


def test_valid_coordinates_construct() -> None:
    action = MouseMoveAction(principal=_principal(), x=100, y=200)
    assert action.x == 100
    assert action.y == 200


@pytest.mark.parametrize("x", [-1, -100])
def test_negative_coordinates_are_rejected(x: int) -> None:
    with pytest.raises(ValidationError):
        MouseMoveAction(principal=_principal(), x=x, y=0)


def test_huge_coordinates_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MouseMoveAction(principal=_principal(), x=10_000_000, y=0)


def test_nan_coordinate_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MouseMoveAction.model_validate(
            {"principal": _principal(), "x": math.nan, "y": 0}
        )


def test_infinite_coordinate_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MouseMoveAction.model_validate(
            {"principal": _principal(), "x": math.inf, "y": 0}
        )


def test_float_coordinate_is_rejected_not_silently_truncated() -> None:
    with pytest.raises(ValidationError):
        MouseMoveAction.model_validate(
            {"principal": _principal(), "x": 10.5, "y": 0}
        )


def test_scroll_amount_is_bounded() -> None:
    with pytest.raises(ValidationError):
        MouseScrollAction(
            principal=_principal(),
            x=0,
            y=0,
            direction=ScrollDirection.DOWN,
            amount=1000,
        )


def test_scroll_amount_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        MouseScrollAction(
            principal=_principal(), x=0, y=0, direction=ScrollDirection.DOWN, amount=0
        )


# --------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------- #


def test_named_key_is_accepted() -> None:
    action = KeyPressAction(principal=_principal(), key="enter")
    assert action.key == "enter"


def test_single_character_key_is_accepted() -> None:
    action = KeyPressAction(principal=_principal(), key="a")
    assert action.key == "a"


@pytest.mark.parametrize("key", ["notarealkey", "", "ab", "ctrl+c", "\x01"])
def test_malformed_key_names_are_rejected(key: str) -> None:
    with pytest.raises(ValidationError):
        KeyPressAction(principal=_principal(), key=key)


def test_key_combination_requires_at_least_one_modifier() -> None:
    with pytest.raises(ValidationError):
        KeyCombinationAction(principal=_principal(), modifiers=(), key="c")


def test_key_combination_rejects_duplicate_modifiers() -> None:
    with pytest.raises(ValidationError):
        KeyCombinationAction(
            principal=_principal(),
            modifiers=(ModifierKey.CTRL, ModifierKey.CTRL),
            key="c",
        )


def test_key_combination_rejects_more_modifiers_than_exist() -> None:
    with pytest.raises(ValidationError):
        KeyCombinationAction.model_validate(
            {
                "principal": _principal(),
                "modifiers": ["ctrl", "alt", "shift", "cmd", "ctrl"],
                "key": "c",
            }
        )


def test_key_combination_rejects_malformed_key() -> None:
    with pytest.raises(ValidationError):
        KeyCombinationAction(
            principal=_principal(), modifiers=(ModifierKey.CTRL,), key="notakey"
        )


# --------------------------------------------------------------------- #
# Typed text
# --------------------------------------------------------------------- #


def test_typed_text_within_bounds_is_accepted() -> None:
    action = TypeTextAction(principal=_principal(), text="hello world")
    assert action.text == "hello world"


def test_typed_text_allows_tab_and_newline() -> None:
    action = TypeTextAction(principal=_principal(), text="line one\tindented\nline two")
    assert "\t" in action.text
    assert "\n" in action.text


def test_typed_text_rejects_null_bytes() -> None:
    with pytest.raises(ValidationError):
        TypeTextAction(principal=_principal(), text="hello\x00world")


@pytest.mark.parametrize("char", ["\x01", "\x07", "\x1b", "\x7f"])
def test_typed_text_rejects_other_control_characters(char: str) -> None:
    with pytest.raises(ValidationError):
        TypeTextAction(principal=_principal(), text=f"hello{char}world")


def test_typed_text_rejects_empty_string() -> None:
    with pytest.raises(ValidationError):
        TypeTextAction(principal=_principal(), text="")


def test_typed_text_rejects_oversized_input() -> None:
    with pytest.raises(ValidationError):
        TypeTextAction(principal=_principal(), text="x" * 501)


# --------------------------------------------------------------------- #
# Action immutability and unknown fields
# --------------------------------------------------------------------- #


def test_action_is_frozen() -> None:
    action = MouseMoveAction(principal=_principal(), x=1, y=1)
    with pytest.raises(ValidationError):
        action.x = 999


def test_action_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        MouseMoveAction.model_validate(
            {"principal": _principal(), "x": 1, "y": 1, "unexpected": "field"}
        )


def test_action_capability_cannot_be_overridden() -> None:
    """The discriminator is fixed per action type, not caller-selectable
    in a way that could mismatch its own class."""

    with pytest.raises(ValidationError):
        MouseMoveAction.model_validate(
            {
                "principal": _principal(),
                "x": 1,
                "y": 1,
                "capability": ComputerCapability.TYPE_TEXT,
            }
        )


# --------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------- #


def _result(**overrides: object) -> ComputerActionResult:
    defaults: dict[str, object] = {
        "action_id": "a1",
        "capability": ComputerCapability.MOUSE_MOVE,
        "principal": _principal(),
        "outcome": ExecutionOutcome.SUCCESS,
        "permission_outcome": PermissionOutcomeSummary.ALLOW,
        "created_at": _NOW,
    }
    defaults.update(overrides)
    return ComputerActionResult.model_validate(defaults)


def test_success_result_must_not_carry_error_category() -> None:
    with pytest.raises(ValidationError):
        _result(error_category=ErrorCategory.BACKEND_ERROR)


def test_failed_result_requires_error_category() -> None:
    with pytest.raises(ValidationError):
        _result(outcome=ExecutionOutcome.FAILED)


def test_failed_result_with_category_is_valid() -> None:
    result = _result(
        outcome=ExecutionOutcome.FAILED, error_category=ErrorCategory.PERMISSION_DENIED
    )
    assert result.outcome is ExecutionOutcome.FAILED


def test_result_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        _result(created_at=datetime(2026, 1, 1))


def test_result_is_frozen() -> None:
    result = _result()
    with pytest.raises(ValidationError):
        result.outcome = ExecutionOutcome.FAILED


# --------------------------------------------------------------------- #
# Screenshot / window / screen-size result shapes
# --------------------------------------------------------------------- #


def test_screenshot_result_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValidationError):
        ScreenshotResult(
            width=100, height=100, data=b"", captured_at=datetime(2026, 1, 1)
        )


def test_screen_size_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        ScreenSize(width=0, height=1080)


def test_active_window_bounds_are_typed() -> None:
    window = ActiveWindow(
        application="Terminal",
        title="zsh",
        bounds=WindowBounds(x=0, y=0, width=800, height=600),
    )
    assert window.bounds.width == 800


def test_active_window_title_is_bounded() -> None:
    with pytest.raises(ValidationError):
        ActiveWindow(
            application="App",
            title="x" * 301,
            bounds=WindowBounds(x=0, y=0, width=1, height=1),
        )


# --------------------------------------------------------------------- #
# Audit event
# --------------------------------------------------------------------- #


def test_audit_event_has_no_field_for_typed_text_or_screenshot_bytes() -> None:
    field_names = set(ComputerAuditEvent.model_fields)
    assert "text" not in field_names
    assert "data" not in field_names
    assert "screenshot" not in field_names
    assert "coordinates" not in field_names


def test_audit_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        ComputerAuditEvent(
            event_id="e1",
            occurred_at=datetime(2026, 1, 1),
            action_id="a1",
            principal=_principal(),
            capability=ComputerCapability.MOUSE_MOVE,
            risk=RiskLevel.MEDIUM,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
        )
