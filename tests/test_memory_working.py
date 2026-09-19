"""Tests for the in-memory working memory store."""

from datetime import UTC, datetime, timedelta

import pytest

from sam.memory.errors import WorkingMemoryFullError
from sam.memory.models import WorkingMemoryEntry
from sam.memory.working import InMemoryWorkingMemoryStore
from sam.permissions.models import Principal, PrincipalKind

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _principal(value: str = "ali") -> Principal:
    return Principal(kind=PrincipalKind.USER, id=value)


def _entry(**overrides: object) -> WorkingMemoryEntry:
    defaults: dict[str, object] = {
        "principal": _principal(),
        "key": "task",
        "content": "reviewing phase 4",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return WorkingMemoryEntry.model_validate(defaults)


def test_set_and_get() -> None:
    store = InMemoryWorkingMemoryStore()
    store.set(_entry(), now=_NOW)

    result = store.get(_principal(), "task", now=_NOW)

    assert result is not None
    assert result.content == "reviewing phase 4"


def test_get_missing_entry_returns_none() -> None:
    store = InMemoryWorkingMemoryStore()
    assert store.get(_principal(), "missing", now=_NOW) is None


def test_set_updates_existing_key() -> None:
    store = InMemoryWorkingMemoryStore()
    store.set(_entry(content="first"), now=_NOW)

    store.set(_entry(content="second"), now=_NOW)

    assert store.get(_principal(), "task", now=_NOW).content == "second"  # type: ignore[union-attr]


def test_clear_removes_every_entry_for_principal() -> None:
    store = InMemoryWorkingMemoryStore()
    store.set(_entry(key="a"), now=_NOW)
    store.set(_entry(key="b"), now=_NOW)

    store.clear(_principal())

    assert store.list(_principal(), now=_NOW) == ()


def test_delete_removes_one_entry() -> None:
    store = InMemoryWorkingMemoryStore()
    store.set(_entry(key="a"), now=_NOW)
    store.set(_entry(key="b"), now=_NOW)

    store.delete(_principal(), "a")

    remaining = {entry.key for entry in store.list(_principal(), now=_NOW)}
    assert remaining == {"b"}


def test_delete_missing_key_is_a_no_op() -> None:
    store = InMemoryWorkingMemoryStore()
    store.delete(_principal(), "missing")  # must not raise


def test_ttl_expired_entry_is_not_returned() -> None:
    store = InMemoryWorkingMemoryStore()
    entry = _entry(expires_at=_NOW + timedelta(seconds=5))
    store.set(entry, now=_NOW)

    later = _NOW + timedelta(seconds=10)
    assert store.get(_principal(), "task", now=later) is None


def test_ttl_boundary_is_inclusive_of_expiry() -> None:
    expires_at = _NOW + timedelta(seconds=5)
    store = InMemoryWorkingMemoryStore()
    store.set(_entry(expires_at=expires_at), now=_NOW)

    just_before = expires_at - timedelta(seconds=1)
    assert store.get(_principal(), "task", now=just_before) is not None
    assert store.get(_principal(), "task", now=expires_at) is None


def test_list_excludes_expired_entries() -> None:
    store = InMemoryWorkingMemoryStore()
    store.set(_entry(key="fresh"), now=_NOW)
    store.set(
        _entry(key="stale", expires_at=_NOW + timedelta(seconds=1)), now=_NOW
    )

    later = _NOW + timedelta(seconds=5)
    remaining = {entry.key for entry in store.list(_principal(), now=later)}

    assert remaining == {"fresh"}


def test_bounded_size_raises_when_exceeded() -> None:
    store = InMemoryWorkingMemoryStore(max_slots_per_principal=2)
    store.set(_entry(key="a"), now=_NOW)
    store.set(_entry(key="b"), now=_NOW)

    with pytest.raises(WorkingMemoryFullError):
        store.set(_entry(key="c"), now=_NOW)


def test_bounded_size_allows_updating_existing_key_when_full() -> None:
    store = InMemoryWorkingMemoryStore(max_slots_per_principal=2)
    store.set(_entry(key="a"), now=_NOW)
    store.set(_entry(key="b"), now=_NOW)

    store.set(_entry(key="a", content="updated"), now=_NOW)  # must not raise

    assert store.get(_principal(), "a", now=_NOW).content == "updated"  # type: ignore[union-attr]


def test_working_memory_is_isolated_per_principal() -> None:
    store = InMemoryWorkingMemoryStore()
    store.set(_entry(principal=_principal("ali")), now=_NOW)

    assert store.get(_principal("bob"), "task", now=_NOW) is None
    assert store.list(_principal("bob"), now=_NOW) == ()


def test_two_working_store_instances_are_isolated() -> None:
    store_a = InMemoryWorkingMemoryStore()
    store_b = InMemoryWorkingMemoryStore()

    store_a.set(_entry(), now=_NOW)

    assert store_a.get(_principal(), "task", now=_NOW) is not None
    assert store_b.get(_principal(), "task", now=_NOW) is None


def test_max_slots_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        InMemoryWorkingMemoryStore(max_slots_per_principal=0)
