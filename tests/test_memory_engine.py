"""Security-focused integration tests for MemoryEngine.

Organized to match the required matrix: remember/policy, get/update/delete
ownership boundaries, retrieval isolation, working memory, audit safety,
fail-closed behavior, isolation, and adversarial probes.
"""

import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from sam.memory.audit import FailingMemoryAuditSink, InMemoryMemoryAuditSink
from sam.memory.engine import MemoryEngine, PermissionChecker
from sam.memory.errors import MemoryNotFoundError, MemoryPermissionDeniedError
from sam.memory.models import (
    Memory,
    MemoryAuditOutcome,
    MemoryCandidate,
    MemoryConfidence,
    MemoryOperation,
    MemorySource,
    MemoryStatus,
    MemoryType,
    PolicyOutcome,
    PolicyReason,
    RetrievalQuery,
)
from sam.memory.store import InMemoryMemoryStore, MemoryStore
from sam.memory.working import InMemoryWorkingMemoryStore
from sam.permissions.models import Principal, PrincipalKind

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _principal(value: str = "ali") -> Principal:
    return Principal(kind=PrincipalKind.USER, id=value)


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


class _Harness:
    """A fully wired, isolated engine plus its collaborators for one test."""

    def __init__(
        self,
        *,
        clock: datetime = _T0,
        permission_checker: PermissionChecker | None = None,
    ) -> None:
        self.store = InMemoryMemoryStore()
        self.working = InMemoryWorkingMemoryStore()
        self.audit = InMemoryMemoryAuditSink()
        self._clock_value = clock
        self.engine = MemoryEngine(
            store=self.store,
            working_store=self.working,
            audit_sink=self.audit,
            permission_checker=permission_checker,
            clock=lambda: self._clock_value,
        )

    def advance(self, delta: timedelta) -> None:
        self._clock_value = self._clock_value + delta


# --------------------------------------------------------------------- #
# remember() / policy integration
# --------------------------------------------------------------------- #


def test_explicit_candidate_is_stored() -> None:
    h = _Harness()
    outcome = h.engine.remember(_candidate())
    assert outcome.decision.outcome is PolicyOutcome.STORE
    assert outcome.memory is not None


def test_conversation_candidate_is_not_stored() -> None:
    h = _Harness()
    outcome = h.engine.remember(
        _candidate(
            source=MemorySource.USER_CONVERSATION,
            confidence=MemoryConfidence.INFERRED,
        )
    )
    assert outcome.decision.outcome is PolicyOutcome.REQUIRES_REVIEW
    assert outcome.memory is None
    assert h.store.list_candidates(principal=_principal()) == ()


def test_secret_like_candidate_is_never_stored() -> None:
    h = _Harness()
    outcome = h.engine.remember(
        _candidate(content="my API key is sk-ant-abcdefghijklmnop1234567890")
    )
    assert outcome.decision.outcome is PolicyOutcome.REJECT
    assert outcome.memory is None
    assert h.store.list_candidates(principal=_principal()) == ()


def test_exact_duplicate_touches_existing_memory_instead_of_creating_new() -> None:
    h = _Harness()
    first = h.engine.remember(_candidate())
    second = h.engine.remember(
        _candidate(content="  user PREFERS farsi technical explanations.  ")
    )

    assert second.was_duplicate is True
    assert second.memory.memory_id == first.memory.memory_id  # type: ignore[union-attr]
    assert len(h.store.list_candidates(principal=_principal())) == 1


def test_near_duplicate_is_not_merged() -> None:
    """Only exact normalized duplicates merge — no fuzzy matching."""

    h = _Harness()
    h.engine.remember(_candidate(content="User prefers Farsi explanations."))
    h.engine.remember(_candidate(content="User prefers Farsi technical writing."))

    assert len(h.store.list_candidates(principal=_principal())) == 2


