"""Tests for the in-memory permission grant store."""

from datetime import UTC, datetime

import pytest

from sam.permissions.errors import DuplicateGrantError, GrantNotFoundError
from sam.permissions.models import (
    GrantStatus,
    PermissionAction,
    PermissionGrant,
    PermissionResource,
    PermissionScope,
    Principal,
    PrincipalKind,
)
from sam.permissions.store import InMemoryPermissionStore

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _principal(value: str = "ali") -> Principal:
    return Principal(kind=PrincipalKind.USER, id=value)


def _grant(**overrides: object) -> PermissionGrant:
    defaults: dict[str, object] = {
        "grant_id": "g1",
        "principal": _principal(),
        "resource": PermissionResource.FILESYSTEM,
        "action": PermissionAction.READ,
        "scope": PermissionScope.from_path("/Projects/Sam"),
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return PermissionGrant.model_validate(defaults)


def test_create_and_get_grant() -> None:
    store = InMemoryPermissionStore()
    store.create_grant(_grant())

    assert store.get_grant("g1") is not None
    assert store.get_grant("g1").grant_id == "g1"  # type: ignore[union-attr]


def test_get_missing_grant_returns_none() -> None:
    store = InMemoryPermissionStore()
    assert store.get_grant("missing") is None


def test_create_duplicate_grant_id_raises() -> None:
    store = InMemoryPermissionStore()
    store.create_grant(_grant())

    with pytest.raises(DuplicateGrantError):
        store.create_grant(_grant())


def test_list_grants_filters_by_principal() -> None:
    store = InMemoryPermissionStore()
    store.create_grant(_grant(grant_id="g1", principal=_principal("ali")))
    store.create_grant(_grant(grant_id="g2", principal=_principal("someone-else")))

    result = store.list_grants(_principal("ali"))

    assert [g.grant_id for g in result] == ["g1"]


def test_list_grants_filters_by_resource_and_action() -> None:
    store = InMemoryPermissionStore()
    store.create_grant(
        _grant(grant_id="g1", resource=PermissionResource.FILESYSTEM)
    )
    store.create_grant(_grant(grant_id="g2", resource=PermissionResource.GMAIL))

    result = store.list_grants(_principal(), resource=PermissionResource.GMAIL)

    assert [g.grant_id for g in result] == ["g2"]


def test_list_grants_includes_revoked_grants() -> None:
    """The store returns every status; filtering usability is the engine's job."""

    store = InMemoryPermissionStore()
    store.create_grant(_grant())
    store.revoke_grant("g1", now=_NOW)

    result = store.list_grants(_principal())

    assert len(result) == 1
    assert result[0].status is GrantStatus.REVOKED


def test_revoke_grant_sets_status_and_timestamps() -> None:
    store = InMemoryPermissionStore()
    store.create_grant(_grant())

    revoked = store.revoke_grant("g1", now=_NOW)

    assert revoked.status is GrantStatus.REVOKED
    assert revoked.revoked_at == _NOW
    assert revoked.updated_at == _NOW


def test_revoke_missing_grant_raises() -> None:
    store = InMemoryPermissionStore()
    with pytest.raises(GrantNotFoundError):
        store.revoke_grant("missing", now=_NOW)


def test_revoke_preserves_the_grant_for_audit_history() -> None:
    """Revocation replaces the record; it never deletes it."""

    store = InMemoryPermissionStore()
    store.create_grant(_grant())

    store.revoke_grant("g1", now=_NOW)

    assert store.get_grant("g1") is not None


def test_two_store_instances_are_fully_isolated() -> None:
    """No global mutable state: separate instances never share grants."""

    store_a = InMemoryPermissionStore()
    store_b = InMemoryPermissionStore()

    store_a.create_grant(_grant())

    assert store_a.get_grant("g1") is not None
    assert store_b.get_grant("g1") is None
    assert store_b.list_grants(_principal()) == ()
