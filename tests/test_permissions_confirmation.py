"""Tests for the confirmation abstraction: lifecycle, expiry, replay."""

from datetime import UTC, datetime, timedelta

import pytest

from sam.permissions.confirmation import InMemoryConfirmationProvider
from sam.permissions.errors import ConfirmationInvalidError, ConfirmationNotFoundError
from sam.permissions.models import (
    ConfirmationStatus,
    PermissionAction,
    PermissionRequest,
    PermissionResource,
    PermissionScope,
    Principal,
    PrincipalKind,
    RiskLevel,
)

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _principal(value: str = "ali") -> Principal:
    return Principal(kind=PrincipalKind.USER, id=value)


def _request(**overrides: object) -> PermissionRequest:
    defaults: dict[str, object] = {
        "principal": _principal(),
        "action": PermissionAction.SEND,
        "resource": PermissionResource.GMAIL,
        "scope": PermissionScope.identifier("mailbox"),
        "target": "message-a",
    }
    defaults.update(overrides)
    return PermissionRequest.model_validate(defaults)


def test_request_creates_pending_confirmation() -> None:
    provider = InMemoryConfirmationProvider()

    record = provider.request(_request(), risk=RiskLevel.HIGH, now=_NOW)

    assert record.status is ConfirmationStatus.PENDING
    assert record.risk is RiskLevel.HIGH
    assert record.target == "message-a"


def test_decide_approve_transitions_to_approved() -> None:
    provider = InMemoryConfirmationProvider()
    record = provider.request(_request(), risk=RiskLevel.HIGH, now=_NOW)

    decided = provider.decide(record.confirmation_id, approved=True, now=_NOW)

    assert decided.status is ConfirmationStatus.APPROVED
    assert decided.decided_at == _NOW


def test_decide_deny_transitions_to_denied() -> None:
    provider = InMemoryConfirmationProvider()
    record = provider.request(_request(), risk=RiskLevel.HIGH, now=_NOW)

    decided = provider.decide(record.confirmation_id, approved=False, now=_NOW)

    assert decided.status is ConfirmationStatus.DENIED


def test_decide_unknown_confirmation_raises() -> None:
    provider = InMemoryConfirmationProvider()
    with pytest.raises(ConfirmationNotFoundError):
        provider.decide("missing", approved=True, now=_NOW)


def test_decide_already_decided_confirmation_raises() -> None:
    provider = InMemoryConfirmationProvider()
    record = provider.request(_request(), risk=RiskLevel.HIGH, now=_NOW)
    provider.decide(record.confirmation_id, approved=True, now=_NOW)

    with pytest.raises(ConfirmationInvalidError):
        provider.decide(record.confirmation_id, approved=True, now=_NOW)


def test_consume_approved_confirmation_succeeds_and_binds_context() -> None:
    provider = InMemoryConfirmationProvider()
    request = _request()
    record = provider.request(request, risk=RiskLevel.HIGH, now=_NOW)
    provider.decide(record.confirmation_id, approved=True, now=_NOW)

    consumed = provider.consume(record.confirmation_id, request=request, now=_NOW)

    assert consumed.status is ConfirmationStatus.CONSUMED


def test_consume_unapproved_pending_confirmation_raises() -> None:
    provider = InMemoryConfirmationProvider()
    request = _request()
    record = provider.request(request, risk=RiskLevel.HIGH, now=_NOW)

    with pytest.raises(ConfirmationInvalidError):
        provider.consume(record.confirmation_id, request=request, now=_NOW)


def test_consume_denied_confirmation_raises() -> None:
    provider = InMemoryConfirmationProvider()
    request = _request()
    record = provider.request(request, risk=RiskLevel.HIGH, now=_NOW)
    provider.decide(record.confirmation_id, approved=False, now=_NOW)

    with pytest.raises(ConfirmationInvalidError):
        provider.consume(record.confirmation_id, request=request, now=_NOW)


def test_consume_is_one_time_replay_is_rejected() -> None:
    """A confirmation must never authorize a second use (replay)."""

    provider = InMemoryConfirmationProvider()
    request = _request()
    record = provider.request(request, risk=RiskLevel.HIGH, now=_NOW)
    provider.decide(record.confirmation_id, approved=True, now=_NOW)
    provider.consume(record.confirmation_id, request=request, now=_NOW)

    with pytest.raises(ConfirmationInvalidError):
        provider.consume(record.confirmation_id, request=request, now=_NOW)