def test_llm_cannot_permanently_store_by_merely_proposing() -> None:
    """The core Phase 4 guarantee: proposing is not the same as persisting."""

    h = _Harness()
    outcome = h.engine.remember(
        _candidate(
            source=MemorySource.AGENT,
            confidence=MemoryConfidence.INFERRED,
            content="I have decided this is an important permanent fact.",
        )
    )
    assert outcome.memory is None
    assert h.store.list_candidates(principal=_principal()) == ()


# --------------------------------------------------------------------- #
# get() — ownership boundary
# --------------------------------------------------------------------- #


def test_get_returns_own_memory() -> None:
    h = _Harness()
    outcome = h.engine.remember(_candidate())
    fetched = h.engine.get(outcome.memory.memory_id, principal=_principal())  # type: ignore[union-attr]
    assert fetched is not None


def test_get_returns_none_for_another_principals_memory() -> None:
    h = _Harness()
    outcome = h.engine.remember(_candidate(principal=_principal("ali")))
    fetched = h.engine.get(outcome.memory.memory_id, principal=_principal("bob"))  # type: ignore[union-attr]
    assert fetched is None


def test_get_returns_none_for_missing_id() -> None:
    h = _Harness()
    assert h.engine.get("missing", principal=_principal()) is None


def test_get_returns_none_for_expired_memory() -> None:
    h = _Harness()
    outcome = h.engine.remember(
        _candidate(source=MemorySource.SYSTEM, expires_at=_T0 + timedelta(hours=1))
    )
    h.advance(timedelta(hours=2))
    assert h.engine.get(outcome.memory.memory_id, principal=_principal()) is None  # type: ignore[union-attr]


def test_get_touches_last_accessed_at() -> None:
    h = _Harness()
    outcome = h.engine.remember(_candidate())
    assert outcome.memory.last_accessed_at is None  # type: ignore[union-attr]

    h.advance(timedelta(minutes=5))
    fetched = h.engine.get(outcome.memory.memory_id, principal=_principal())  # type: ignore[union-attr]

    assert fetched is not None
    assert fetched.last_accessed_at == _T0 + timedelta(minutes=5)


def test_deleted_memory_cannot_be_retrieved() -> None:
    h = _Harness()
    outcome = h.engine.remember(_candidate())
    h.engine.delete(outcome.memory.memory_id, principal=_principal())  # type: ignore[union-attr]
    assert h.engine.get(outcome.memory.memory_id, principal=_principal()) is None  # type: ignore[union-attr]


# --------------------------------------------------------------------- #
# update() — ownership boundary and re-validation
# --------------------------------------------------------------------- #


def test_update_changes_content() -> None:
    h = _Harness()
    outcome = h.engine.remember(_candidate())
    updated = h.engine.update(
        outcome.memory.memory_id,  # type: ignore[union-attr]
        principal=_principal(),
        content="User prefers English explanations now.",
    )
    assert updated.content == "User prefers English explanations now."


def test_update_missing_memory_raises() -> None:
    h = _Harness()
    with pytest.raises(MemoryNotFoundError):
        h.engine.update("missing", principal=_principal(), content="x")


def test_cannot_update_another_principals_memory() -> None:
    h = _Harness()
    outcome = h.engine.remember(_candidate(principal=_principal("ali")))
    with pytest.raises(MemoryNotFoundError):
        h.engine.update(
            outcome.memory.memory_id,  # type: ignore[union-attr]
            principal=_principal("bob"),
            content="hijacked",
        )


def test_update_rejects_oversized_content() -> None:
    """update() re-validates through the full model, never bypasses bounds."""

    h = _Harness()
    outcome = h.engine.remember(_candidate())
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        h.engine.update(
            outcome.memory.memory_id, principal=_principal(), content="x" * 2001  # type: ignore[union-attr]
        )


def test_update_can_archive_a_memory() -> None:
    h = _Harness()
    outcome = h.engine.remember(_candidate())
    updated = h.engine.update(
        outcome.memory.memory_id, principal=_principal(), status=MemoryStatus.ARCHIVED  # type: ignore[union-attr]
    )
    assert updated.status is MemoryStatus.ARCHIVED
    assert h.engine.get(outcome.memory.memory_id, principal=_principal()) is None  # type: ignore[union-attr]


