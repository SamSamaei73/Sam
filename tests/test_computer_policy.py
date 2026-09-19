"""Tests for the ComputerAction → PermissionRequest mapping."""

from sam.computer.models import (
    ComputerCapability,
    GetActiveWindowAction,
    KeyCombinationAction,
    KeyPressAction,
    ModifierKey,
    MouseButton,
    MouseClickAction,
    MouseMoveAction,
    MouseScrollAction,
    ScreenshotAction,
    ScrollDirection,
    TypeTextAction,
)
from sam.computer.policy import build_request, risk_for
from sam.permissions.models import (
    PermissionAction,
    PermissionResource,
    Principal,
    PrincipalKind,
    RiskLevel,
)


def _principal() -> Principal:
    return Principal(kind=PrincipalKind.USER, id="ali")


def test_request_resource_is_always_computer() -> None:
    request = build_request(MouseMoveAction(principal=_principal(), x=1, y=1))
    assert request.resource is PermissionResource.COMPUTER


def test_request_principal_matches_the_action() -> None:
    principal = _principal()
    request = build_request(ScreenshotAction(principal=principal))
    assert request.principal == principal


def test_screenshot_maps_to_read_screen_scope() -> None:
    request = build_request(ScreenshotAction(principal=_principal()))
    assert request.action is PermissionAction.READ
    assert request.scope.as_text() == "screen"


def test_get_active_window_maps_to_read_window_scope() -> None:
    request = build_request(GetActiveWindowAction(principal=_principal()))
    assert request.action is PermissionAction.READ
    assert request.scope.as_text() == "window"


def test_mouse_click_target_encodes_coordinates_safely() -> None:
    request = build_request(
        MouseClickAction(principal=_principal(), x=10, y=20, button=MouseButton.LEFT)
    )
    assert request.target == "10,20"


def test_scroll_target_encodes_direction_and_amount() -> None:
    request = build_request(
        MouseScrollAction(
            principal=_principal(),
            x=1,
            y=2,
            direction=ScrollDirection.DOWN,
            amount=3,
        )
    )
    assert request.target == "1,2,down,3"


def test_key_press_target_is_the_key_name_only() -> None:
    request = build_request(KeyPressAction(principal=_principal(), key="enter"))
    assert request.target == "key:enter"


def test_key_combination_target_encodes_modifiers_and_key() -> None:
    request = build_request(
        KeyCombinationAction(
            principal=_principal(), modifiers=(ModifierKey.CTRL,), key="c"
        )
    )
    assert request.target == "combo:ctrl+c"


def test_type_text_target_never_contains_the_raw_text() -> None:
    secret_text = "my password is hunter2plus"
    request = build_request(TypeTextAction(principal=_principal(), text=secret_text))
    assert request.target is not None
    assert secret_text not in request.target
    assert "password" not in request.target
    assert request.target.startswith("sha256:")


def test_type_text_target_is_deterministic_for_the_same_text() -> None:
    text = "identical text content"
    first = build_request(TypeTextAction(principal=_principal(), text=text))
    second = build_request(TypeTextAction(principal=_principal(), text=text))
    assert first.target == second.target


def test_type_text_target_differs_for_different_text() -> None:
    first = build_request(TypeTextAction(principal=_principal(), text="text one"))
    second = build_request(TypeTextAction(principal=_principal(), text="text two"))
    assert first.target != second.target


def test_reason_is_passed_through() -> None:
    request = build_request(
        ScreenshotAction(principal=_principal()), reason="checking window state"
    )
    assert request.reason == "checking window state"


def test_risk_for_read_capabilities_is_low() -> None:
    assert risk_for(ComputerCapability.SCREENSHOT) is RiskLevel.LOW
    assert risk_for(ComputerCapability.GET_SCREEN_SIZE) is RiskLevel.LOW
    assert risk_for(ComputerCapability.GET_ACTIVE_WINDOW) is RiskLevel.LOW


def test_risk_for_mouse_capabilities_is_medium() -> None:
    assert risk_for(ComputerCapability.MOUSE_MOVE) is RiskLevel.MEDIUM
    assert risk_for(ComputerCapability.MOUSE_CLICK) is RiskLevel.MEDIUM
    assert risk_for(ComputerCapability.MOUSE_DOUBLE_CLICK) is RiskLevel.MEDIUM
    assert risk_for(ComputerCapability.MOUSE_SCROLL) is RiskLevel.MEDIUM


def test_risk_for_keyboard_capabilities_is_high() -> None:
    assert risk_for(ComputerCapability.KEY_PRESS) is RiskLevel.HIGH
    assert risk_for(ComputerCapability.KEY_COMBINATION) is RiskLevel.HIGH
    assert risk_for(ComputerCapability.TYPE_TEXT) is RiskLevel.HIGH


def test_no_phase5_capability_is_critical() -> None:
    for capability in ComputerCapability:
        assert risk_for(capability) is not RiskLevel.CRITICAL
