"""Tests for the coding-agent audit sink abstraction."""

from datetime import UTC, datetime

import pytest

from sam.coding.audit import FailingCodingAuditSink, InMemoryCodingAuditSink
from sam.coding.models import (
    CodingAuditEvent,
    CodingOperation,
    PermissionOutcomeSummary,
)
from sam.permissions.models import Principal, PrincipalKind, RiskLevel

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _event(**overrides: object) -> CodingAuditEvent:
    defaults: dict[str, object] = {
        "event_id": "e1",
        "occurred_at": _NOW,
        "operation_id": "op1",
        "principal": Principal(kind=PrincipalKind.USER, id="ali"),
        "repository_id": "sam-core",
        "operation": CodingOperation.READ_FILE,
        "risk": RiskLevel.LOW,
        "permission_outcome": PermissionOutcomeSummary.ALLOW,
    }
    defaults.update(overrides)
    return CodingAuditEvent.model_validate(defaults)


def test_record_and_list_events() -> None:
    sink = InMemoryCodingAuditSink()
    sink.record(_event())
    events = sink.list_events()
    assert len(events) == 1
    assert events[0].event_id == "e1"


def test_events_are_recorded_in_order() -> None:
    sink = InMemoryCodingAuditSink()
    sink.record(_event(event_id="e1"))
    sink.record(_event(event_id="e2"))
    assert [e.event_id for e in sink.list_events()] == ["e1", "e2"]


def test_two_sink_instances_are_isolated() -> None:
    sink_a = InMemoryCodingAuditSink()
    sink_b = InMemoryCodingAuditSink()
    sink_a.record(_event())
    assert len(sink_a.list_events()) == 1
    assert len(sink_b.list_events()) == 0


def test_failing_sink_always_raises() -> None:
    sink = FailingCodingAuditSink()
    with pytest.raises(RuntimeError):
        sink.record(_event())
