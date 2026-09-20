"""Sam's MCP Gateway & Integrations Framework (Phase 8).

MCP is an integration protocol, not a security boundary. Every externally
consequential operation stays subject to Sam's trusted registry and the
``PermissionEngine`` — the sole authorization authority. See
``sam.mcp.gateway.MCPGateway`` for the execution lifecycle and
``docs/mcp.md`` for the trust model.

Phase 8 implements the framework only, against deterministic fakes: no real
integration, no network, no real credentials, no OAuth.
"""