# --------------------------------------------------------------------- #
# delete() — ownership + optional PermissionChecker
# --------------------------------------------------------------------- #


def test_delete_removes_the_memory() -> None:
    h = _Harness()
    outcome = h.engine.remember(_candidate())
    h.engine.delete(outcome.memory.memory_id, principal=_principal())  # type: ignore[union-attr]
    assert h.store.get(outcome.memory.memory_id, principal=_principal()) is None  # type: ignore[union-attr]


def test_delete_missing_memory_raises() -> None:
    h = _Harness()
    with pytest.raises(MemoryNotFoundError):
        h.engine.delete("missing", principal=_principal())


def test_cannot_delete_another_principals_memory() -> None:
    h = _Harness()
    outcome = h.engine.remember(_candidate(principal=_principal("ali")))
    memory_id = outcome.memory.memory_id  # type: ignore[union-attr]
    with pytest.raises(MemoryNotFoundError):
        h.engine.delete(memory_id, principal=_principal("bob"))
    assert h.store.get(memory_id, principal=_principal("ali")) is not None


class _DenyingChecker:
    def check(self, *, principal: Principal, operation: str, memory: Memory) -> bool:
        return False


class _RaisingChecker:
    def check(self, *, principal: Principal, operation: str, memory: Memory) -> bool:
        raise RuntimeError("checker backend unavailable")


class _AllowingChecker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def check(self, *, principal: Principal, operation: str, memory: Memory) -> bool:
        self.calls.append(operation)
        return True


def test_delete_denied_by_permission_checker() -> None:
    h = _Harness(permission_checker=_DenyingChecker())
    outcome = h.engine.remember(_candidate())
    with pytest.raises(MemoryPermissionDeniedError):
        h.engine.delete(outcome.memory.memory_id, principal=_principal())  # type: ignore[union-attr]
    assert h.store.get(outcome.memory.memory_id, principal=_principal()) is not None  # type: ignore[union-attr]


def test_delete_fails_closed_when_checker_raises() -> None:
    h = _Harness(permission_checker=_RaisingChecker())
    outcome = h.engine.remember(_candidate())
    with pytest.raises(MemoryPermissionDeniedError):
        h.engine.delete(outcome.memory.memory_id, principal=_principal())  # type: ignore[union-attr]
    assert h.store.get(outcome.memory.memory_id, principal=_principal()) is not None  # type: ignore[union-attr]


def test_delete_allowed_by_permission_checker_and_checker_is_consulted() -> None:
    checker = _AllowingChecker()
    h = _Harness(permission_checker=checker)
    outcome = h.engine.remember(_candidate())
    h.engine.delete(outcome.memory.memory_id, principal=_principal())  # type: ignore[union-attr]
    assert checker.calls == ["memory:delete"]


def test_delete_without_a_configured_checker_relies_on_ownership_alone() -> None:
    h = _Harness(permission_checker=None)
    outcome = h.engine.remember(_candidate())
    h.engine.delete(outcome.memory.memory_id, principal=_principal())  # type: ignore[union-attr]
    assert h.store.get(outcome.memory.memory_id, principal=_principal()) is None  # type: ignore[union-attr]


# --------------------------------------------------------------------- #
# retrieve() — principal and project isolation
# --------------------------------------------------------------------- #


def test_retrieve_only_returns_own_memories() -> None:
    h = _Harness()
    h.engine.remember(_candidate(principal=_principal("ali")))
    h.engine.remember(_candidate(principal=_principal("bob")))

    result = h.engine.retrieve(RetrievalQuery(principal=_principal("ali")))

    assert len(result.items) == 1
    assert result.items[0].memory.principal.id == "ali"


