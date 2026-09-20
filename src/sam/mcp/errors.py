"""Typed errors for the MCP gateway.

Every error carries a closed ``category`` (see
``sam.mcp.models.MCPErrorCategory``) and a fixed, generic message. Messages
never include credentials, argument values, result bodies, environment
values, or the text of an underlying provider/server exception — the
underlying exception is deliberately not chained into anything that could
be logged or shown (``raise ... from None`` at every boundary).
"""

from __future__ import annotations

from sam.mcp.models import MCPErrorCategory


class MCPError(Exception):
    """Base class. ``category`` is what the gateway reports; ``str(error)``
    is only ever a short, generic, content-free message."""

    category: MCPErrorCategory = MCPErrorCategory.INTERNAL_ERROR


class MCPRegistrationError(MCPError):
    """A trusted-registry mutation was rejected (duplicate, malformed,
    unknown server, unsupported binding)."""

    category = MCPErrorCategory.REGISTRATION_ERROR


class MCPToolNotFoundError(MCPError):
    category = MCPErrorCategory.UNKNOWN_TOOL


class MCPToolDisabledError(MCPError):
    category = MCPErrorCategory.TOOL_DISABLED


class MCPPolicyError(MCPError):
    """Deterministic policy/scope construction failed (for example a
    scope-bearing argument that is not a safe scope segment)."""

    category = MCPErrorCategory.POLICY_ERROR


class MCPPermissionError(MCPError):
    category = MCPErrorCategory.PERMISSION_DENIED


class MCPCredentialError(MCPError):
    category = MCPErrorCategory.CREDENTIAL_ERROR


class MCPInputValidationError(MCPError):
    category = MCPErrorCategory.INPUT_INVALID

    def __init__(
        self,
        message: str = "tool arguments are invalid",
        *,
        category: MCPErrorCategory = MCPErrorCategory.INPUT_INVALID,
    ) -> None:
        super().__init__(message)
        self.category = category


class MCPTransportError(MCPError):
    category = MCPErrorCategory.TRANSPORT_ERROR


class MCPTimeoutError(MCPError):
    category = MCPErrorCategory.TIMEOUT


class MCPOutputValidationError(MCPError):
    category = MCPErrorCategory.OUTPUT_INVALID

    def __init__(
        self,
        message: str = "tool output is invalid",
        *,
        category: MCPErrorCategory = MCPErrorCategory.OUTPUT_INVALID,
    ) -> None:
        super().__init__(message)
        self.category = category


class MCPVerificationError(MCPError):
    category = MCPErrorCategory.VERIFICATION_FAILED


__all__ = [
    "MCPCredentialError",
    "MCPError",
    "MCPInputValidationError",
    "MCPOutputValidationError",
    "MCPPermissionError",
    "MCPPolicyError",
    "MCPRegistrationError",
    "MCPTimeoutError",
    "MCPToolDisabledError",
    "MCPToolNotFoundError",
    "MCPTransportError",
    "MCPVerificationError",
]
