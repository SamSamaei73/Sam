"""The Knowledge Engine — the single orchestration entry point.

Owns nothing except orchestration; every real decision is delegated:

    caller
       │
       ▼
    KnowledgeOperationRequest
       │
       ▼
    sam.knowledge.policy.build_request()   (pure mapping — no decision)
       │
       ▼
    sam.permissions.engine.PermissionEngine.evaluate()   (the only authority)
       │
       ├─ DENY ─────────────────────────► stop, store/index/parser never called
       ├─ CONFIRM_REQUIRED ───────────────► stop, store/index/parser never called
       └─ ALLOW
            │
            ▼
       dispatch to sam.knowledge.ingestion / .retrieval / .store / .index

There is no path from a caller/LLM to ``KnowledgeStore`` or
``KnowledgeIndex`` that does not first obtain ALLOW from
``PermissionEngine.evaluate`` — each ``_..._unsafe`` method is the only
caller of ``evaluate`` for its operation, and the only place any
store/index/parser call happens. Every public method never raises for a
well-formed request: an unexpected internal failure (a raising store, a
raising permission engine, a bug) is caught and converted into a
``FAILED`` result — the same fail-closed discipline as
``sam.coding.executor.CodingExecutor``.

This module never imports ``sam.memory`` anything, directly or
transitively through its own code (only ``sam.knowledge.metadata``
imports one pure function from ``sam.memory.sanitization`` — see that
module's docstring). No ingestion here ever writes to Memory.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from threading import RLock
from typing import TypeVar

from sam.knowledge import policy
from sam.knowledge.audit import KnowledgeAuditSink
from sam.knowledge.chunker import ChunkerConfig
from sam.knowledge.errors import RetrievalError
from sam.knowledge.index import KnowledgeIndex
from sam.knowledge.ingestion import compute_checksum, run_ingestion_pipeline
from sam.knowledge.models import (
    ExecutionOutcome,
    GetResourceRequest,
    IngestionResult,
    IngestionStatus,
    IngestResourceRequest,
    KnowledgeAuditEvent,
    KnowledgeConfirmationOutcome,
    KnowledgeErrorCategory,
    KnowledgeOperation,
    ListResourcesRequest,
    PermissionOutcomeSummary,
    Principal,
    RemovalResult,
    RemoveResourceRequest,
    ResourceListResult,
    ResourceQueryResult,
    ResourceType,
    RetrievalOutcome,
    RetrieveRequest,
    RiskLevel,
    new_id,
    utc_now,
)
from sam.knowledge.parser import DocumentParser, default_parsers
from sam.knowledge.retrieval import KnowledgeRetriever, retrieve_or_raise
from sam.knowledge.store import KnowledgeStore
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import DecisionOutcome, DenialReason, PermissionDecision

_T = TypeVar("_T")


def _confirmation_outcome_for(
    permission_outcome: PermissionOutcomeSummary,
    error_category: KnowledgeErrorCategory | None,
) -> KnowledgeConfirmationOutcome | None:
    if permission_outcome is PermissionOutcomeSummary.DENY:
        if error_category is KnowledgeErrorCategory.CONFIRMATION_INVALID:
            return KnowledgeConfirmationOutcome.CONFIRM_REQUIRED
        return KnowledgeConfirmationOutcome.DENIED
    if permission_outcome is PermissionOutcomeSummary.CONFIRM_REQUIRED:
        return KnowledgeConfirmationOutcome.CONFIRM_REQUIRED
    return None


class KnowledgeEngine:
    """Ties policy, the Permission Engine, storage, the index, retrieval,
    and audit together. No global state: construct one per application
    (or per test) with explicit collaborators."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        index: KnowledgeIndex,
        permission_engine: PermissionEngine,
        retriever: KnowledgeRetriever | None = None,
        parsers: tuple[DocumentParser, ...] | None = None,
        chunker_config: ChunkerConfig | None = None,
        audit_sink: KnowledgeAuditSink | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._index = index
        self._permission_engine = permission_engine
        self._retriever = retriever or KnowledgeRetriever(store=store, index=index)
        self._parsers = parsers if parsers is not None else default_parsers()
        self._chunker_config = (
            chunker_config if chunker_config is not None else ChunkerConfig()
        )
        self._audit = audit_sink
        self._clock = clock
        # Resource ids whose failed ingestion could not be fully cleaned
        # up. Per-instance (never global) and consulted by every read
        # path, so a resource the engine reported as FAILED can never be
        # returned even if the store/index still hold remnants of it.
        self._quarantined: set[str] = set()
        self._quarantine_lock = RLock()

    # ------------------------------------------------------------- #
    # ingest
    # ------------------------------------------------------------- #

    def ingest(
        self, request: IngestResourceRequest, *, confirmation_id: str | None = None
    ) -> IngestionResult:
        now = self._clock()
        operation_id = new_id()
        risk = RiskLevel.CRITICAL
        try:
            risk = policy.risk_for(KnowledgeOperation.INGEST_RESOURCE)
            result = self._ingest_unsafe(
                request,
                operation_id=operation_id,
                confirmation_id=confirmation_id,
                now=now,
            )
        except Exception:
            result = IngestionResult(
                operation_id=operation_id,
                principal=request.principal,
                permission_outcome=PermissionOutcomeSummary.DENY,
                status=IngestionStatus.FAILED,
                error_category=KnowledgeErrorCategory.INTERNAL_ERROR,
                created_at=now,
            )
        self._record_audit(
            now=now,
            operation_id=operation_id,
            principal=request.principal,
            collection_id=request.collection_id,
            resource_id=result.resource.resource_id if result.resource else None,
            operation=KnowledgeOperation.INGEST_RESOURCE,
            risk=risk,
            permission_outcome=result.permission_outcome,
            error_category=result.error_category,
            execution_outcome=_status_to_outcome(result.status),
            resource_type=result.resource.resource_type if result.resource else None,
            chunk_count=result.chunk_count
            if result.status is IngestionStatus.SUCCESS
            else None,
            rollback_incomplete=result.rollback_incomplete,
        )
        return result

    def _ingest_unsafe(
        self,
        request: IngestResourceRequest,
        *,
        operation_id: str,
        confirmation_id: str | None,
        now: datetime,
    ) -> IngestionResult:
        decision = self._permission_engine.evaluate(
            policy.build_request(request), confirmation_id=confirmation_id
        )
        denial = self._deny_ingestion(
            decision, operation_id=operation_id, principal=request.principal, now=now
        )
        if denial is not None:
            return denial

        checksum = compute_checksum(request.content)
        existing = self._store.find_by_checksum(request.collection_id, checksum)
        if existing is not None and self._is_quarantined(existing.resource_id):
            # A previous attempt failed and its cleanup did not complete.
            # Retry that cleanup first: only if it now succeeds may this
            # attempt proceed, so a retry never stacks a second copy on a
            # zombie and never reports a quarantined resource as a duplicate.
            if not self._compensate(request.collection_id, existing.resource_id):
                return IngestionResult(
                    operation_id=operation_id,
                    principal=request.principal,
                    permission_outcome=PermissionOutcomeSummary.ALLOW,
                    status=IngestionStatus.FAILED,
                    error_category=KnowledgeErrorCategory.STORAGE_ERROR,
                    rollback_incomplete=True,
                    created_at=now,
                )
            existing = self._store.find_by_checksum(request.collection_id, checksum)
        if existing is not None:
            return IngestionResult(
                operation_id=operation_id,
                principal=request.principal,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                status=IngestionStatus.DUPLICATE,
                duplicate_of=existing.resource_id,
                created_at=now,
            )

        outcome = run_ingestion_pipeline(
            request, parsers=self._parsers, chunker_config=self._chunker_config, now=now
        )
        if not outcome.accepted:
            return IngestionResult(
                operation_id=operation_id,
                principal=request.principal,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                status=IngestionStatus.REJECTED,
                error_category=outcome.error_category,
                created_at=now,
            )

        resource = outcome.resource
        assert resource is not None  # guaranteed by IngestionOutcome.accepted
        self._store.ensure_collection(request.collection_id, now=now)
        # Persistence is NOT atomic: three separate provider calls. Any
        # failure triggers a compensating cleanup of both the index and
        # the store (see ``_compensate``); the original failure category
        # is always the reported one.
        stage = KnowledgeErrorCategory.STORAGE_ERROR
        try:
            self._store.save_resource(resource)
            self._store.save_chunks(resource.resource_id, outcome.chunks)
            stage = KnowledgeErrorCategory.INDEXING_ERROR
            self._index.index_chunks(outcome.chunks)
        except Exception:
            cleaned = self._compensate(request.collection_id, resource.resource_id)
            return IngestionResult(
                operation_id=operation_id,
                principal=request.principal,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                status=IngestionStatus.FAILED,
                error_category=stage,
                rollback_incomplete=not cleaned,
                created_at=now,
            )

        return IngestionResult(
            operation_id=operation_id,
            principal=request.principal,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
            status=IngestionStatus.SUCCESS,
            resource=resource,
            chunk_count=len(outcome.chunks),
            created_at=now,
        )

    def _compensate(self, collection_id: str, resource_id: str) -> bool:
        """Best-effort cleanup of a resource left by a failed ingestion.

        Removes index visibility first, then the store record, each step
        guarded independently so one failing never skips the other. The
        store step is verified with a read-back. Returns ``True`` only if
        both steps completed and the store no longer holds the resource;
        otherwise the id is quarantined and ``False`` is returned. Never
        raises, never logs or returns document content.
        """

        clean = True
        try:
            self._index.remove_resource(resource_id)
        except Exception:
            clean = False
        try:
            self._store.delete_resource(collection_id, resource_id)
            if self._store.get_resource(collection_id, resource_id) is not None:
                clean = False
        except Exception:
            clean = False
        with self._quarantine_lock:
            if clean:
                self._quarantined.discard(resource_id)
            else:
                self._quarantined.add(resource_id)
        return clean

    def _is_quarantined(self, resource_id: str) -> bool:
        with self._quarantine_lock:
            return resource_id in self._quarantined

    def _deny_ingestion(
        self,
        decision: PermissionDecision,
        *,
        operation_id: str,
        principal: Principal,
        now: datetime,
    ) -> IngestionResult | None:
        if decision.outcome is DecisionOutcome.DENY:
            category = (
                KnowledgeErrorCategory.CONFIRMATION_INVALID
                if decision.reason is DenialReason.CONFIRMATION_INVALID
                else KnowledgeErrorCategory.PERMISSION_DENIED
            )
            return IngestionResult(
                operation_id=operation_id,
                principal=principal,
                permission_outcome=PermissionOutcomeSummary.DENY,
                status=IngestionStatus.FAILED,
                error_category=category,
                created_at=now,
            )
        if decision.outcome is DecisionOutcome.CONFIRM_REQUIRED:
            pending_id = (
                decision.confirmation.confirmation_id if decision.confirmation else None
            )
            return IngestionResult(
                operation_id=operation_id,
                principal=principal,
                permission_outcome=PermissionOutcomeSummary.CONFIRM_REQUIRED,
                status=IngestionStatus.FAILED,
                error_category=KnowledgeErrorCategory.CONFIRMATION_REQUIRED,
                confirmation_id=pending_id,
                created_at=now,
            )
        return None

    # ------------------------------------------------------------- #
    # get_resource
    # ------------------------------------------------------------- #

    def get_resource(
        self, request: GetResourceRequest, *, confirmation_id: str | None = None
    ) -> ResourceQueryResult:
        now = self._clock()
        operation_id = new_id()
        risk = RiskLevel.CRITICAL
        try:
            risk = policy.risk_for(KnowledgeOperation.GET_RESOURCE)
            result = self._get_resource_unsafe(
                request,
                operation_id=operation_id,
                confirmation_id=confirmation_id,
                now=now,
            )
        except Exception:
            result = ResourceQueryResult(
                operation_id=operation_id,
                principal=request.principal,
                permission_outcome=PermissionOutcomeSummary.DENY,
                outcome=ExecutionOutcome.FAILED,
                error_category=KnowledgeErrorCategory.INTERNAL_ERROR,
                created_at=now,
            )
        self._record_audit(
            now=now,
            operation_id=operation_id,
            principal=request.principal,
            collection_id=request.collection_id,
            resource_id=request.resource_id,
            operation=KnowledgeOperation.GET_RESOURCE,
            risk=risk,
            permission_outcome=result.permission_outcome,
            error_category=result.error_category,
            execution_outcome=result.outcome,
            resource_type=result.resource.resource_type if result.resource else None,
        )
        return result

    def _get_resource_unsafe(
        self,
        request: GetResourceRequest,
        *,
        operation_id: str,
        confirmation_id: str | None,
        now: datetime,
    ) -> ResourceQueryResult:
        decision = self._permission_engine.evaluate(
            policy.build_request(request), confirmation_id=confirmation_id
        )
        gate = self._gate(
            decision,
            builder=lambda **kw: ResourceQueryResult(
                operation_id=operation_id,
                principal=request.principal,
                created_at=now,
                **kw,
            ),
        )
        if gate is not None:
            return gate

        resource = self._store.get_resource(request.collection_id, request.resource_id)
        if resource is not None and self._is_quarantined(resource.resource_id):
            resource = None
        if resource is None:
            return ResourceQueryResult(
                operation_id=operation_id,
                principal=request.principal,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                outcome=ExecutionOutcome.FAILED,
                error_category=KnowledgeErrorCategory.RESOURCE_NOT_FOUND,
                created_at=now,
            )
        return ResourceQueryResult(
            operation_id=operation_id,
            principal=request.principal,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
            outcome=ExecutionOutcome.SUCCESS,
            resource=resource,
            created_at=now,
        )

    # ------------------------------------------------------------- #
    # list_resources
    # ------------------------------------------------------------- #

    def list_resources(
        self, request: ListResourcesRequest, *, confirmation_id: str | None = None
    ) -> ResourceListResult:
        now = self._clock()
        operation_id = new_id()
        risk = RiskLevel.CRITICAL
        try:
            risk = policy.risk_for(KnowledgeOperation.LIST_RESOURCES)
            result = self._list_resources_unsafe(
                request,
                operation_id=operation_id,
                confirmation_id=confirmation_id,
                now=now,
            )
        except Exception:
            result = ResourceListResult(
                operation_id=operation_id,
                principal=request.principal,
                permission_outcome=PermissionOutcomeSummary.DENY,
                outcome=ExecutionOutcome.FAILED,
                error_category=KnowledgeErrorCategory.INTERNAL_ERROR,
                created_at=now,
            )
        self._record_audit(
            now=now,
            operation_id=operation_id,
            principal=request.principal,
            collection_id=request.collection_id,
            resource_id=None,
            operation=KnowledgeOperation.LIST_RESOURCES,
            risk=risk,
            permission_outcome=result.permission_outcome,
            error_category=result.error_category,
            execution_outcome=result.outcome,
            result_count=len(result.resources)
            if result.outcome is ExecutionOutcome.SUCCESS
            else None,
        )
        return result

    def _list_resources_unsafe(
        self,
        request: ListResourcesRequest,
        *,
        operation_id: str,
        confirmation_id: str | None,
        now: datetime,
    ) -> ResourceListResult:
        decision = self._permission_engine.evaluate(
            policy.build_request(request), confirmation_id=confirmation_id
        )
        gate = self._gate(
            decision,
            builder=lambda **kw: ResourceListResult(
                operation_id=operation_id,
                principal=request.principal,
                created_at=now,
                **kw,
            ),
        )
        if gate is not None:
            return gate

        resources = tuple(
            resource
            for resource in self._store.list_resources(request.collection_id)
            if not self._is_quarantined(resource.resource_id)
        )
        return ResourceListResult(
            operation_id=operation_id,
            principal=request.principal,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
            outcome=ExecutionOutcome.SUCCESS,
            resources=resources,
            created_at=now,
        )

    # ------------------------------------------------------------- #
    # retrieve
    # ------------------------------------------------------------- #

    def retrieve(
        self, request: RetrieveRequest, *, confirmation_id: str | None = None
    ) -> RetrievalOutcome:
        now = self._clock()
        operation_id = new_id()
        risk = RiskLevel.CRITICAL
        try:
            risk = policy.risk_for(KnowledgeOperation.RETRIEVE)
            result = self._retrieve_unsafe(
                request,
                operation_id=operation_id,
                confirmation_id=confirmation_id,
                now=now,
            )
        except Exception:
            result = RetrievalOutcome(
                operation_id=operation_id,
                principal=request.principal,
                permission_outcome=PermissionOutcomeSummary.DENY,
                outcome=ExecutionOutcome.FAILED,
                error_category=KnowledgeErrorCategory.INTERNAL_ERROR,
                created_at=now,
            )
        self._record_audit(
            now=now,
            operation_id=operation_id,
            principal=request.principal,
            collection_id=policy.collection_for(request),
            resource_id=request.query.resource_id,
            operation=KnowledgeOperation.RETRIEVE,
            risk=risk,
            permission_outcome=result.permission_outcome,
            error_category=result.error_category,
            execution_outcome=result.outcome,
            result_count=len(result.results)
            if result.outcome is ExecutionOutcome.SUCCESS
            else None,
        )
        return result

    def _retrieve_unsafe(
        self,
        request: RetrieveRequest,
        *,
        operation_id: str,
        confirmation_id: str | None,
        now: datetime,
    ) -> RetrievalOutcome:
        decision = self._permission_engine.evaluate(
            policy.build_request(request), confirmation_id=confirmation_id
        )
        gate = self._gate(
            decision,
            builder=lambda **kw: RetrievalOutcome(
                operation_id=operation_id,
                principal=request.principal,
                created_at=now,
                **kw,
            ),
        )
        if gate is not None:
            return gate

        try:
            results = tuple(
                result
                for result in retrieve_or_raise(self._retriever, request.query)
                if not self._is_quarantined(result.resource.resource_id)
            )
        except RetrievalError:
            return RetrievalOutcome(
                operation_id=operation_id,
                principal=request.principal,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                outcome=ExecutionOutcome.FAILED,
                error_category=KnowledgeErrorCategory.RETRIEVAL_ERROR,
                created_at=now,
            )
        return RetrievalOutcome(
            operation_id=operation_id,
            principal=request.principal,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
            outcome=ExecutionOutcome.SUCCESS,
            results=results,
            created_at=now,
        )

    # ------------------------------------------------------------- #
    # remove_resource
    # ------------------------------------------------------------- #

    def remove_resource(
        self, request: RemoveResourceRequest, *, confirmation_id: str | None = None
    ) -> RemovalResult:
        now = self._clock()
        operation_id = new_id()
        risk = RiskLevel.CRITICAL
        try:
            risk = policy.risk_for(KnowledgeOperation.REMOVE_RESOURCE)
            result = self._remove_resource_unsafe(
                request,
                operation_id=operation_id,
                confirmation_id=confirmation_id,
                now=now,
            )
        except Exception:
            result = RemovalResult(
                operation_id=operation_id,
                principal=request.principal,
                permission_outcome=PermissionOutcomeSummary.DENY,
                outcome=ExecutionOutcome.FAILED,
                error_category=KnowledgeErrorCategory.INTERNAL_ERROR,
                created_at=now,
            )
        self._record_audit(
            now=now,
            operation_id=operation_id,
            principal=request.principal,
            collection_id=request.collection_id,
            resource_id=request.resource_id,
            operation=KnowledgeOperation.REMOVE_RESOURCE,
            risk=risk,
            permission_outcome=result.permission_outcome,
            error_category=result.error_category,
            execution_outcome=result.outcome,
        )
        return result

    def _remove_resource_unsafe(
        self,
        request: RemoveResourceRequest,
        *,
        operation_id: str,
        confirmation_id: str | None,
        now: datetime,
    ) -> RemovalResult:
        decision = self._permission_engine.evaluate(
            policy.build_request(request), confirmation_id=confirmation_id
        )
        gate = self._gate(
            decision,
            builder=lambda **kw: RemovalResult(
                operation_id=operation_id,
                principal=request.principal,
                created_at=now,
                **kw,
            ),
        )
        if gate is not None:
            return gate

        resource = self._store.get_resource(request.collection_id, request.resource_id)
        if resource is None:
            return RemovalResult(
                operation_id=operation_id,
                principal=request.principal,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                outcome=ExecutionOutcome.FAILED,
                error_category=KnowledgeErrorCategory.RESOURCE_NOT_FOUND,
                created_at=now,
            )
        try:
            self._index.remove_resource(request.resource_id)
        except Exception:
            return RemovalResult(
                operation_id=operation_id,
                principal=request.principal,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                outcome=ExecutionOutcome.FAILED,
                error_category=KnowledgeErrorCategory.INDEXING_ERROR,
                created_at=now,
            )
        removed = self._store.delete_resource(
            request.collection_id, request.resource_id
        )
        if not removed:
            return RemovalResult(
                operation_id=operation_id,
                principal=request.principal,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                outcome=ExecutionOutcome.FAILED,
                error_category=KnowledgeErrorCategory.RESOURCE_NOT_FOUND,
                created_at=now,
            )
        with self._quarantine_lock:
            self._quarantined.discard(request.resource_id)
        return RemovalResult(
            operation_id=operation_id,
            principal=request.principal,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
            outcome=ExecutionOutcome.SUCCESS,
            removed=True,
            created_at=now,
        )

    # ------------------------------------------------------------- #
    # shared helpers
    # ------------------------------------------------------------- #

    def _gate(
        self, decision: PermissionDecision, *, builder: Callable[..., _T]
    ) -> _T | None:
        """Common DENY/CONFIRM_REQUIRED handling for the four non-ingest
        methods (``ingest`` has its own, since ``IngestionResult`` uses a
        ``status`` field rather than ``outcome``). Returns ``None`` when
        the decision is ALLOW — the caller must then, and only then,
        proceed to its store/index/parser work."""

        if decision.outcome is DecisionOutcome.DENY:
            category = (
                KnowledgeErrorCategory.CONFIRMATION_INVALID
                if decision.reason is DenialReason.CONFIRMATION_INVALID
                else KnowledgeErrorCategory.PERMISSION_DENIED
            )
            return builder(
                permission_outcome=PermissionOutcomeSummary.DENY,
                outcome=ExecutionOutcome.FAILED,
                error_category=category,
            )
        if decision.outcome is DecisionOutcome.CONFIRM_REQUIRED:
            pending_id = (
                decision.confirmation.confirmation_id if decision.confirmation else None
            )
            return builder(
                permission_outcome=PermissionOutcomeSummary.CONFIRM_REQUIRED,
                outcome=ExecutionOutcome.FAILED,
                error_category=KnowledgeErrorCategory.CONFIRMATION_REQUIRED,
                confirmation_id=pending_id,
            )
        return None

    def _record_audit(
        self,
        *,
        now: datetime,
        operation_id: str,
        principal: Principal,
        collection_id: str,
        resource_id: str | None,
        operation: KnowledgeOperation,
        risk: RiskLevel,
        permission_outcome: PermissionOutcomeSummary,
        error_category: KnowledgeErrorCategory | None,
        execution_outcome: ExecutionOutcome | None = None,
        resource_type: ResourceType | None = None,
        chunk_count: int | None = None,
        result_count: int | None = None,
        rollback_incomplete: bool = False,
    ) -> None:
        if self._audit is None:
            return
        event = KnowledgeAuditEvent(
            event_id=new_id(),
            occurred_at=now,
            operation_id=operation_id,
            principal=principal,
            collection_id=collection_id,
            resource_id=resource_id,
            operation=operation,
            risk=risk,
            permission_outcome=permission_outcome,
            confirmation_outcome=_confirmation_outcome_for(
                permission_outcome, error_category
            ),
            execution_outcome=execution_outcome,
            error_category=error_category,
            resource_type=resource_type,
            chunk_count=chunk_count,
            result_count=result_count,
            rollback_incomplete=rollback_incomplete,
        )
        try:
            self._audit.record(event)
        except Exception:
            # Best-effort observability, not an authorization gate.
            return


def _status_to_outcome(status: IngestionStatus) -> ExecutionOutcome:
    return (
        ExecutionOutcome.SUCCESS
        if status is IngestionStatus.SUCCESS
        else ExecutionOutcome.FAILED
    )


__all__ = ["KnowledgeEngine"]
