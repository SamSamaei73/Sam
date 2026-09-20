"""Untrusted server discovery: read-only comparison, data-only result.

    MCP server -> (untrusted listing) -> MCPDiscoveryService.discover
               -> MCPDiscoveryResult (frozen data) -> [trusted admin step]

The service holds only the read-only ``MCPRegistryReader`` and the
transports. It has no registry mutation capability, and the result it
returns is a frozen data model — it carries no handle to anything. Whether
a result is ever acted on is decided separately by trusted administrative
code via ``MCPRegistryAdmin.apply_discovery`` (which can only *disable*).

**Discovery never authorizes or registers a tool.** An advertised tool that
has no trusted registry entry is reported as ``unexpected`` and stays
unavailable; it becomes executable only if trusted code explicitly registers
it through ``MCPRegistryAdmin.register_tool`` with a reviewed binding.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sam.mcp.client import MCPClient
from sam.mcp.errors import MCPToolNotFoundError
from sam.mcp.models import MCPDiscoveredTool, MCPDiscoveryResult
from sam.mcp.registry import MCPRegistryReader
from sam.mcp.transport import MCPTransport


def compare_discovery(
    registry: MCPRegistryReader,
    server_id: str,
    discovered: Sequence[MCPDiscoveredTool],
    *,
    malformed_count: int = 0,
) -> MCPDiscoveryResult:
    """Pure, read-only comparison of advertised tools to the registry."""

    registry.get_server(server_id)
    matched: list[str] = []
    unexpected: list[str] = []
    mismatched: list[str] = []
    seen: set[str] = set()
    for item in discovered:
        key = f"{server_id}:{item.name}"
        seen.add(key)
        try:
            trusted = registry.get_tool(key)
        except MCPToolNotFoundError:
            unexpected.append(item.name)
            continue
        if item.input_schema != trusted.input_schema:
            mismatched.append(key)
        else:
            matched.append(key)
    registered = [str(t.tool_id) for t in registry.list_tools(server_id)]
    return MCPDiscoveryResult(
        server_id=server_id,
        matched=tuple(sorted(matched)),
        unexpected=tuple(sorted(unexpected)),
        schema_mismatch=tuple(sorted(mismatched)),
        not_advertised=tuple(sorted(k for k in registered if k not in seen)),
        malformed_count=malformed_count,
    )


class MCPDiscoveryService:
    def __init__(
        self, *, registry: MCPRegistryReader, transports: Mapping[str, MCPTransport]
    ) -> None:
        self._registry = registry
        self._transports: dict[str, MCPTransport] = dict(transports)

    def discover(self, server_id: str) -> MCPDiscoveryResult:
        """List the server's tools and compare them with the registry.
        Mutates nothing."""

        self._registry.get_server(server_id)
        transport = self._transports.get(server_id)
        if transport is None:
            raise MCPToolNotFoundError("no transport is registered for this server")
        found = MCPClient(transport).discover()
        return compare_discovery(
            self._registry,
            server_id,
            found.tools,
            malformed_count=found.malformed_count,
        )


__all__ = ["MCPDiscoveryService", "compare_discovery"]
