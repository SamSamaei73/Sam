"""Tests for deterministic retrieval ranking."""

from datetime import UTC, datetime, timedelta

from sam.memory.models import (
    Memory,
    MemoryConfidence,
    MemoryImportance,
    MemorySource,
    MemoryStatus,
    MemoryType,
    RetrievalQuery,
)
from sam.memory.retrieval import rank
from sam.permissions.models import Principal, PrincipalKind

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _principal() -> Principal:
    return Principal(kind=PrincipalKind.USER, id="ali")


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


def _query(**overrides: object) -> RetrievalQuery:
    defaults: dict[str, object] = {"principal": _principal()}
    defaults.update(overrides)
    return RetrievalQuery.model_validate(defaults)


def test_exact_text_match_ranks_highest() -> None:
    exact = _memory(memory_id="exact", content="Sam uses FastAPI.")
    partial = _memory(memory_id="partial", content="Sam is a helpful assistant.")
    result = rank([partial, exact], _query(text="Sam uses FastAPI."), now=_NOW)

    assert result.items[0].memory.memory_id == "exact"
    assert result.items[0].score > 0.9


def test_normalized_match_ignores_case_and_whitespace() -> None:
    memory = _memory(content="Sam uses FastAPI.")
    result = rank([memory], _query(text="  sam   USES fastapi.  "), now=_NOW)

    assert result.items[0].score > 0.9


def test_token_overlap_ranks_partial_matches_above_unrelated() -> None:
    related = _memory(memory_id="related", content="Sam uses FastAPI for the backend.")
    unrelated = _memory(memory_id="unrelated", content="The weather today is sunny.")
    result = rank(
        [unrelated, related], _query(text="Sam uses FastAPI"), now=_NOW
    )

    assert [item.memory.memory_id for item in result.items] == ["related", "unrelated"]


def test_recency_breaks_ties_between_equal_text_relevance() -> None:
    older = _memory(memory_id="older", created_at=_NOW - timedelta(days=10))
    newer = _memory(memory_id="newer", created_at=_NOW)
    result = rank([older, newer], _query(), now=_NOW)

    assert result.items[0].memory.memory_id == "newer"


def test_importance_influences_ranking_without_text_query() -> None:
    low = _memory(
        memory_id="low",
        importance=MemoryImportance.LOW,
        created_at=_NOW - timedelta(days=1),
    )
    high = _memory(
        memory_id="high",
        importance=MemoryImportance.HIGH,
        created_at=_NOW - timedelta(days=1),
    )
    result = rank([low, high], _query(), now=_NOW)

    assert result.items[0].memory.memory_id == "high"


def test_type_filtering_is_the_stores_job_not_ranks() -> None:
    """rank() only scores/orders what it is given — filtering by type
    happens at the store layer (see MemoryEngine.retrieve)."""

    semantic = _memory(memory_id="s", memory_type=MemoryType.SEMANTIC)
    result = rank([semantic], _query(), now=_NOW)
    assert len(result.items) == 1


def test_tag_filtering_requires_overlap() -> None:
    tagged = _memory(memory_id="tagged", tags=("preferences",))
    untagged = _memory(memory_id="untagged", tags=())
    result = rank([tagged, untagged], _query(tags=("preferences",)), now=_NOW)

    assert [item.memory.memory_id for item in result.items] == ["tagged"]


def test_limit_is_enforced() -> None:
    memories = [_memory(memory_id=f"m{i}") for i in range(10)]
    result = rank(memories, _query(limit=3), now=_NOW)
    assert len(result.items) == 3


def test_archived_memories_are_excluded() -> None:
    archived = _memory(status=MemoryStatus.ARCHIVED)
    result = rank([archived], _query(), now=_NOW)
    assert result.items == ()


def test_expired_memories_are_excluded() -> None:
    expired = _memory(expires_at=_NOW - timedelta(seconds=1))
    result = rank([expired], _query(), now=_NOW)
    assert result.items == ()


def test_ordering_is_deterministic_across_repeated_calls() -> None:
    memories = [
        _memory(memory_id="a", created_at=_NOW),
        _memory(memory_id="b", created_at=_NOW),
        _memory(memory_id="c", created_at=_NOW),
    ]
    first = rank(memories, _query(), now=_NOW)
    second = rank(memories, _query(), now=_NOW)

    ids_first = [item.memory.memory_id for item in first.items]
    ids_second = [item.memory.memory_id for item in second.items]
    assert ids_first == ids_second == ["a", "b", "c"]  # stable tie-break by id


def test_no_matching_memories_returns_empty_result() -> None:
    result = rank([], _query(), now=_NOW)
    assert result.items == ()
