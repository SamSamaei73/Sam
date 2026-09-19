"""Tests for the audit sink abstraction."""

from datetime import UTC, datetime

import pytest

from sam.permissions.audit import FailingAuditSink, InMemoryAuditSink
from sam.permissions.models import (
    AuditEvent,
    DecisionOutcome,
    PermissionAction,
    PermissionResource,
    Principal,
    PrincipalKind,
    RiskLevel,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _event(**overrides: object) -> AuditEvent:
    defaults: dict[str, object] = {
        "event_id": "e1",
        "occurred_at": _NOW,
        "principal": Principal(kind=PrincipalKind.USER, id="ali"),
        "action": PermissionAction.READ,
        "resource": PermissionResource.FILESYSTEM,
        "scope_summary": "Projects/Sam",
        "risk": RiskLevel.LOW,
        "outcome": DecisionOutcome.ALLOW,
    }
    defaults.update(overrides)
    return AuditEvent.model_validate(defaults)


def test_record_and_list_events() -> None:
    sink = InMemoryAuditSink()
    sink.record(_event())

    events = sink.list_events()

    assert len(events) == 1
    assert events[0].event_id == "e1"


def test_events_are_recorded_in_order() -> None:
    sink = InMemoryAuditSink()
    sink.record(_event(event_id="e1"))
    sink.record(_event(event_id="e2"))

    events = sink.list_events()

    assert [event.event_id for event in events] == ["e1", "e2"]


def test_list_events_returns_a_snapshot_not_a_live_view() -> None:
    sink = InMemoryAuditSink()
    sink.record(_event(event_id="e1"))

    snapshot = sink.list_events()
    sink.record(_event(event_id="e2"))

    assert len(snapshot) == 1


def test_two_sink_instances_are_isolated() -> None:
    sink_a = InMemoryAuditSink()
    sink_b = InMemoryAuditSink()

    sink_a.record(_event())

    assert len(sink_a.list_events()) == 1
    assert len(sink_b.list_events()) == 0


def test_failing_sink_always_raises() -> None:
    sink = FailingAuditSink()
    with pytest.raises(RuntimeError):
        sink.record(_event())
