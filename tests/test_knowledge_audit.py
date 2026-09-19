"""Tests for sam.knowledge.audit."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sam.knowledge.audit import FailingKnowledgeAuditSink, InMemoryKnowledgeAuditSink
from sam.knowledge.models import (
    KnowledgeAuditEvent,
    KnowledgeOperation,
    PermissionOutcomeSummary,
)
from sam.permissions.models import Principal, PrincipalKind, RiskLevel

NOW = datetime.now(UTC)
PRINCIPAL = Principal(kind=PrincipalKind.USER, id="alice")


def _event() -> KnowledgeAuditEvent:
    return KnowledgeAuditEvent(
        event_id="e1",
        occurred_at=NOW,
        operation_id="op1",
        principal=PRINCIPAL,
        collection_id="docs",
        operation=KnowledgeOperation.INGEST_RESOURCE,
        risk=RiskLevel.MEDIUM,
        permission_outcome=PermissionOutcomeSummary.ALLOW,
    )


class TestInMemoryKnowledgeAuditSink:
    def test_records_and_lists_events(self) -> None:
        sink = InMemoryKnowledgeAuditSink()
        event = _event()
        sink.record(event)
        assert sink.list_events() == (event,)

    def test_events_are_append_only_snapshot(self) -> None:
        sink = InMemoryKnowledgeAuditSink()
        sink.record(_event())
        snapshot = sink.list_events()
        sink.record(_event())
        assert len(snapshot) == 1


class TestFailingKnowledgeAuditSink:
    def test_always_raises(self) -> None:
        sink = FailingKnowledgeAuditSink()
        with pytest.raises(RuntimeError):
            sink.record(_event())