def test_retrieve_project_a_never_returns_project_b() -> None:
    h = _Harness()
    h.engine.remember(
        _candidate(
            memory_type=MemoryType.PROJECT,
            project_id="project-a",
            source=MemorySource.SYSTEM,
        )
    )
    h.engine.remember(
        _candidate(
            memory_type=MemoryType.PROJECT,
            project_id="project-b",
            source=MemorySource.SYSTEM,
            content="Project B uses a different stack entirely.",
        )
    )

    result = h.engine.retrieve(
        RetrievalQuery(
            principal=_principal(),
            memory_types=(MemoryType.PROJECT,),
            project_id="project-a",
        )
    )

    assert len(result.items) == 1
    assert result.items[0].memory.project_id == "project-a"


def test_unscoped_retrieval_excludes_project_memories() -> None:
    h = _Harness()
    h.engine.remember(_candidate())  # semantic
    h.engine.remember(
        _candidate(
            memory_type=MemoryType.PROJECT,
            project_id="project-a",
            source=MemorySource.SYSTEM,
        )
    )

    result = h.engine.retrieve(RetrievalQuery(principal=_principal()))

    assert all(item.memory.memory_type != MemoryType.PROJECT for item in result.items)


def test_retrieve_respects_limit() -> None:
    h = _Harness()
    for i in range(5):
        h.engine.remember(
            _candidate(
                content=f"Distinct fact number {i} about the user preference set."
            )
        )

    result = h.engine.retrieve(RetrievalQuery(principal=_principal(), limit=2))

    assert len(result.items) == 2


def test_retrieve_excludes_expired_memories() -> None:
    h = _Harness()
    outcome = h.engine.remember(
        _candidate(source=MemorySource.SYSTEM, expires_at=_T0 + timedelta(hours=1))
    )
    h.advance(timedelta(hours=2))

    result = h.engine.retrieve(RetrievalQuery(principal=_principal()))

    ids = [item.memory.memory_id for item in result.items]
    assert outcome.memory.memory_id not in ids  # type: ignore[union-attr]


# --------------------------------------------------------------------- #
# Working memory via the engine
# --------------------------------------------------------------------- #


def test_remember_working_and_recall() -> None:
    h = _Harness()
    h.engine.remember_working(principal=_principal(), key="task", content="reviewing")
    entry = h.engine.recall_working(principal=_principal(), key="task")
    assert entry is not None
    assert entry.content == "reviewing"


def test_working_memory_never_appears_in_long_term_retrieval() -> None:
    h = _Harness()
    h.engine.remember_working(
        principal=_principal(), key="task", content="unique working content xyz"
    )
    result = h.engine.retrieve(RetrievalQuery(principal=_principal(), text="unique"))
    assert result.items == ()


def test_clear_working_removes_everything_for_principal() -> None:
    h = _Harness()
    h.engine.remember_working(principal=_principal(), key="a", content="x")
    h.engine.remember_working(principal=_principal(), key="b", content="y")

    h.engine.clear_working(principal=_principal())

    assert h.engine.list_working(principal=_principal()) == ()


def test_working_memory_is_isolated_per_principal() -> None:
    h = _Harness()
    h.engine.remember_working(principal=_principal("ali"), key="task", content="x")
    assert h.engine.recall_working(principal=_principal("bob"), key="task") is None


def test_forget_working_removes_one_key() -> None:
    h = _Harness()
    h.engine.remember_working(principal=_principal(), key="task", content="x")
    h.engine.forget_working(principal=_principal(), key="task")
    assert h.engine.recall_working(principal=_principal(), key="task") is None


# --------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------- #


def test_remember_generates_an_audit_event() -> None:
    h = _Harness()
    h.engine.remember(_candidate())
    events = [
        e for e in h.audit.list_events() if e.operation is MemoryOperation.REMEMBER
    ]
    assert len(events) == 1
    assert events[0].outcome is MemoryAuditOutcome.SUCCESS


