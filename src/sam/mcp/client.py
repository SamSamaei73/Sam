"""Discovery client: turns a transport's untrusted tool listing into bounded,
typed ``MCPDiscoveredTool`` values.

Everything a server advertises is untrusted. The parser keeps only what it
can validate (a well-formed name, a bounded sanitized description, a schema
inside Sam's strict subset) and *counts* everything else it discards —
including security-looking keys such as ``risk`` or ``requires_confirmation``
— without ever reading them. Nothing here executes a tool or registers one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from sam.mcp.errors import MCPTransportError
from sam.mcp.models import (
    MAX_MCP_DESCRIPTION_LENGTH,
    MAX_MCP_TOOLS_PER_SERVER,
    MCPDiscoveredTool,
    MCPToolSchema,
    sanitize_display_text,
)
from sam.mcp.transport import MCPTransport

_KNOWN_KEYS = frozenset({"name", "description", "input_schema", "inputSchema"})


@dataclass(frozen=True)
class DiscoveryResult:
    tools: tuple[MCPDiscoveredTool, ...]
    malformed_count: int


class MCPClient:
    def __init__(self, transport: MCPTransport) -> None:
        self._transport = transport

    def discover(self) -> DiscoveryResult:
        try:
            raw = self._transport.list_tools()
        except Exception:
            raise MCPTransportError("tool discovery failed") from None
        if not isinstance(raw, list) or len(raw) > MAX_MCP_TOOLS_PER_SERVER:
            raise MCPTransportError("tool discovery returned an invalid listing")
        tools: list[MCPDiscoveredTool] = []
        malformed = 0
        seen: set[str] = set()
        for item in raw:
            parsed = _parse_tool(item)
            if parsed is None or parsed.name in seen:
                malformed += 1
                continue
            seen.add(parsed.name)
            tools.append(parsed)
        return DiscoveryResult(tools=tuple(tools), malformed_count=malformed)


def _parse_tool(item: object) -> MCPDiscoveredTool | None:
    if not isinstance(item, Mapping):
        return None
    name = item.get("name")
    if not isinstance(name, str):
        return None
    description = item.get("description")
    text = (
        sanitize_display_text(description, max_length=MAX_MCP_DESCRIPTION_LENGTH)
        if isinstance(description, str)
        else None
    )
    schema_raw: Any = item.get("input_schema", item.get("inputSchema"))
    schema: MCPToolSchema | None
    try:
        schema = MCPToolSchema.from_untrusted(schema_raw)
    except (ValueError, ValidationError):
        schema = None
    ignored = sum(1 for key in item if key not in _KNOWN_KEYS)
    try:
        return MCPDiscoveredTool(
            name=name,
            description=text or "",
            input_schema=schema,
            ignored_key_count=ignored,
        )
    except ValidationError:
        return None


__all__ = ["DiscoveryResult", "MCPClient"]
