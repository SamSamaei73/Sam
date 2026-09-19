"""Tests for the in-memory long-term memory store."""

from datetime import UTC, datetime, timedelta

import pytest

from sam.memory.errors import DuplicateMemoryError, MemoryNotFoundError
from sam.memory.models import Memory, MemoryConfidence, MemorySource, MemoryType
from sam.memory.store import InMemoryMemoryStore
from sam.permissions.models import Principal, PrincipalKind

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _principal(value: str = "ali") -> Principal:
    return Principal(kind=PrincipalKind.USER, id=value)


def _memory(**overrides: object) -> Memory:
    defaults: dict[str, object] = {
        "memory_id": "m1",
        "memory_type": MemoryType.SEMANTIC,
        "principal": _principal(),
        "content": "User prefers Farsi technical explanations.",
        "source": MemorySource.USER_EXPLICIT,
        "confidence": MemoryConfidence.EXPLICIT,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return Memory.model_validate(defaults)


def test_save_and_get() -> None:
    store = InMemoryMemoryStore()
    store.save(_memory())

    result = store.get("m1", principal=_principal())

    assert result is not None
    assert result.memory_id == "m1"


def test_get_missing_memory_returns_none() -> None:
    store = InMemoryMemoryStore()
    assert store.get("missing", principal=_principal()) is None


def test_get_by_wrong_principal_returns_none_not_error() -> None:
    """A lookup can never distinguish "doesn't exist" from "not yours"."""

    store = InMemoryMemoryStore()
    store.save(_memory(principal=_principal("ali")))

    result = store.get("m1", principal=_principal("bob"))

    assert result is None


def test_save_duplicate_id_raises() -> None:
    store = InMemoryMemoryStore()
    store.save(_memory())

    with pytest.raises(DuplicateMemoryError):
        store.save(_memory())


def test_replace_updates_the_record() -> None:
    store = InMemoryMemoryStore()
    store.save(_memory())
    updated = _memory(content="Updated fact.", updated_at=_NOW + timedelta(hours=1))

    result = store.replace(updated, principal=_principal())

    assert result.content == "Updated fact."
    assert store.get("m1", principal=_principal()).content == "Updated fact."  # type: ignore[union-attr]


def test_replace_missing_memory_raises() -> None:
    store = InMemoryMemoryStore()
    with pytest.raises(MemoryNotFoundError):
        store.replace(_memory(), principal=_principal())


def test_replace_by_wrong_principal_raises() -> None:
    store = InMemoryMemoryStore()
    store.save(_memory(principal=_principal("ali")))

    with pytest.raises(MemoryNotFoundError):
        store.replace(_memory(principal=_principal("bob")), principal=_principal("bob"))


def test_replace_cannot_reassign_owner() -> None:
    """A defensive backstop: even if a caller tries to change the owner
    via the replacement object, the store refuses."""

    store = InMemoryMemoryStore()
    store.save(_memory(principal=_principal("ali")))

    with pytest.raises(MemoryNotFoundError):
        store.replace(_memory(principal=_principal("bob")), principal=_principal("ali"))


def test_delete_removes_the_record_permanently() -> None:
    store = InMemoryMemoryStore()
    store.save(_memory())

    store.delete("m1", principal=_principal())

    assert store.get("m1", principal=_principal()) is None


def test_delete_missing_memory_raises() -> None:
    store = InMemoryMemoryStore()
    with pytest.raises(MemoryNotFoundError):
        store.delete("missing", principal=_principal())


def test_delete_by_wrong_principal_raises_and_does_not_delete() -> None:
    store = InMemoryMemoryStore()
    store.save(_memory(principal=_principal("ali")))

    with pytest.raises(MemoryNotFoundError):
        store.delete("m1", principal=_principal("bob"))

    assert store.get("m1", principal=_principal("ali")) is not None


def test_list_candidates_filters_by_principal() -> None:
    store = InMemoryMemoryStore()
    store.save(_memory(memory_id="m1", principal=_principal("ali")))
    store.save(_memory(memory_id="m2", principal=_principal("bob")))

    result = store.list_candidates(principal=_principal("ali"))

    assert [m.memory_id for m in result] == ["m1"]


def test_list_candidates_filters_by_type() -> None:
    store = InMemoryMemoryStore()
    store.save(_memory(memory_id="m1", memory_type=MemoryType.SEMANTIC))
    store.save(_memory(memory_id="m2", memory_type=MemoryType.EPISODIC))

    result = store.list_candidates(
        principal=_principal(), memory_types=(MemoryType.EPISODIC,)
    )

    assert [m.memory_id for m in result] == ["m2"]


def test_list_candidates_filters_by_project_id() -> None:
    store = InMemoryMemoryStore()
    store.save(
        _memory(
            memory_id="m1",
            memory_type=MemoryType.PROJECT,
            project_id="sam-core",
        )
    )
    store.save(
        _memory(
            memory_id="m2",
            memory_type=MemoryType.PROJECT,
            project_id="other-project",
        )
    )

    result = store.list_candidates(principal=_principal(), project_id="sam-core")

    assert [m.memory_id for m in result] == ["m1"]


def test_empty_store_returns_no_candidates() -> None:
    store = InMemoryMemoryStore()
    assert store.list_candidates(principal=_principal()) == ()


def test_two_store_instances_are_fully_isolated() -> None:
    store_a = InMemoryMemoryStore()
    store_b = InMemoryMemoryStore()

    store_a.save(_memory())

    assert store_a.get("m1", principal=_principal()) is not None
    assert store_b.get("m1", principal=_principal()) is None
    assert store_b.list_candidates(principal=_principal()) == ()