def test_rejected_remember_is_audited_as_not_stored() -> None:
    h = _Harness()
    h.engine.remember(_candidate(content="my password is hunter2plus"))
    events = [
        e for e in h.audit.list_events() if e.operation is MemoryOperation.REMEMBER
    ]
    assert events[0].outcome is MemoryAuditOutcome.NOT_STORED
    assert events[0].policy_reason is PolicyReason.SECRET_LIKE_CONTENT


def test_audit_event_never_contains_memory_content() -> None:
    h = _Harness()
    secret_bearing = "my API key is sk-ant-abcdefghijklmnop1234567890"
    h.engine.remember(_candidate(content=secret_bearing))

    for event in h.audit.list_events():
        serialized = event.model_dump_json()
        assert "sk-ant" not in serialized
        assert secret_bearing not in serialized


def test_audit_failure_does_not_change_the_returned_outcome() -> None:
    h = _Harness()
    engine = MemoryEngine(
        store=h.store,
        working_store=h.working,
        audit_sink=FailingMemoryAuditSink(),
        clock=lambda: _T0,
    )
    outcome = engine.remember(_candidate())
    assert outcome.decision.outcome is PolicyOutcome.STORE
    assert outcome.memory is not None


def test_engine_works_without_an_audit_sink_configured() -> None:
    store = InMemoryMemoryStore()
    working = InMemoryWorkingMemoryStore()
    engine = MemoryEngine(store=store, working_store=working, clock=lambda: _T0)
    outcome = engine.remember(_candidate())
    assert outcome.memory is not None


# --------------------------------------------------------------------- #
# Fail-closed behavior
# --------------------------------------------------------------------- #


class _RaisingStore:
    def save(self, memory: Memory) -> Memory:
        raise RuntimeError("store unavailable")

    def get(self, memory_id: str, *, principal: Principal) -> Memory | None:
        raise RuntimeError("store unavailable")

    def replace(self, memory: Memory, *, principal: Principal) -> Memory:
        raise RuntimeError("store unavailable")

    def delete(self, memory_id: str, *, principal: Principal) -> None:
        raise RuntimeError("store unavailable")

    def list_candidates(
        self,
        *,
        principal: Principal,
        memory_types: Sequence[MemoryType] | None = None,
        project_id: str | None = None,
    ) -> Sequence[Memory]:
        raise RuntimeError("store unavailable")


def test_remember_fails_closed_when_store_is_unavailable() -> None:
    store: MemoryStore = _RaisingStore()
    engine = MemoryEngine(
        store=store, working_store=InMemoryWorkingMemoryStore(), clock=lambda: _T0
    )
    outcome = engine.remember(_candidate())
    assert outcome.decision.outcome is PolicyOutcome.REJECT
    assert outcome.memory is None


def test_get_fails_closed_when_store_is_unavailable() -> None:
    store: MemoryStore = _RaisingStore()
    engine = MemoryEngine(
        store=store, working_store=InMemoryWorkingMemoryStore(), clock=lambda: _T0
    )
    assert engine.get("m1", principal=_principal()) is None


def test_retrieve_fails_closed_when_store_is_unavailable() -> None:
    store: MemoryStore = _RaisingStore()
    engine = MemoryEngine(
        store=store, working_store=InMemoryWorkingMemoryStore(), clock=lambda: _T0
    )
    result = engine.retrieve(RetrievalQuery(principal=_principal()))
    assert result.items == ()


def test_no_secret_leaks_via_exception_details_on_store_failure() -> None:
    store: MemoryStore = _RaisingStore()
    engine = MemoryEngine(
        store=store, working_store=InMemoryWorkingMemoryStore(), clock=lambda: _T0
    )
    outcome = engine.remember(_candidate())
    serialized = outcome.model_dump_json()
    assert "store unavailable" not in serialized
    assert "RuntimeError" not in serialized


# --------------------------------------------------------------------- #
# Isolation between engine instances
# --------------------------------------------------------------------- #


