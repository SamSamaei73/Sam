"""MCP tool request -> ``PermissionRequest`` translation.

This layer only *builds* a request. It never decides anything: the result
must go to ``PermissionEngine.evaluate``, which is the sole authorization
authority. It reads exactly two things — the trusted registry entry and the
already-validated arguments — and never a tool description, a server-claimed
risk, or anything an LLM wrote outside the validated argument values.

Scope is deterministic: ``(server_id, tool_name, *scope-argument values)``.
The server id and tool name prefix guarantees a grant for one server/tool can
never match another (no confused deputy), and that a grant issued for a
native (non-MCP) tool can never match an MCP request. Each scope-argument
value must be a safe single segment (no ``/``, ``:``, whitespace, ``..``),
so a value cannot smuggle extra scope depth or traverse to a sibling.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from sam.mcp.errors import MCPPolicyError
from sam.mcp.models import (
    MAX_MCP_SCOPE_SEGMENT_LENGTH,
    SCOPE_SEGMENT_RE,
    MCPToolDescriptor,
    MCPToolRequest,
    canonical_json,
)
from sam.permissions.models import PermissionRequest, PermissionScope, RiskLevel
from sam.permissions.policy import classify


def _segment(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise MCPPolicyError("scope argument is not a safe scope segment") from None
    text = str(value)
    if (
        not text
        or len(text) > MAX_MCP_SCOPE_SEGMENT_LENGTH
        or not SCOPE_SEGMENT_RE.match(text)
        or set(text) == {"."}
    ):
        raise MCPPolicyError("scope argument is not a safe scope segment") from None
    return text


def derive_scope(
    descriptor: MCPToolDescriptor, arguments: Mapping[str, Any]
) -> PermissionScope:
    """Deterministic scope from trusted registry data + validated arguments."""

    segments = [descriptor.server_id, descriptor.tool_name]
    for name in descriptor.binding.scope_arguments:
        if name not in arguments:
            raise MCPPolicyError("scope argument is missing") from None
        segments.append(_segment(arguments[name]))
    try:
        return PermissionScope(segments=tuple(segments))
    except (ValidationError, ValueError):
        raise MCPPolicyError("scope could not be constructed") from None


def arguments_digest(arguments: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical argument JSON. Binds a confirmation to the
    exact arguments the user approved — never reversible into content."""

    return hashlib.sha256(canonical_json(dict(arguments)).encode("utf-8")).hexdigest()


def build_permission_request(
    descriptor: MCPToolDescriptor,
    request: MCPToolRequest,
    arguments: Mapping[str, Any],
) -> PermissionRequest:
    """Build (never decide) the ``PermissionRequest`` for one tool call.

    ``target`` carries the canonical tool id plus a digest of the validated
    arguments. ``PermissionEngine`` binds a confirmation to its target, so a
    confirmation approved for one set of arguments can never be consumed by
    the same tool with different arguments.
    """

    scope = derive_scope(descriptor, arguments)
    try:
        return PermissionRequest(
            principal=request.principal,
            action=descriptor.binding.action,
            resource=descriptor.binding.resource,
            scope=scope,
            reason=request.reason,
            target=f"{descriptor.tool_id} #{arguments_digest(arguments)[:16]}",
            correlation_id=request.request_id,
        )
    except ValidationError:
        raise MCPPolicyError("permission request could not be constructed") from None


def risk_for(descriptor: MCPToolDescriptor) -> RiskLevel:
    """The deterministic risk from ``sam.permissions.policy`` for the tool's
    trusted binding — never a value a server claims."""

    entry = classify(descriptor.binding.resource, descriptor.binding.action)
    return entry.risk if entry is not None else RiskLevel.CRITICAL


__all__ = ["arguments_digest", "build_permission_request", "derive_scope", "risk_for"]
