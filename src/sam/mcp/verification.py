"""Result verification.

A server returning ``{"success": true}`` does not prove an external side
effect happened. A verifier is an independent, provider-specific check
(for example, "read the sent message back") that a future real integration
supplies. Statuses are honest:

* ``VERIFIED``      the side effect was independently confirmed
* ``UNVERIFIED``    verification was required but could not confirm it
* ``FAILED``        verification ran and the effect is absent/wrong
* ``NOT_REQUIRED``  the trusted registry entry does not require it

Sam never reports ``VERIFIED`` unless a verifier said so.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from sam.mcp.models import (
    MCPExecutionContext,
    MCPToolCall,
    MCPToolDescriptor,
    MCPVerificationResult,
    VerificationStatus,
)


class MCPResultVerifier(Protocol):
    def verify(
        self,
        descriptor: MCPToolDescriptor,
        call: MCPToolCall,
        context: MCPExecutionContext,
        content_json: str,
    ) -> MCPVerificationResult: ...


class FakeMCPVerifier:
    """Deterministic verifier for tests. ``decide`` receives the (validated)
    result JSON and returns a status; the default verifies."""

    def __init__(
        self,
        decide: Callable[[str], VerificationStatus] | None = None,
        *,
        raises: bool = False,
    ) -> None:
        self._decide = decide or (lambda _content: VerificationStatus.VERIFIED)
        self._raises = raises
        self.calls = 0

    def verify(
        self,
        descriptor: MCPToolDescriptor,
        call: MCPToolCall,
        context: MCPExecutionContext,
        content_json: str,
    ) -> MCPVerificationResult:
        self.calls += 1
        if self._raises:
            raise RuntimeError("verifier unavailable")
        return MCPVerificationResult(status=self._decide(content_json))


__all__ = ["FakeMCPVerifier", "MCPResultVerifier"]
