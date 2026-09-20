"""Audit sink for MCP gateway requests.

Same shape and fail-safe contract as ``sam.permissions.audit`` /
``sam.knowledge.audit``: ``record(event) -> None``, and a failing sink never
changes the result of the operation it records (audit is observability, not
an authorization gate — an audit failure can never turn a denial into an
allow, because authorization was decided before the event exists). Events
are content-free: see ``sam.mcp.models.MCPAuditEvent``.
"""

from __future__ import annotations

from collections.abc import Sequence
from threading import RLock
from typing import Protocol

from sam.mcp.models import MCPAuditEvent


class MCPAuditSink(Protocol):
    def record(self, event: MCPAuditEvent) -> None: ...


class InMemoryMCPAuditSink:
    def __init__(self) -> None:
        self._events: list[MCPAuditEvent] = []
        self._lock = RLock()

    def record(self, event: MCPAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def list_events(self) -> Sequence[MCPAuditEvent]:
        with self._lock:
            return tuple(self._events)


class FailingMCPAuditSink:
    """Always raises — proves audit unavailability never changes a result."""

    def record(self, event: MCPAuditEvent) -> None:
        raise RuntimeError("audit sink unavailable")


__all__ = ["FailingMCPAuditSink", "InMemoryMCPAuditSink", "MCPAuditSink"]
