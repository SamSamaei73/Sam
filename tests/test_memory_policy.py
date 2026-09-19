"""Tests for the deterministic Memory Policy."""

from datetime import UTC, datetime

import pytest

from sam.memory.models import (
    MemoryCandidate,
    MemoryConfidence,
    MemorySource,
    MemoryType,
    PolicyOutcome,
    PolicyReason,
)
from sam.memory.policy import MemoryPolicy
from sam.permissions.models import Principal, PrincipalKind

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _principal() -> Principal:
    return Principal(kind=PrincipalKind.USER, id="ali")


def _candidate(**overrides: object) -> MemoryCandidate:
    defaults: dict[str, object] = {
        "principal": _principal(),
        "memory_type": MemoryType.SEMANTIC,
        "content": "User prefers Farsi technical explanations.",
        "source": MemorySource.USER_EXPLICIT,
        "confidence": MemoryConfidence.EXPLICIT,
    }
    defaults.update(overrides)
    return MemoryCandidate.model_validate(defaults)


def test_explicit_user_memory_is_stored() -> None:
    decision = MemoryPolicy().decide(_candidate(), now=_NOW)
    assert decision.outcome is PolicyOutcome.STORE


def test_system_sourced_memory_is_stored() -> None:
    decision = MemoryPolicy().decide(
        _candidate(source=MemorySource.SYSTEM, content="Phase 3 was approved."),
        now=_NOW,
    )
    assert decision.outcome is PolicyOutcome.STORE


def test_ordinary_conversation_is_not_automatically_persisted() -> None:
    decision = MemoryPolicy().decide(
        _candidate(
            source=MemorySource.USER_CONVERSATION,
            confidence=MemoryConfidence.INFERRED,
            content="I think I might like blue, not sure.",
        ),
        now=_NOW,
    )
    assert decision.outcome is PolicyOutcome.REQUIRES_REVIEW
    assert decision.reason is PolicyReason.NOT_EXPLICITLY_CONFIRMED


def test_agent_inferred_memory_requires_review() -> None:
    """AGENT-generated memory never automatically becomes authoritative."""

    decision = MemoryPolicy().decide(
        _candidate(
            source=MemorySource.AGENT,
            confidence=MemoryConfidence.INFERRED,
            content="The user probably prefers dark mode based on context.",
        ),
        now=_NOW,
    )
    assert decision.outcome is PolicyOutcome.REQUIRES_REVIEW


def test_agent_source_even_with_explicit_confidence_requires_review() -> None:
    """Confidence alone cannot override source-based trust."""

    decision = MemoryPolicy().decide(
        _candidate(source=MemorySource.AGENT, confidence=MemoryConfidence.EXPLICIT),
        now=_NOW,
    )
    assert decision.outcome is PolicyOutcome.REQUIRES_REVIEW


@pytest.mark.parametrize("source", [MemorySource.TOOL, MemorySource.IMPORT])
def test_tool_and_import_sources_require_review(source: MemorySource) -> None:
    decision = MemoryPolicy().decide(_candidate(source=source), now=_NOW)
    assert decision.outcome is PolicyOutcome.REQUIRES_REVIEW


def test_secret_like_content_is_rejected_even_when_explicit() -> None:
    decision = MemoryPolicy().decide(
        _candidate(content="my API key is sk-ant-abcdefghijklmnop1234567890"),
        now=_NOW,
    )
    assert decision.outcome is PolicyOutcome.REJECT
    assert decision.reason is PolicyReason.SECRET_LIKE_CONTENT


def test_bearer_token_content_is_rejected() -> None:
    decision = MemoryPolicy().decide(
        _candidate(content="Authorization: Bearer abc123def456ghi789"), now=_NOW
    )
    assert decision.outcome is PolicyOutcome.REJECT
    assert decision.reason is PolicyReason.SECRET_LIKE_CONTENT


def test_private_key_block_is_rejected() -> None:
    decision = MemoryPolicy().decide(
        _candidate(content="-----BEGIN PRIVATE KEY-----\nMIIB..."), now=_NOW
    )
    assert decision.outcome is PolicyOutcome.REJECT
    assert decision.reason is PolicyReason.SECRET_LIKE_CONTENT


def test_trivially_short_content_is_rejected() -> None:
    decision = MemoryPolicy().decide(_candidate(content="ok"), now=_NOW)
    assert decision.outcome is PolicyOutcome.REJECT
    assert decision.reason is PolicyReason.CONTENT_TOO_SHORT


def test_working_type_candidate_is_rejected_by_policy() -> None:
    """Working memory never reaches this policy through normal use, but
    if one arrives it must be rejected, not silently stored."""

    candidate = _candidate(memory_type=MemoryType.WORKING, content="current task")
    decision = MemoryPolicy().decide(candidate, now=_NOW)
    assert decision.outcome is PolicyOutcome.REJECT
    assert decision.reason is PolicyReason.INVALID_CANDIDATE


def test_policy_decisions_are_deterministic() -> None:
    candidate = _candidate()
    policy = MemoryPolicy()
    first = policy.decide(candidate, now=_NOW)
    second = policy.decide(candidate, now=_NOW)
    assert first.outcome == second.outcome
    assert first.reason == second.reason


def test_policy_defaults_to_its_own_clock_when_now_not_given() -> None:
    decision = MemoryPolicy().decide(_candidate())
    assert decision.decided_at.tzinfo is not None
