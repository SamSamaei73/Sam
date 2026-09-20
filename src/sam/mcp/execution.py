"""Execution boundary: run one already-authorized call through the one
transport registered for its server, under a Sam-owned timeout, and validate
the untrusted result.

Nothing here decides whether a call may happen — that already happened in
the gateway, via the PermissionEngine. This module only *carries out* an
authorized call safely:

* the transport is chosen from the trusted registry entry's ``server_id``,
  never from anything the request or a server supplied, so one server can
  never receive another server's call or credential;
* the timeout is the registry's, enforced externally (a daemon thread is
  abandoned on overrun, its late result discarded);
* transport exceptions are collapsed to a generic ``MCPTransportError``
  with no chained cause, so provider/server text never escapes;
* the result must be bounded plain JSON, and must not echo the credential.

There are no retries. A timeout means the outcome is *unknown*, so the
gateway never re-sends: a caller must issue a new request explicitly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Event, Thread
from typing import Any

from sam.mcp.credentials import Credential
from sam.mcp.errors import (
    MCPError,
    MCPOutputValidationError,
    MCPTimeoutError,
    MCPTransportError,
)
from sam.mcp.models import (
    MCPErrorCategory,
    MCPExecutionContext,
    MCPToolCall,
    MCPToolDescriptor,
)
from sam.mcp.transport import MCPTransport
from sam.mcp.validation import validate_output


def _run_with_timeout(fn: Callable[[], object], timeout: float) -> object:
    box: dict[str, Any] = {}
    done = Event()

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as error:  # noqa: BLE001 - collapsed below, never re-raised as-is
            box["error"] = error
        finally:
            done.set()

    worker = Thread(target=target, name="sam-mcp-call", daemon=True)
    worker.start()
    if not done.wait(timeout):
        raise MCPTimeoutError("tool call timed out") from None
    error = box.get("error")
    if error is not None:
        if isinstance(error, MCPTimeoutError):
            raise MCPTimeoutError("tool call timed out") from None
        raise MCPTransportError("tool call failed") from None
    return box.get("value")


class MCPExecutor:
    def __init__(self, transports: Mapping[str, MCPTransport]) -> None:
        # Copied: later mutation of the caller's mapping cannot re-route calls.
        self._transports: dict[str, MCPTransport] = dict(transports)

    def transport_for(self, server_id: str) -> MCPTransport:
        transport = self._transports.get(server_id)
        if transport is None:
            raise MCPTransportError("no transport is registered for this server")
        return transport

    def execute(
        self,
        descriptor: MCPToolDescriptor,
        call: MCPToolCall,
        context: MCPExecutionContext,
        credential: Credential | None,
    ) -> tuple[str, int]:
        """Return ``(content_json, size_bytes)`` or raise an ``MCPError``."""

        transport = self.transport_for(descriptor.server_id)
        raw = _run_with_timeout(
            lambda: transport.call_tool(call, context, credential),
            context.timeout_seconds,
        )
        try:
            content_json, size = validate_output(raw, descriptor)
        except MCPError:
            raise
        except Exception:
            raise MCPOutputValidationError("tool output is invalid") from None
        if credential is not None and credential.appears_in(content_json):
            # A server echoed the secret back. Discard the whole result.
            raise MCPOutputValidationError(
                "tool output was rejected", category=MCPErrorCategory.CREDENTIAL_LEAK
            )
        return content_json, size


__all__ = ["MCPExecutor"]