def test_two_independent_engines_do_not_share_state() -> None:
    h1 = _Harness()
    h2 = _Harness()
    outcome = h1.engine.remember(_candidate())

    assert h1.engine.get(outcome.memory.memory_id, principal=_principal()) is not None  # type: ignore[union-attr]
    assert h2.engine.get(outcome.memory.memory_id, principal=_principal()) is None  # type: ignore[union-attr]


# --------------------------------------------------------------------- #
# Adversarial probes
# --------------------------------------------------------------------- #


def test_control_characters_in_project_id_are_stripped_not_smuggled() -> None:
    h = _Harness()
    outcome = h.engine.remember(
        _candidate(
            memory_type=MemoryType.PROJECT,
            project_id="sam\x00core",
            source=MemorySource.SYSTEM,
        )
    )
    assert outcome.memory is not None
    assert "\x00" not in (outcome.memory.project_id or "")


def test_similar_looking_project_ids_do_not_cross_contaminate() -> None:
    h = _Harness()
    h.engine.remember(
        _candidate(
            memory_type=MemoryType.PROJECT,
            project_id="sam",
            source=MemorySource.SYSTEM,
            content="Sam core project fact one.",
        )
    )
    h.engine.remember(
        _candidate(
            memory_type=MemoryType.PROJECT,
            project_id="sam-production",
            source=MemorySource.SYSTEM,
            content="Sam production project fact two.",
        )
    )

    result = h.engine.retrieve(
        RetrievalQuery(
            principal=_principal(),
            memory_types=(MemoryType.PROJECT,),
            project_id="sam",
        )
    )

    assert len(result.items) == 1
    assert result.items[0].memory.project_id == "sam"


def test_oversized_candidate_content_is_rejected_at_construction() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        _candidate(content="x" * 2001)


def test_malformed_memory_type_string_cannot_construct_a_candidate() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        MemoryCandidate.model_validate(
            {
                "principal": {"kind": "user", "id": "ali"},
                "memory_type": "not_a_real_type",
                "content": "x",
                "source": "user_explicit",
                "confidence": "explicit",
            }
        )


def test_empty_principal_id_cannot_construct_a_candidate() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        MemoryCandidate.model_validate(
            {
                "principal": {"kind": "user", "id": "   "},
                "memory_type": "semantic",
                "content": "x",
                "source": "user_explicit",
                "confidence": "explicit",
            }
        )


def test_empty_string_project_id_is_treated_as_missing_not_a_project() -> None:
    """An empty/whitespace project_id can never satisfy PROJECT's requirement."""

    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        _candidate(memory_type=MemoryType.PROJECT, project_id="   ")


def test_null_metadata_is_rejected_not_defaulted_silently() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        MemoryCandidate.model_validate(
            {
                "principal": {"kind": "user", "id": "ali"},
                "memory_type": "semantic",
                "content": "x",
                "source": "user_explicit",
                "confidence": "explicit",
                "metadata": None,
            }
        )


def test_unicode_confusable_project_ids_do_not_cross_contaminate() -> None:
    """Different Unicode normalization forms of the same-looking id never
    collide — proven with explicit NFC/NFD forms, not source-literal text
    an editor could silently normalize for us."""

    nfc = unicodedata.normalize("NFC", "café")  # precomposed é
    nfd = unicodedata.normalize("NFD", "café")  # e + combining accent
    assert nfc != nfd  # sanity: these really are different code points

    h = _Harness()
    h.engine.remember(
        _candidate(
            memory_type=MemoryType.PROJECT,
            project_id=nfc,
            source=MemorySource.SYSTEM,
            content="NFC-named project fact.",
        )
    )
    h.engine.remember(
        _candidate(
            memory_type=MemoryType.PROJECT,
            project_id=nfd,
            source=MemorySource.SYSTEM,
            content="NFD-named project fact.",
        )
    )

    result = h.engine.retrieve(
        RetrievalQuery(
            principal=_principal(),
            memory_types=(MemoryType.PROJECT,),
            project_id=nfc,
        )
    )

    # Only the exact code-point match is returned — never both, which
    # would mean two visually-identical project ids were silently merged.
    assert len(result.items) == 1
