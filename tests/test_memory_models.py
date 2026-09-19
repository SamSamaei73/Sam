"""Tests for memory domain models: validation, bounds, immutability."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sam.memory.models import (
    Memory,
    MemoryCandidate,
    MemoryConfidence,
    MemoryImportance,
    MemorySource,
    MemoryStatus,
    MemoryType,
    PolicyDecision,
    PolicyOutcome,
    PolicyReason,
    RetrievalQuery,
    WorkingMemoryEntry,
    normalize_for_comparison,
)
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


def _candidate(**overrides: object) -> MemoryCandidate:
    defaults: dict[str, object] = {
        "principal": _principal(),
        "memory_type": MemoryType.SEMANTIC,
        "content": "Sam uses FastAPI.",
        "source": MemorySource.USER_EXPLICIT,
        "confidence": MemoryConfidence.EXPLICIT,
    }
    defaults.update(overrides)
    return MemoryCandidate.model_validate(defaults)


# --------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------- #


def test_valid_memory_constructs() -> None:
    memory = _memory()
    assert memory.status is MemoryStatus.ACTIVE
    assert memory.importance is MemoryImportance.NORMAL


def test_memory_rejects_unknown_type_string() -> None:
    with pytest.raises(ValidationError):
        Memory.model_validate(
            {
                "memory_id": "m1",
                "memory_type": "fictional",
                "principal": {"kind": "user", "id": "ali"},
                "content": "x",
                "source": "user_explicit",
                "confidence": "explicit",
                "created_at": _NOW,
                "updated_at": _NOW,
            }
        )


def test_memory_rejects_naive_timestamps() -> None:
    with pytest.raises(ValidationError):
        _memory(created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1))


def test_memory_rejects_content_over_length_limit() -> None:
    with pytest.raises(ValidationError):
        _memory(content="x" * 2_001)


def test_memory_rejects_blank_content() -> None:
    with pytest.raises(ValidationError):
        _memory(content="   ")


def test_memory_rejects_too_many_tags() -> None:
    with pytest.raises(ValidationError):
        _memory(tags=tuple(f"tag{i}" for i in range(11)))


def test_memory_rejects_oversized_tag() -> None:
    with pytest.raises(ValidationError):
        _memory(tags=("x" * 41,))


def test_memory_rejects_too_much_metadata() -> None:
    with pytest.raises(ValidationError):
        _memory(metadata={f"k{i}": "v" for i in range(21)})


def test_memory_rejects_oversized_metadata_value() -> None:
    with pytest.raises(ValidationError):
        _memory(metadata={"k": "v" * 201})


def test_memory_rejects_non_string_metadata_values() -> None:
    """Arbitrary objects can never enter metadata — dict[str, str] only."""

    with pytest.raises(ValidationError):
        Memory.model_validate(
            {
                "memory_id": "m1",
                "memory_type": "semantic",
                "principal": {"kind": "user", "id": "ali"},
                "content": "x",
                "source": "user_explicit",
                "confidence": "explicit",
                "created_at": _NOW,
                "updated_at": _NOW,
                "metadata": {"k": {"nested": "object"}},
            }
        )


def test_memory_rejects_invalid_project_id_length() -> None:
    with pytest.raises(ValidationError):
        _memory(project_id="p" * 101)


def test_project_memory_requires_project_id() -> None:
    with pytest.raises(ValidationError):
        _memory(memory_type=MemoryType.PROJECT, project_id=None)


def test_project_memory_with_project_id_succeeds() -> None:
    memory = _memory(memory_type=MemoryType.PROJECT, project_id="sam-core")
    assert memory.project_id == "sam-core"


def test_memory_rejects_working_type() -> None:
    """Working memory is never a Memory record — see WorkingMemoryEntry."""

    with pytest.raises(ValidationError):
        _memory(memory_type=MemoryType.WORKING)


def test_memory_is_frozen() -> None:
    memory = _memory()
    with pytest.raises(ValidationError):
        memory.content = "changed"


def test_memory_is_usable_false_when_archived() -> None:
    memory = _memory(status=MemoryStatus.ARCHIVED)
    assert not memory.is_usable(now=_NOW)


def test_memory_is_usable_false_when_expired() -> None:
    memory = _memory(expires_at=_NOW)
    assert not memory.is_usable(now=_NOW)
    from datetime import timedelta

    assert memory.is_usable(now=_NOW - timedelta(seconds=1))


def test_memory_content_control_characters_are_stripped() -> None:
    memory = _memory(content="hello\x00world")
    assert "\x00" not in memory.content


# --------------------------------------------------------------------- #
# MemoryCandidate
# --------------------------------------------------------------------- #


def test_candidate_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryCandidate.model_validate(
            {
                "principal": {"kind": "user", "id": "ali"},
                "memory_type": "semantic",
                "content": "x",
                "source": "user_explicit",
                "confidence": "explicit",
                "unexpected": "field",
            }
        )


def test_candidate_project_type_requires_project_id() -> None:
    with pytest.raises(ValidationError):
        _candidate(memory_type=MemoryType.PROJECT)


def test_candidate_is_frozen() -> None:
    candidate = _candidate()
    with pytest.raises(ValidationError):
        candidate.content = "changed"


def test_candidate_normalized_content() -> None:
    candidate = _candidate(content="  Sam   USES FastAPI.  ")
    assert candidate.normalized_content() == "sam uses fastapi."


def test_candidate_reason_is_sanitized_and_bounded() -> None:
    candidate = _candidate(reason="x" * 1000)
    assert candidate.reason is not None
    assert len(candidate.reason) <= 500


# --------------------------------------------------------------------- #
# normalize_for_comparison
# --------------------------------------------------------------------- #


def test_normalize_collapses_whitespace_and_case() -> None:
    assert normalize_for_comparison("  Hello   WORLD  ") == "hello world"


def test_normalize_strips_control_characters() -> None:
    assert normalize_for_comparison("hi\x00there") == "hithere"


# --------------------------------------------------------------------- #
# PolicyDecision shape invariants
# --------------------------------------------------------------------- #


def test_store_decision_must_not_carry_a_reason() -> None:
    with pytest.raises(ValidationError):
        PolicyDecision(
            outcome=PolicyOutcome.STORE,
            candidate=_candidate(),
            decided_at=_NOW,
            reason=PolicyReason.CONTENT_TOO_SHORT,
        )


def test_reject_decision_requires_a_reason() -> None:
    with pytest.raises(ValidationError):
        PolicyDecision(
            outcome=PolicyOutcome.REJECT, candidate=_candidate(), decided_at=_NOW
        )


def test_decision_is_frozen() -> None:
    decision = PolicyDecision(
        outcome=PolicyOutcome.STORE, candidate=_candidate(), decided_at=_NOW
    )
    with pytest.raises(ValidationError):
        decision.outcome = PolicyOutcome.REJECT


# --------------------------------------------------------------------- #
# WorkingMemoryEntry
# --------------------------------------------------------------------- #


def test_working_entry_rejects_blank_key() -> None:
    with pytest.raises(ValidationError):
        WorkingMemoryEntry(
            principal=_principal(),
            key="   ",
            content="x",
            created_at=_NOW,
            updated_at=_NOW,
        )


def test_working_entry_rejects_naive_timestamps() -> None:
    with pytest.raises(ValidationError):
        WorkingMemoryEntry(
            principal=_principal(),
            key="k",
            content="x",
            created_at=datetime(2026, 1, 1),
            updated_at=datetime(2026, 1, 1),
        )


def test_working_entry_is_frozen() -> None:
    entry = WorkingMemoryEntry(
        principal=_principal(), key="k", content="x", created_at=_NOW, updated_at=_NOW
    )
    with pytest.raises(ValidationError):
        entry.content = "changed"


# --------------------------------------------------------------------- #
# RetrievalQuery
# --------------------------------------------------------------------- #


def test_retrieval_query_requires_principal() -> None:
    with pytest.raises(ValidationError):
        RetrievalQuery.model_validate({"limit": 10})


def test_retrieval_query_rejects_working_type() -> None:
    with pytest.raises(ValidationError):
        RetrievalQuery(principal=_principal(), memory_types=(MemoryType.WORKING,))


def test_retrieval_query_rejects_project_only_without_project_id() -> None:
    with pytest.raises(ValidationError):
        RetrievalQuery(principal=_principal(), memory_types=(MemoryType.PROJECT,))


def test_retrieval_query_allows_project_only_with_project_id() -> None:
    query = RetrievalQuery(
        principal=_principal(),
        memory_types=(MemoryType.PROJECT,),
        project_id="sam-core",
    )
    assert query.project_id == "sam-core"


def test_retrieval_query_rejects_zero_or_negative_limit() -> None:
    with pytest.raises(ValidationError):
        RetrievalQuery(principal=_principal(), limit=0)
    with pytest.raises(ValidationError):
        RetrievalQuery(principal=_principal(), limit=-1)


def test_retrieval_query_rejects_excessive_limit() -> None:
    with pytest.raises(ValidationError):
        RetrievalQuery(principal=_principal(), limit=100_000)


def test_retrieval_query_is_frozen() -> None:
    query = RetrievalQuery(principal=_principal())
    with pytest.raises(ValidationError):
        query.limit = 5
