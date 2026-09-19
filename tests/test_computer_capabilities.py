"""Tests for the computer-capability registry."""

import pytest

from sam.computer.capabilities import mapping_for
from sam.computer.models import ComputerCapability
from sam.permissions.models import PermissionAction


@pytest.mark.parametrize("capability", list(ComputerCapability))
def test_every_capability_has_a_mapping(capability: ComputerCapability) -> None:
    mapping = mapping_for(capability)
    assert mapping.permission_action in PermissionAction
    assert mapping.scope_segment


@pytest.mark.parametrize(
    "capability",
    [
        ComputerCapability.SCREENSHOT,
        ComputerCapability.GET_SCREEN_SIZE,
        ComputerCapability.GET_ACTIVE_WINDOW,
    ],
)
def test_read_capabilities_map_to_read_action(capability: ComputerCapability) -> None:
    assert mapping_for(capability).permission_action is PermissionAction.READ


@pytest.mark.parametrize(
    "capability",
    [
        ComputerCapability.MOUSE_MOVE,
        ComputerCapability.MOUSE_CLICK,
        ComputerCapability.MOUSE_DOUBLE_CLICK,
        ComputerCapability.MOUSE_SCROLL,
    ],
)
def test_mouse_capabilities_map_to_write_action(capability: ComputerCapability) -> None:
    assert mapping_for(capability).permission_action is PermissionAction.WRITE


@pytest.mark.parametrize(
    "capability",
    [
        ComputerCapability.KEY_PRESS,
        ComputerCapability.KEY_COMBINATION,
        ComputerCapability.TYPE_TEXT,
    ],
)
def test_keyboard_capabilities_map_to_execute_action(
    capability: ComputerCapability,
) -> None:
    assert mapping_for(capability).permission_action is PermissionAction.EXECUTE


def test_mouse_and_keyboard_use_different_scopes() -> None:
    mouse_scope = mapping_for(ComputerCapability.MOUSE_MOVE).scope_segment
    keyboard_scope = mapping_for(ComputerCapability.KEY_PRESS).scope_segment
    assert mouse_scope != keyboard_scope


def test_no_capability_is_named_after_excluded_functionality() -> None:
    excluded = {
        "execute_command",
        "shell",
        "terminal",
        "delete_file",
        "upload_file",
        "download_file",
        "install_application",
    }
    capability_values = {c.value for c in ComputerCapability}
    assert not (capability_values & excluded)
