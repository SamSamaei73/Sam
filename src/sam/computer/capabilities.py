"""The declarative computer-capability registry.

This is *data*, not decision logic: for each ``ComputerCapability`` it
records which ``PermissionAction`` it maps to and which computer scope
segment (``screen`` / ``window`` / ``mouse`` / ``keyboard``) it belongs
to. ``sam.computer.policy`` is the module that turns this data plus a
concrete action into an actual ``PermissionRequest`` — this module never
talks to the Permission Engine itself.

No new ``PermissionAction`` was added for Phase 5: every capability maps
onto an existing action (``READ``, ``WRITE``, or ``EXECUTE``), and the
resulting risk comes entirely from ``sam.permissions.policy`` — see the
``computer`` section added there. This registry cannot itself authorize
anything; it only says "this capability's request looks like this."
"""

from __future__ import annotations

from dataclasses import dataclass

from sam.computer.models import ComputerCapability
from sam.permissions.models import PermissionAction

# Explicit and reviewable, the same discipline as
# sam.permissions.policy._POLICY: every row is a considered mapping, not
# inferred. Extending Phase 5 to a new capability means adding a row here
# *and* ensuring sam.permissions.policy classifies the resulting
# (COMPUTER, action) pair — both are deliberate edits, never automatic.


@dataclass(frozen=True)
class CapabilityMapping:
    """Where one capability's ``PermissionRequest`` comes from."""

    permission_action: PermissionAction
    scope_segment: str


_CAPABILITY_MAPPING: dict[ComputerCapability, CapabilityMapping] = {
    # --- read-only queries: LOW risk, resource=computer, action=READ ---
    ComputerCapability.SCREENSHOT: CapabilityMapping(PermissionAction.READ, "screen"),
    ComputerCapability.GET_SCREEN_SIZE: CapabilityMapping(
        PermissionAction.READ, "screen"
    ),
    ComputerCapability.GET_ACTIVE_WINDOW: CapabilityMapping(
        PermissionAction.READ, "window"
    ),
    # --- mouse: MEDIUM risk (state-changing, reversible), action=WRITE --
    ComputerCapability.MOUSE_MOVE: CapabilityMapping(PermissionAction.WRITE, "mouse"),
    ComputerCapability.MOUSE_CLICK: CapabilityMapping(PermissionAction.WRITE, "mouse"),
    ComputerCapability.MOUSE_DOUBLE_CLICK: CapabilityMapping(
        PermissionAction.WRITE, "mouse"
    ),
    ComputerCapability.MOUSE_SCROLL: CapabilityMapping(PermissionAction.WRITE, "mouse"),
    # --- keyboard: HIGH risk (can trigger arbitrary app behavior) -------
    ComputerCapability.KEY_PRESS: CapabilityMapping(
        PermissionAction.EXECUTE, "keyboard"
    ),
    ComputerCapability.KEY_COMBINATION: CapabilityMapping(
        PermissionAction.EXECUTE, "keyboard"
    ),
    ComputerCapability.TYPE_TEXT: CapabilityMapping(
        PermissionAction.EXECUTE, "keyboard"
    ),
}

# Every ComputerCapability must have a mapping — fail at import time if a
# future edit adds a capability without wiring its permission mapping.
if set(_CAPABILITY_MAPPING) != set(ComputerCapability):
    raise AssertionError("every ComputerCapability must have a CapabilityMapping")


def mapping_for(capability: ComputerCapability) -> CapabilityMapping:
    """Return the fixed mapping for ``capability``. Never raises for a
    valid ``ComputerCapability`` member — the assertion above guarantees
    every member is present."""

    return _CAPABILITY_MAPPING[capability]
