"""Computer action → Permission Engine request mapping.

This module makes **no authorization decision itself** — it only
translates a concrete ``ComputerAction`` into a
``sam.permissions.models.PermissionRequest`` using the static mapping in
``sam.computer.capabilities``. The actual ALLOW / CONFIRM_REQUIRED / DENY
decision is made exclusively by
``sam.permissions.engine.PermissionEngine.evaluate`` — see
``sam.computer.controller``, which is the only caller of ``build_request``
that then hands the result to that engine. Duplicating any part of that
decision here would be exactly the "do not duplicate the Permission
Engine" mistake the Phase 5 task warns against.

The ``target`` on the resulting request exists only to bind a
confirmation to the *exact* action it was issued for (see
``sam.permissions.confirmation`` — a confirmation's ``consume`` requires
an exact ``target`` match) — never to describe the action for humans.
It is chosen to be safe to display and, for ``TYPE_TEXT``, to never
contain the typed text itself: a truncated SHA-256 hex digest binds the
confirmation to that exact string without ever exposing it. See
``docs/computer-control.md``.
"""

from __future__ import annotations

import hashlib

from sam.computer.capabilities import mapping_for
from sam.computer.models import (
    ComputerAction,
    ComputerCapability,
    KeyCombinationAction,
    KeyPressAction,
    MouseClickAction,
    MouseDoubleClickAction,
    MouseMoveAction,
    MouseScrollAction,
    TypeTextAction,
)
from sam.permissions.models import (
    PermissionRequest,
    PermissionResource,
    PermissionScope,
    RiskLevel,
)
from sam.permissions.policy import classify

_TARGET_HASH_LENGTH = 16


def _target_for(action: ComputerAction) -> str | None:
    """A safe, bounded, content-free-for-typed-text summary of *what* the
    action targets, for confirmation binding and human display. Never the
    raw typed text — see the module docstring."""

    if isinstance(action, MouseMoveAction | MouseClickAction | MouseDoubleClickAction):
        return f"{action.x},{action.y}"
    if isinstance(action, MouseScrollAction):
        return f"{action.x},{action.y},{action.direction.value},{action.amount}"
    if isinstance(action, KeyPressAction):
        return f"key:{action.key}"
    if isinstance(action, KeyCombinationAction):
        modifiers = "+".join(m.value for m in action.modifiers)
        return f"combo:{modifiers}+{action.key}"
    if isinstance(action, TypeTextAction):
        digest = hashlib.sha256(action.text.encode("utf-8")).hexdigest()
        return f"sha256:{digest[:_TARGET_HASH_LENGTH]}"
    return None


def risk_for(capability: ComputerCapability) -> RiskLevel:
    """The deterministic risk for one capability, straight from
    ``sam.permissions.policy`` — never computed independently. Used only
    for audit records; the actual authorization decision always comes
    from ``PermissionEngine.evaluate``, not from this function.
    """

    mapping = mapping_for(capability)
    entry = classify(PermissionResource.COMPUTER, mapping.permission_action)
    if entry is None:
        # Should be unreachable: sam.permissions.policy classifies every
        # (COMPUTER, action) pair this module's mapping can produce (see
        # its "computer" section). If it ever did not, treat it with
        # maximum caution rather than guess.
        return RiskLevel.CRITICAL
    return entry.risk


def build_request(
    action: ComputerAction, *, reason: str | None = None
) -> PermissionRequest:
    """Translate one action into a ``PermissionRequest``. Pure — no I/O,
    no calls to the Permission Engine."""

    mapping = mapping_for(action.capability)
    return PermissionRequest(
        principal=action.principal,
        action=mapping.permission_action,
        resource=PermissionResource.COMPUTER,
        scope=PermissionScope.identifier(mapping.scope_segment),
        reason=reason,
        target=_target_for(action),
    )
