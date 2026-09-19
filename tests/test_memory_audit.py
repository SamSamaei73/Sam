"""Tests for the memory audit sink abstraction."""

from datetime import UTC, datetime

import pytest

from sam.memory.audit import FailingMemoryAuditSink, InMemoryMemoryAuditSink
from sam.memory.models import (
    MemoryAuditEvent,
    MemoryAuditOutcome,
    MemoryOperation,
    MemoryType,
)
from sam.permissions.models import Principal, PrincipalKind

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _event(**overrides: object) -> MemoryAuditEvent:
    defaults: dict[str, object] = {
        "event_id": "e1",
        "occurred_at": _NOW,
        "operation": MemoryOperation.REMEMBER,
        "principal": Principal(kind=PrincipalKind.USER, id="ali"),
        "memory_type": MemoryType.SEMANTIC,
        "outcome": MemoryAuditOutcome.SUCCESS,
    }
    defaults.update(overrides)
    return MemoryAuditEvent.model_validate(defaults)


def test_record_and_list_events() -> None:
    sink = InMemoryMemoryAuditSink()
    sink.record(_event())

    events = sink.list_events()

    assert len(events) == 1
    assert events[0].event_id == "e1"


def test_events_are_recorded_in_order() -> None:
    sink = InMemoryMemoryAuditSink()
    sink.record(_event(event_id="e1"))
    sink.record(_event(event_id="e2"))

    assert [e.event_id for e in sink.list_events()] == ["e1", "e2"]


def test_two_sink_instances_are_isolated() -> None:
    sink_a = InMemoryMemoryAuditSink()
    sink_b = InMemoryMemoryAuditSink()

    sink_a.record(_event())

    assert len(sink_a.list_events()) == 1
    assert len(sink_b.list_events()) == 0


def test_failing_sink_always_raises() -> None:
    sink = FailingMemoryAuditSink()
    with pytest.raises(RuntimeError):
        sink.record(_event())


def test_audit_event_has_no_field_for_memory_content() -> None:
    field_names = set(MemoryAuditEvent.model_fields)
    assert "content" not in field_names
    assert "reason" not in field_names
