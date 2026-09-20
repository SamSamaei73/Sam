"""The trusted tool registry, split into two structurally separate trust
domains.

* **Runtime registry access** — ``MCPRegistryReader`` (a Protocol) and its
  only implementation ``MCPRegistryView``. It can resolve and list trusted
  entries and has *no* method that mutates anything. This is all
  ``MCPGateway``, ``MCPAgentBoundary`` and AgentCore ever receive.
* **Administrative registry mutation** — ``MCPRegistryAdmin``. It alone can
  register servers/tools, enable/disable a tool, and apply a discovery
  result (which can only disable). It is held by trusted administrative
  code and is never passed to the runtime path.

The separation is structural, not a naming convention: the view is built
from ``types.MappingProxyType`` proxies of the admin's dictionaries, so a
holder of the view has no reference to the admin and no writable handle to
the data — even by introspection it can only read. ``MCPGateway`` also
refuses, at construction, any registry object that exposes an administrative
method.

Discovery never authorizes or registers a tool: see ``sam.mcp.discovery``
(read-only comparison producing a data-only ``MCPDiscoveryResult``) and
``MCPRegistryAdmin.apply_discovery`` (restrictive only).
"""

from __future__ import annotations

from collections.abc import Mapping
from threading import RLock
from types import MappingProxyType
from typing import Protocol

from sam.mcp.errors import MCPRegistrationError, MCPToolNotFoundError
from sam.mcp.models import (
    MAX_MCP_SERVERS,
    MAX_MCP_TOOLS_PER_SERVER,
    MCPDiscoveryResult,
    MCPServerDescriptor,
    MCPToolDescriptor,
    MCPToolId,
)

# Attribute names that constitute administrative capability. The gateway
# refuses any registry object exposing one of these (see MCPGateway).
ADMIN_CAPABILITY_NAMES: frozenset[str] = frozenset(
    {"register_server", "register_tool", "enable", "disable", "apply_discovery"}
)


class MCPRegistryReader(Protocol):
    """The runtime registry interface: resolve and list, nothing else."""

    def get_tool(self, tool_id: str | MCPToolId) -> MCPToolDescriptor: ...

    def get_server(self, server_id: str) -> MCPServerDescriptor: ...

    def list_tools(
        self, server_id: str | None = None
    ) -> tuple[MCPToolDescriptor, ...]: ...

    def list_servers(self) -> tuple[MCPServerDescriptor, ...]: ...


class MCPRegistryView:
    """Read-only view of a registry. Holds only read-only mapping proxies."""

    __slots__ = ("_lock", "_servers", "_tools")

    _tools: Mapping[str, MCPToolDescriptor]
    _servers: Mapping[str, MCPServerDescriptor]
    _lock: RLock

    def __init__(
        self,
        tools: Mapping[str, MCPToolDescriptor],
        servers: Mapping[str, MCPServerDescriptor],
        lock: RLock,
    ) -> None:
        object.__setattr__(self, "_tools", tools)
        object.__setattr__(self, "_servers", servers)
        object.__setattr__(self, "_lock", lock)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("the registry view is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("the registry view is read-only")

    def get_tool(self, tool_id: str | MCPToolId) -> MCPToolDescriptor:
        """Resolve exactly one trusted entry or raise. Values returned are
        frozen models; mutating them cannot affect the registry."""

        with self._lock:
            found = self._tools.get(str(tool_id))
        if found is None:
            raise MCPToolNotFoundError("tool is not registered")
        return found

    def get_server(self, server_id: str) -> MCPServerDescriptor:
        with self._lock:
            found = self._servers.get(server_id)
        if found is None:
            raise MCPToolNotFoundError("server is not registered")
        return found

    def list_tools(self, server_id: str | None = None) -> tuple[MCPToolDescriptor, ...]:
        with self._lock:
            tools = tuple(self._tools.values())
        if server_id is not None:
            tools = tuple(t for t in tools if t.server_id == server_id)
        return tuple(sorted(tools, key=lambda t: str(t.tool_id)))

    def list_servers(self) -> tuple[MCPServerDescriptor, ...]:
        with self._lock:
            return tuple(sorted(self._servers.values(), key=lambda s: s.server_id))


class MCPRegistryAdmin:
    """Administrative mutation of Sam's trusted registry.

    Constructed and held only by trusted administrative code. Every method
    is an explicit, programmatic decision by Sam; nothing here is reachable
    from the runtime path, from server output, or from an LLM.
    """

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerDescriptor] = {}
        self._tools: dict[str, MCPToolDescriptor] = {}
        self._folded: set[tuple[str, str]] = set()
        self._lock = RLock()

    def reader(self) -> MCPRegistryReader:
        """A live, read-only view. Hand *this* to the runtime path."""

        return MCPRegistryView(
            MappingProxyType(self._tools), MappingProxyType(self._servers), self._lock
        )

    def register_server(self, server: MCPServerDescriptor) -> None:
        with self._lock:
            if server.server_id in self._servers:
                raise MCPRegistrationError("server is already registered")
            if len(self._servers) >= MAX_MCP_SERVERS:
                raise MCPRegistrationError("too many servers registered")
            self._servers[server.server_id] = server

    def register_tool(self, tool: MCPToolDescriptor) -> None:
        """Add one trusted tool. Duplicates (including case-only variants) and
        tools for unregistered servers are rejected; an existing entry is
        never replaced — there is deliberately no schema-replacement path."""

        with self._lock:
            if tool.server_id not in self._servers:
                raise MCPRegistrationError("server is not registered")
            key = str(tool.tool_id)
            folded = (tool.server_id, tool.tool_name.casefold())
            if key in self._tools or folded in self._folded:
                raise MCPRegistrationError("tool is already registered")
            per_server = sum(
                1 for t in self._tools.values() if t.server_id == tool.server_id
            )
            if per_server >= MAX_MCP_TOOLS_PER_SERVER:
                raise MCPRegistrationError("too many tools registered for server")
            self._tools[key] = tool
            self._folded.add(folded)

    def disable(self, tool_id: str | MCPToolId) -> None:
        self._set_enabled(tool_id, False)

    def enable(self, tool_id: str | MCPToolId) -> None:
        """Re-enabling only lifts the *additional* restriction; it never
        grants permission — the PermissionEngine still decides."""

        self._set_enabled(tool_id, True)

    def _set_enabled(self, tool_id: str | MCPToolId, enabled: bool) -> None:
        with self._lock:
            current = self._tools.get(str(tool_id))
            if current is None:
                raise MCPToolNotFoundError("tool is not registered")
            self._tools[str(current.tool_id)] = current.model_copy(
                update={"enabled": enabled}
            )

    def apply_discovery(self, result: MCPDiscoveryResult) -> tuple[str, ...]:
        """Apply a discovery result. **Restrictive only**: it disables
        registered tools whose advertised schema no longer matches. It never
        registers, replaces, or enables anything, and it never touches a
        binding, credential reference, schema, timeout, or limit. Returns
        the tool ids it disabled."""

        disabled: list[str] = []
        with self._lock:
            for key in result.schema_mismatch:
                current = self._tools.get(key)
                if current is None or current.server_id != result.server_id:
                    continue
                if current.enabled:
                    self._tools[key] = current.model_copy(update={"enabled": False})
                    disabled.append(key)
        return tuple(sorted(disabled))


__all__ = [
    "ADMIN_CAPABILITY_NAMES",
    "MCPRegistryAdmin",
    "MCPRegistryReader",
    "MCPRegistryView",
]
