"""The Memory Engine.

Implements the flow the Phase 4 task specifies:

    LLM / caller
       │
       ▼
    MemoryCandidate
       │
       ▼
    MemoryPolicy.decide()      →  STORE / REJECT / REQUIRES_REVIEW
       │  (only STORE continues)
       ▼
    MemoryEngine.remember()
       │
       ▼
    MemoryStore

The LLM can propose a candidate; it cannot make it persist merely by
proposing it (``MemoryPolicy`` decides that, deterministically, before
``MemoryEngine`` ever calls the store — see ``sam.memory.policy``), and
every read/update/delete requires an explicit ``principal`` that the
store itself uses to enforce ownership — the engine never assumes "if a
caller reached me, it is allowed". See ``docs/memory.md`` for the full
security write-up.

``MemoryEngine`` knows nothing about whether ``store``/``working_store``
are backed by the in-memory Phase 4 implementations or a future
PostgreSQL-backed one — it only depends on the ``MemoryStore`` /
``WorkingMemoryStore`` protocols.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from sam.memory.audit import MemoryAuditSink
from sam.memory.errors import MemoryNotFoundError, MemoryPermissionDeniedError
from sam.memory.models import (
    Memory,
    MemoryAuditEvent,
    MemoryAuditOutcome,
    MemoryCandidate,
    MemoryImportance,
    MemoryOperation,
    MemoryOutcome,
    MemoryStatus,
    MemoryType,
    PolicyDecision,
    PolicyOutcome,
    PolicyReason,
    Principal,
    RetrievalQuery,
    RetrievalResult,
    WorkingMemoryEntry,
    utc_now,
)
from sam.memory.policy import MemoryPolicy
from sam.memory.retrieval import rank
from sam.memory.store import MemoryStore
from sam.memory.working import WorkingMemoryStore

_LONG_TERM_TYPES = (MemoryType.EPISODIC, MemoryType.SEMANTIC, MemoryType.PROJECT)


class PermissionChecker(Protocol):
    """A minimal, provider-neutral authorization hook.

    Deliberately not tied to ``sam.permissions``' concrete enums — Phase 4
    does not modify that package. A future phase can implement this
    Protocol with a small adapter that calls the real
    ``sam.permissions.engine.PermissionEngine`` internally, translating
    ``operation``/``memory`` into a ``PermissionRequest``. Until that
    adapter exists, leaving ``permission_checker`` unset (the default)
    means deletion is authorized by store-enforced ownership alone —
    the Phase-4-only baseline, documented in ``docs/memory.md``.
    """

    def check(self, *, principal: Principal, operation: str, memory: Memory) -> bool:
        """Return True if ``principal`` may perform ``operation`` on ``memory``."""


def new_memory_id() -> str:
    return uuid4().hex


class MemoryEngine:
    """Ties policy, long-term storage, working memory, and audit together.

    No global state: construct one per application (or per test) with
    explicit collaborators, the same discipline as
    ``sam.permissions.engine.PermissionEngine``.
    """

    def __init__(
        self,
        *,
        store: MemoryStore,
        working_store: WorkingMemoryStore,
        policy: MemoryPolicy | None = None,
        audit_sink: MemoryAuditSink | None = None,
        permission_checker: PermissionChecker | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._working = working_store
        self._policy = policy or MemoryPolicy()
        self._audit = audit_sink
        self._permission_checker = permission_checker
        self._clock = clock

    # ------------------------------------------------------------- #
    # Long-term memory
    # ------------------------------------------------------------- #

    def remember(self, candidate: MemoryCandidate) -> MemoryOutcome:
        """Run ``candidate`` through policy, then store it iff STORE.

        Never raises for a well-formed candidate: an unexpected internal
        failure (a raising store, a bug) is caught and converted into a
        REJECT outcome — remembering fails closed, the same as Phase 3's
        permission evaluation never fails open.
        """

        now = self._clock()
        try:
            outcome = self._remember_unsafe(candidate, now=now)
        except Exception:
            decision = PolicyDecision(
                outcome=PolicyOutcome.REJECT,
                candidate=candidate,
                decided_at=now,
                reason=PolicyReason.INVALID_CANDIDATE,
            )
            outcome = MemoryOutcome(decision=decision, memory=None)
        self._record_audit(
            MemoryOperation.REMEMBER,
            principal=candidate.principal,
            memory_id=outcome.memory.memory_id if outcome.memory else None,
            memory_type=candidate.memory_type,
            project_id=candidate.project_id,
            outcome=(
                MemoryAuditOutcome.SUCCESS
                if outcome.memory is not None
                else MemoryAuditOutcome.NOT_STORED
            ),
            policy_reason=outcome.decision.reason,
            now=now,
        )
        return outcome

    def _remember_unsafe(
        self, candidate: MemoryCandidate, *, now: datetime
    ) -> MemoryOutcome:
        decision = self._policy.decide(candidate, now=now)
        if decision.outcome is not PolicyOutcome.STORE:
            return MemoryOutcome(decision=decision, memory=None)

        duplicate = self._find_exact_duplicate(candidate, now=now)
        if duplicate is not None:
            touched = duplicate.model_copy(update={"last_accessed_at": now})
            saved = self._store.replace(touched, principal=candidate.principal)
            return MemoryOutcome(decision=decision, memory=saved, was_duplicate=True)

        memory = Memory(
            memory_id=new_memory_id(),
            memory_type=candidate.memory_type,
            principal=candidate.principal,
            content=candidate.content,
            source=candidate.source,
            confidence=candidate.confidence,
            project_id=candidate.project_id,
            tags=candidate.tags,
            metadata=candidate.metadata,
            importance=candidate.importance,
            created_at=now,
            updated_at=now,
            expires_at=candidate.expires_at,
        )
        saved = self._store.save(memory)
        return MemoryOutcome(decision=decision, memory=saved)

    def _find_exact_duplicate(
        self, candidate: MemoryCandidate, *, now: datetime
    ) -> Memory | None:
        """Exact-duplicate handling only — never fuzzy matching.

        An identical (principal, type, project, normalized content) match
        is treated as an access of the existing memory rather than a new
        record. A near-duplicate is left untouched, for a future
        semantic-deduplication phase.
        """

        normalized = candidate.normalized_content()
        siblings = self._store.list_candidates(
            principal=candidate.principal,
            memory_types=(candidate.memory_type,),
            project_id=candidate.project_id,
        )
        for existing in siblings:
            same_content = existing.normalized_content() == normalized
            if existing.is_usable(now=now) and same_content:
                return existing
        return None

    def get(self, memory_id: str, *, principal: Principal) -> Memory | None:
        """Fetch one memory by id, scoped to ``principal``.

        Touches ``last_accessed_at`` on a successful fetch. Returns
        ``None`` for a missing id, an expired/archived memory, *and* a
        memory owned by a different principal — all three are
        indistinguishable to the caller.
        """

        now = self._clock()
        outcome = MemoryAuditOutcome.NOT_FOUND
        memory: Memory | None = None
        try:
            found = self._store.get(memory_id, principal=principal)
            if found is not None and found.is_usable(now=now):
                touched = found.model_copy(update={"last_accessed_at": now})
                memory = self._store.replace(touched, principal=principal)
                outcome = MemoryAuditOutcome.SUCCESS
        except Exception:
            memory = None
            outcome = MemoryAuditOutcome.ERROR
        self._record_audit(
            MemoryOperation.GET,
            principal=principal,
            memory_id=memory_id,
            memory_type=memory.memory_type if memory else None,
            project_id=memory.project_id if memory else None,
            outcome=outcome,
            now=now,
        )
        return memory

    def update(
        self,
        memory_id: str,
        *,
        principal: Principal,
        content: str | None = None,
        tags: tuple[str, ...] | None = None,
        metadata: dict[str, str] | None = None,
        importance: MemoryImportance | None = None,
        status: MemoryStatus | None = None,
    ) -> Memory:
        """Correct an existing memory in place (no version history).

        Only content/tags/metadata/importance/status may change — the
        owning principal, memory type, and project scope are immutable
        through this method (enforced again, defensively, by
        ``MemoryStore.replace``): an "update" can never be used to move a
        memory to a different owner or project.

        Raises ``MemoryNotFoundError`` if ``memory_id`` does not exist for
        ``principal`` (including if it belongs to someone else).
        """

        now = self._clock()
        existing = self._store.get(memory_id, principal=principal)
        if existing is None:
            self._record_audit(
                MemoryOperation.UPDATE,
                principal=principal,
                memory_id=memory_id,
                memory_type=None,
                project_id=None,
                outcome=MemoryAuditOutcome.NOT_FOUND,
                now=now,
            )
            raise MemoryNotFoundError(f"no memory with id {memory_id!r}")

        merged = existing.model_dump()
        if content is not None:
            merged["content"] = content
        if tags is not None:
            merged["tags"] = tags
        if metadata is not None:
            merged["metadata"] = metadata
        if importance is not None:
            merged["importance"] = importance
        if status is not None:
            merged["status"] = status
        merged["updated_at"] = now

        try:
            # Re-validated in full via the model, not model_copy — content
            # and metadata here may be externally supplied and must pass
            # every Field/validator constraint again, not bypass them.
            candidate_memory = Memory.model_validate(merged)
            updated = self._store.replace(candidate_memory, principal=principal)
        except Exception:
            self._record_audit(
                MemoryOperation.UPDATE,
                principal=principal,
                memory_id=memory_id,
                memory_type=existing.memory_type,
                project_id=existing.project_id,
                outcome=MemoryAuditOutcome.ERROR,
                now=now,
            )
            raise

        self._record_audit(
            MemoryOperation.UPDATE,
            principal=principal,
            memory_id=memory_id,
            memory_type=updated.memory_type,
            project_id=updated.project_id,
            outcome=MemoryAuditOutcome.SUCCESS,
            now=now,
        )
        return updated

    def delete(self, memory_id: str, *, principal: Principal) -> None:
        """Permanently delete a memory — a real "forget this", not a
        status change. Scoped to ``principal``; raises
        ``MemoryNotFoundError`` for a missing or not-owned id.

        If a ``PermissionChecker`` was configured, it is consulted first
        and a denial (or the checker itself raising) fails closed —
        deletion never proceeds on an authorization error.
        """

        now = self._clock()
        existing = self._store.get(memory_id, principal=principal)
        if existing is None:
            self._record_audit(
                MemoryOperation.DELETE,
                principal=principal,
                memory_id=memory_id,
                memory_type=None,
                project_id=None,
                outcome=MemoryAuditOutcome.NOT_FOUND,
                now=now,
            )
            raise MemoryNotFoundError(f"no memory with id {memory_id!r}")

        if self._permission_checker is not None:
            allowed = self._check_permission(principal=principal, memory=existing)
            if not allowed:
                self._record_audit(
                    MemoryOperation.DELETE,
                    principal=principal,
                    memory_id=memory_id,
                    memory_type=existing.memory_type,
                    project_id=existing.project_id,
                    outcome=MemoryAuditOutcome.DENIED,
                    now=now,
                )
                raise MemoryPermissionDeniedError(
                    "permission denied for memory deletion"
                )

        try:
            self._store.delete(memory_id, principal=principal)
        except Exception:
            self._record_audit(
                MemoryOperation.DELETE,
                principal=principal,
                memory_id=memory_id,
                memory_type=existing.memory_type,
                project_id=existing.project_id,
                outcome=MemoryAuditOutcome.ERROR,
                now=now,
            )
            raise

        self._record_audit(
            MemoryOperation.DELETE,
            principal=principal,
            memory_id=memory_id,
            memory_type=existing.memory_type,
            project_id=existing.project_id,
            outcome=MemoryAuditOutcome.SUCCESS,
            now=now,
        )

    def _check_permission(self, *, principal: Principal, memory: Memory) -> bool:
        if self._permission_checker is None:
            return True
        try:
            return self._permission_checker.check(
                principal=principal, operation="memory:delete", memory=memory
            )
        except Exception:
            # Fail closed: a raising checker denies, it never authorizes.
            return False

    def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        """Deterministic, bounded, principal-scoped retrieval.

        See ``sam.memory.retrieval`` — this is not semantic search.
        """

        now = self._clock()
        try:
            types = self._effective_types(query)
            candidates = self._store.list_candidates(
                principal=query.principal,
                memory_types=types,
                project_id=query.project_id,
            )
            result = rank(candidates, query, now=now)
            outcome = MemoryAuditOutcome.SUCCESS
        except Exception:
            result = RetrievalResult(items=(), query=query)
            outcome = MemoryAuditOutcome.ERROR
        self._record_audit(
            MemoryOperation.RETRIEVE,
            principal=query.principal,
            memory_id=None,
            memory_type=None,
            project_id=query.project_id,
            outcome=outcome,
            now=now,
            result_count=len(result.items),
        )
        return result

    @staticmethod
    def _effective_types(query: RetrievalQuery) -> Sequence[MemoryType] | None:
        """Which types to query, enforcing "no unscoped project leakage".

        ``RetrievalQuery`` already rejects a query that asks *exclusively*
        for PROJECT memories without a ``project_id`` at construction time
        (fail closed). Here, a *mixed* type list that includes PROJECT
        without a ``project_id`` simply drops PROJECT rather than erroring
        — a "everything relevant" query should not fail just because
        project-scoped facts exist; it should just not include them
        without being told which project.
        """

        if query.memory_types is not None:
            unscoped_project = (
                MemoryType.PROJECT in query.memory_types and query.project_id is None
            )
            if unscoped_project:
                return tuple(
                    t for t in query.memory_types if t is not MemoryType.PROJECT
                )
            return query.memory_types
        if query.project_id is None:
            return (MemoryType.EPISODIC, MemoryType.SEMANTIC)
        return _LONG_TERM_TYPES

    # ------------------------------------------------------------- #
    # Working memory — never policy-gated, never long-term.
    # ------------------------------------------------------------- #

    def remember_working(
        self,
        *,
        principal: Principal,
        key: str,
        content: str,
        expires_at: datetime | None = None,
    ) -> WorkingMemoryEntry:
        now = self._clock()
        existing = self._working.get(principal, key, now=now)
        entry = WorkingMemoryEntry(
            principal=principal,
            key=key,
            content=content,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            expires_at=expires_at,
        )
        return self._working.set(entry, now=now)

    def recall_working(
        self, *, principal: Principal, key: str
    ) -> WorkingMemoryEntry | None:
        return self._working.get(principal, key, now=self._clock())

    def list_working(self, *, principal: Principal) -> Sequence[WorkingMemoryEntry]:
        return self._working.list(principal, now=self._clock())

    def forget_working(self, *, principal: Principal, key: str) -> None:
        self._working.delete(principal, key)

    def clear_working(self, *, principal: Principal) -> None:
        self._working.clear(principal)

    # ------------------------------------------------------------- #
    # Audit
    # ------------------------------------------------------------- #

    def _record_audit(
        self,
        operation: MemoryOperation,
        *,
        principal: Principal,
        memory_id: str | None,
        memory_type: MemoryType | None,
        project_id: str | None,
        outcome: MemoryAuditOutcome,
        now: datetime,
        policy_reason: PolicyReason | None = None,
        result_count: int | None = None,
    ) -> None:
        if self._audit is None:
            return
        event = MemoryAuditEvent(
            event_id=uuid4().hex,
            occurred_at=now,
            operation=operation,
            principal=principal,
            memory_type=memory_type,
            memory_id=memory_id,
            project_id=project_id,
            outcome=outcome,
            policy_reason=policy_reason,
            result_count=result_count,
        )
        try:
            self._audit.record(event)
        except Exception:
            # Best-effort observability, not an authorization gate — the
            # same trade-off as sam.permissions.engine._record_audit.
            return