def test_consume_rejects_mismatched_target() -> None:
    """A confirmation for message A must not authorize message B."""

    provider = InMemoryConfirmationProvider()
    request_a = _request(target="message-a")
    record = provider.request(request_a, risk=RiskLevel.HIGH, now=_NOW)
    provider.decide(record.confirmation_id, approved=True, now=_NOW)

    request_b = _request(target="message-b")
    with pytest.raises(ConfirmationInvalidError):
        provider.consume(record.confirmation_id, request=request_b, now=_NOW)


def test_consume_rejects_mismatched_action() -> None:
    provider = InMemoryConfirmationProvider()
    request = _request(action=PermissionAction.SEND)
    record = provider.request(request, risk=RiskLevel.HIGH, now=_NOW)
    provider.decide(record.confirmation_id, approved=True, now=_NOW)

    other_action = _request(action=PermissionAction.DELETE)
    with pytest.raises(ConfirmationInvalidError):
        provider.consume(record.confirmation_id, request=other_action, now=_NOW)


def test_consume_rejects_mismatched_principal() -> None:
    provider = InMemoryConfirmationProvider()
    request = _request(principal=_principal("ali"))
    record = provider.request(request, risk=RiskLevel.HIGH, now=_NOW)
    provider.decide(record.confirmation_id, approved=True, now=_NOW)

    impersonation_attempt = _request(principal=_principal("someone-else"))
    with pytest.raises(ConfirmationInvalidError):
        provider.consume(
            record.confirmation_id, request=impersonation_attempt, now=_NOW
        )


def test_consume_unknown_confirmation_raises() -> None:
    provider = InMemoryConfirmationProvider()
    with pytest.raises(ConfirmationNotFoundError):
        provider.consume("missing", request=_request(), now=_NOW)


def test_pending_confirmation_expires_and_cannot_be_decided() -> None:
    provider = InMemoryConfirmationProvider(ttl=timedelta(minutes=5))
    record = provider.request(_request(), risk=RiskLevel.HIGH, now=_NOW)
    later = _NOW + timedelta(minutes=6)

    with pytest.raises(ConfirmationInvalidError):
        provider.decide(record.confirmation_id, approved=True, now=later)

    assert provider.get(record.confirmation_id).status is ConfirmationStatus.EXPIRED  # type: ignore[union-attr]


def test_approved_confirmation_expires_before_consumption() -> None:
    """An approval sitting unused past its validity window cannot be spent."""

    provider = InMemoryConfirmationProvider(ttl=timedelta(minutes=5))
    request = _request()
    record = provider.request(request, risk=RiskLevel.HIGH, now=_NOW)
    provider.decide(record.confirmation_id, approved=True, now=_NOW)

    later = _NOW + timedelta(minutes=10)
    with pytest.raises(ConfirmationInvalidError):
        provider.consume(record.confirmation_id, request=request, now=later)


def test_expiration_boundary_is_inclusive() -> None:
    provider = InMemoryConfirmationProvider(ttl=timedelta(minutes=5))
    record = provider.request(_request(), risk=RiskLevel.HIGH, now=_NOW)
    exact_expiry = _NOW + timedelta(minutes=5)

    with pytest.raises(ConfirmationInvalidError):
        provider.decide(record.confirmation_id, approved=True, now=exact_expiry)


def test_get_does_not_mutate_state() -> None:
    provider = InMemoryConfirmationProvider()
    record = provider.request(_request(), risk=RiskLevel.HIGH, now=_NOW)

    fetched_first = provider.get(record.confirmation_id)
    fetched_second = provider.get(record.confirmation_id)

    assert fetched_first == fetched_second
    assert fetched_first.status is ConfirmationStatus.PENDING  # type: ignore[union-attr]


def test_two_provider_instances_are_isolated() -> None:
    provider_a = InMemoryConfirmationProvider()
    provider_b = InMemoryConfirmationProvider()

    record = provider_a.request(_request(), risk=RiskLevel.HIGH, now=_NOW)

    assert provider_a.get(record.confirmation_id) is not None
    assert provider_b.get(record.confirmation_id) is None
