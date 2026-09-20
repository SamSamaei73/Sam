"""Transport boundary.

``MCPTransport`` is deliberately narrow: list the tools a server advertises,
and call one tool. Phase 8 ships only ``FakeMCPTransport``, a deterministic
in-process fake. There is no network transport, no subprocess transport, no
shell, and no dynamic import here — a real stdio/HTTP transport is a later,
separately-reviewed addition (see docs/mcp.md for the requirements it must
meet: a static registered executable, no shell, a minimal explicit
environment, finite timeout, bounded output).

``build_transport_environment`` is the only sanctioned way for a future
transport to obtain environment variables. It builds a minimal environment
from *explicitly configured* trusted values and never reads the process
environment, so ambient secrets (ANTHROPIC_API_KEY, cloud credentials,
database URLs, ...) cannot leak into or redirect an MCP server process.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from threading import RLock
from typing import Any, Protocol

from sam.mcp.credentials import Credential
from sam.mcp.errors import MCPRegistrationError, MCPTransportError
from sam.mcp.models import MCPExecutionContext, MCPToolCall

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MAX_ENV_VALUE_LENGTH = 1_024
_MAX_ENV_ENTRIES = 32
# Names that ambient process environments commonly carry credentials in. A
# trusted transport configuration must never name them: credentials reach a
# server only through the CredentialReference boundary, not through the
# environment.
_FORBIDDEN_ENV_FRAGMENTS = (
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "AWS_",
    "DATABASE_URL",
    "ANTHROPIC",
    "OPENAI",
    "GITHUB",
    "SLACK",
)

Handler = Callable[[Mapping[str, Any], Credential | None], object]


class MCPTransport(Protocol):
    """One connection to one MCP server."""

    def list_tools(self) -> object:
        """Return the server's advertised tools. Untrusted."""

    def call_tool(
        self,
        call: MCPToolCall,
        context: MCPExecutionContext,
        credential: Credential | None,
    ) -> object:
        """Execute one tool call and return its raw result. Untrusted.

        Must honor ``context.timeout_seconds`` cooperatively; the gateway
        also enforces it externally and abandons a call that overruns it.
        ``credential`` is present only if the tool's trusted registry entry
        references one, and only for this server."""


def build_transport_environment(trusted: Mapping[str, str]) -> dict[str, str]:
    """A minimal, explicit environment for a future subprocess transport.

    ``trusted`` must come from Sam's own trusted configuration. The process
    environment is never consulted. Names must be plain upper-case
    identifiers and must not look credential-bearing; values are bounded and
    must not contain control characters.
    """

    if len(trusted) > _MAX_ENV_ENTRIES:
        raise MCPRegistrationError("too many environment entries")
    result: dict[str, str] = {}
    for name, value in trusted.items():
        if not _ENV_NAME_RE.match(name):
            raise MCPRegistrationError("invalid environment variable name")
        if any(fragment in name for fragment in _FORBIDDEN_ENV_FRAGMENTS):
            raise MCPRegistrationError("environment variable name is not allowed")
        if (
            not isinstance(value, str)
            or len(value) > _MAX_ENV_VALUE_LENGTH
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
        ):
            raise MCPRegistrationError("invalid environment variable value")
        result[name] = value
    return result


class FakeMCPTransport:
    """A deterministic in-process transport for tests.

    ``handlers`` maps tool name -> callable(arguments, credential) returning
    the raw result. Every call is recorded (arguments and whether a
    credential was presented — never the credential value).
    """

    def __init__(
        self,
        server_id: str,
        *,
        handlers: Mapping[str, Handler] | None = None,
        advertised: object = None,
    ) -> None:
        self.server_id = server_id
        self._handlers = dict(handlers or {})
        self._advertised = advertised if advertised is not None else []
        self._lock = RLock()
        self.call_count = 0
        self.calls: list[tuple[str, dict[str, Any], bool]] = []

    def set_advertised(self, advertised: object) -> None:
        self._advertised = advertised

    def list_tools(self) -> object:
        return self._advertised

    def call_tool(
        self,
        call: MCPToolCall,
        context: MCPExecutionContext,
        credential: Credential | None,
    ) -> object:
        with self._lock:
            self.call_count += 1
            self.calls.append(
                (call.tool_name, dict(call.arguments), credential is not None)
            )
        if call.server_id != self.server_id:
            # A misrouted call is a bug, never something to serve.
            raise MCPTransportError("call routed to the wrong server")
        handler = self._handlers.get(call.tool_name)
        if handler is None:
            raise MCPTransportError("tool is not implemented")
        return handler(call.arguments, credential)


__all__ = ["FakeMCPTransport", "MCPTransport", "build_transport_environment"]
