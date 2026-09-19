"""Adversarial tests for ingestion failure handling.

Persistence is three separate provider calls (save_resource, save_chunks,
index_chunks), so it is *not* atomic. These tests prove the compensating
rollback: a failed ingestion never leaves a resource that can be treated
as ingested, cleanup failure never hides the primary failure, and the
resource stays unretrievable even when cleanup itself fails.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from sam.knowledge.chunker import ChunkerConfig
from sam.knowledge.engine import KnowledgeEngine
from sam.knowledge.index import InMemoryLexicalIndex
from sam.knowledge.models import (
    DocumentChunk,
    ExecutionOutcome,
    GetResourceRequest,
    IngestionResult,
    IngestionStatus,
    KnowledgeErrorCategory,
    ListResourcesRequest,
    PermissionOutcomeSummary,
    RetrievalQuery,
    RetrieveRequest,
)
from sam.knowledge.store import InMemoryKnowledgeStore
from sam.permissions.models import PermissionAction
from tests.test_knowledge_engine import ALICE, NOW, _Harness, _ingest_request

# Several paragraphs so a small chunk size yields multiple chunks (a
# partial index write is only meaningful with more than one chunk).
BODY = (
    "Graph neural networks propagate information along edges. "
    "Attention weights let each node prioritise neighbours. "
    "Misinformation spreads through social graphs quickly. "
    "Detection models combine text features with graph structure. "
) * 3


class FlakyStore(InMemoryKnowledgeStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_save_chunks = False
        self.fail_delete = False
        self.fail_get_after_delete = False

    def save_chunks(self, resource_id: str, chunks: tuple[DocumentChunk, ...]) -> None:
        if self.fail_save_chunks:
            raise RuntimeError("chunk storage unavailable")
        super().save_chunks(resource_id, chunks)

    def delete_resource(self, collection_id: str, resource_id: str) -> bool:
        if self.fail_delete:
            raise RuntimeError("delete unavailable")
        return super().delete_resource(collection_id, resource_id)


class FlakyIndex(InMemoryLexicalIndex):
    def __init__(self) -> None:
        super().__init__()
        self.fail_index = False
        self.fail_remove = False
        self.remove_calls: list[str] = []

    def index_chunks(self, chunks: tuple[DocumentChunk, ...]) -> None:
        if self.fail_index:
            # Write a real partial state first, then raise.
            super().index_chunks(chunks[:1])
            raise RuntimeError("index unavailable")
        super().index_chunks(chunks)

    def remove_resource(self, resource_id: str) -> None:
        self.remove_calls.append(resource_id)
        if self.fail_remove:
            raise RuntimeError("index cleanup unavailable")
        super().remove_resource(resource_id)


class _FlakyHarness(_Harness):
    store: FlakyStore
    index: FlakyIndex

    def __init__(self) -> None:
        super().__init__()
        self.store = FlakyStore()
        self.index = FlakyIndex()
        self.engine = KnowledgeEngine(
            store=self.store,
            index=self.index,
            permission_engine=self.pengine,
            chunker_config=ChunkerConfig(max_chunk_characters=100),
            audit_sink=self.audit,
        )
        self.grant_full_access(ALICE, "docs")

    def ingest(self) -> IngestionResult:
        return self.engine.ingest(_ingest_request(content=BODY.encode()))

    def retrieve_ids(self) -> list[str]:
        outcome = self.engine.retrieve(
            RetrieveRequest(
                principal=ALICE,
                query=RetrievalQuery(
                    query="graph neural networks", collection_id="docs"
                ),
            )
        )
        assert outcome.outcome is ExecutionOutcome.SUCCESS
        # One result per matching chunk: report distinct resources.
        return list(dict.fromkeys(r.resource.resource_id for r in outcome.results))

    def listed(self) -> tuple[Any, ...]:
        result = self.engine.list_resources(
            ListResourcesRequest(principal=ALICE, collection_id="docs")
        )
        return tuple(result.resources)


def _raw_retriever_ids(h: _FlakyHarness) -> list[str]:
    """What the store+index would return with no engine-level guard."""

    from sam.knowledge.retrieval import KnowledgeRetriever

    results = KnowledgeRetriever(store=h.store, index=h.index).retrieve(
        RetrievalQuery(query="graph neural networks", collection_id="docs")
    )
    return list(dict.fromkeys(r.resource.resource_id for r in results))


# --------------------------------------------------------------------- #
# Baseline
# --------------------------------------------------------------------- #


def test_baseline_ingest_produces_multiple_chunks() -> None:
    h = _FlakyHarness()
    result = h.ingest()
    assert result.status is IngestionStatus.SUCCESS
    assert result.chunk_count > 1
    assert result.rollback_incomplete is False


# --------------------------------------------------------------------- #
# save_resource succeeds, save_chunks fails
# --------------------------------------------------------------------- #


def test_save_chunks_failure_leaves_no_partial_resource() -> None:
    h = _FlakyHarness()
    h.store.fail_save_chunks = True
    result = h.ingest()

    assert result.status is IngestionStatus.FAILED
    assert result.error_category is KnowledgeErrorCategory.STORAGE_ERROR
    assert result.resource is None
    assert result.rollback_incomplete is False
    assert h.store.list_resources("docs") == ()
    assert h.store.list_collections() != ()  # the empty collection is harmless
    assert h.retrieve_ids() == []
    assert h.listed() == ()


def test_save_chunks_failure_cleans_the_index_too() -> None:
    h = _FlakyHarness()
    h.store.fail_save_chunks = True
    h.ingest()
    # Index cleanup was attempted even though indexing never started.
    assert len(h.index.remove_calls) == 1


# --------------------------------------------------------------------- #
# Index writes some entries, then raises
# --------------------------------------------------------------------- #


def test_partial_index_write_is_removed_and_store_rolled_back() -> None:
    h = _FlakyHarness()
    h.index.fail_index = True
    result = h.ingest()

    assert result.status is IngestionStatus.FAILED
    assert result.error_category is KnowledgeErrorCategory.INDEXING_ERROR
    assert result.rollback_incomplete is False
    assert h.store.list_resources("docs") == ()
    assert h.store.get_chunks("anything") == ()
    # The partial index entry written before the raise is gone.
    assert (
        h.index.search(collection_id=None, resource_id=None, query="graph", limit=10)
        == ()
    )
    assert h.retrieve_ids() == []


def test_cleanup_order_is_index_before_store() -> None:
    h = _FlakyHarness()
    h.index.fail_index = True
    order: list[str] = []
    original_delete = h.store.delete_resource

    def recording_delete(collection_id: str, resource_id: str) -> bool:
        order.append("store")
        return original_delete(collection_id, resource_id)

    original_remove = h.index.remove_resource

    def recording_remove(resource_id: str) -> None:
        order.append("index")
        original_remove(resource_id)

    h.store.delete_resource = recording_delete  # type: ignore[method-assign]
    h.index.remove_resource = recording_remove  # type: ignore[method-assign]
    h.ingest()
    assert order == ["index", "store"]


# --------------------------------------------------------------------- #
# Cleanup failures
# --------------------------------------------------------------------- #


def test_index_cleanup_failure_reports_primary_failure_and_incomplete_rollback() -> (
    None
):
    h = _FlakyHarness()
    h.index.fail_index = True
    h.index.fail_remove = True
    result = h.ingest()

    # The ORIGINAL failure is still the reported category.
    assert result.status is IngestionStatus.FAILED
    assert result.error_category is KnowledgeErrorCategory.INDEXING_ERROR
    assert result.rollback_incomplete is True
    # Store cleanup still ran despite the index cleanup failing.
    assert h.store.list_resources("docs") == ()
    # Orphaned index entries cannot surface a result.
    assert h.retrieve_ids() == []


def test_store_cleanup_failure_reports_incomplete_and_quarantines() -> None:
    h = _FlakyHarness()
    h.index.fail_index = True
    h.store.fail_delete = True
    result = h.ingest()

    assert result.status is IngestionStatus.FAILED
    assert result.error_category is KnowledgeErrorCategory.INDEXING_ERROR
    assert result.rollback_incomplete is True
    # Index cleanup still ran despite the store cleanup failing.
    assert (
        h.index.search(collection_id=None, resource_id=None, query="graph", limit=10)
        == ()
    )
    # The store really does still hold the remnant ...
    assert len(h.store.list_resources("docs")) == 1
    # ... but the engine will not surface it.
    assert h.listed() == ()
    assert h.retrieve_ids() == []


def test_both_cleanups_fail_resource_still_unretrievable() -> None:
    h = _FlakyHarness()
    h.index.fail_index = True
    h.index.fail_remove = True
    h.store.fail_delete = True
    result = h.ingest()

    assert result.rollback_incomplete is True
    zombie = h.store.list_resources("docs")
    assert len(zombie) == 1
    zombie_id = zombie[0].resource_id
    # Without the engine guard the remnants WOULD be returned ...
    assert _raw_retriever_ids(h) == [zombie_id]
    # ... with it, neither retrieve, list nor get can see the resource.
    assert h.retrieve_ids() == []
    assert h.listed() == ()
    h.grant(ALICE, PermissionAction.READ, f"docs/{zombie_id}", path=True)
    got = h.engine.get_resource(
        GetResourceRequest(principal=ALICE, collection_id="docs", resource_id=zombie_id)
    )
    assert got.outcome is ExecutionOutcome.FAILED
    assert got.error_category is KnowledgeErrorCategory.RESOURCE_NOT_FOUND


def test_cleanup_failure_is_audited_without_content() -> None:
    h = _FlakyHarness()
    h.index.fail_index = True
    h.store.fail_delete = True
    h.ingest()

    events = list(h.audit.list_events())
    failed = [e for e in events if e.rollback_incomplete]
    assert len(failed) == 1
    event = failed[0]
    assert event.error_category is KnowledgeErrorCategory.INDEXING_ERROR
    dumped = str(event.model_dump())
    for fragment in ("Graph neural", "unavailable", "Attention weights"):
        assert fragment not in dumped


def test_clean_rollback_is_not_flagged_incomplete_in_audit() -> None:
    h = _FlakyHarness()
    h.index.fail_index = True
    h.ingest()
    assert all(not e.rollback_incomplete for e in h.audit.list_events())


def test_failure_result_never_carries_a_resource() -> None:
    h = _FlakyHarness()
    h.store.fail_save_chunks = True
    h.store.fail_delete = True
    result = h.ingest()
    assert result.resource is None
    assert result.chunk_count == 0
    assert result.permission_outcome is PermissionOutcomeSummary.ALLOW


# --------------------------------------------------------------------- #
# Retry behaviour
# --------------------------------------------------------------------- #


def test_retry_after_clean_rollback_succeeds_once() -> None:
    h = _FlakyHarness()
    h.index.fail_index = True
    assert h.ingest().status is IngestionStatus.FAILED

    h.index.fail_index = False
    retry = h.ingest()
    assert retry.status is IngestionStatus.SUCCESS
    assert len(h.store.list_resources("docs")) == 1
    assert h.retrieve_ids() == [retry.resource.resource_id]  # type: ignore[union-attr]

    # A third attempt is a plain duplicate, not a second copy.
    third = h.ingest()
    assert third.status is IngestionStatus.DUPLICATE
    assert len(h.store.list_resources("docs")) == 1


def test_retry_after_incomplete_rollback_heals_then_succeeds() -> None:
    h = _FlakyHarness()
    h.index.fail_index = True
    h.index.fail_remove = True
    h.store.fail_delete = True
    first = h.ingest()
    assert first.rollback_incomplete is True
    zombie_id = h.store.list_resources("docs")[0].resource_id

    # Providers recover; the retry repairs the remnant and then ingests.
    h.index.fail_index = False
    h.index.fail_remove = False
    h.store.fail_delete = False
    retry = h.ingest()

    assert retry.status is IngestionStatus.SUCCESS
    assert retry.resource is not None
    assert retry.resource.resource_id != zombie_id
    assert [r.resource_id for r in h.store.list_resources("docs")] == [
        retry.resource.resource_id
    ]
    assert h.retrieve_ids() == [retry.resource.resource_id]


def test_retry_while_providers_still_broken_fails_without_stacking_copies() -> None:
    h = _FlakyHarness()
    h.index.fail_index = True
    h.index.fail_remove = True
    h.store.fail_delete = True
    h.ingest()
    before = len(h.store.list_resources("docs"))

    second = h.ingest()
    assert second.status is IngestionStatus.FAILED
    assert second.rollback_incomplete is True
    assert second.error_category is KnowledgeErrorCategory.STORAGE_ERROR
    # No second copy was written on top of the remnant, and it is not
    # reported as a DUPLICATE of a failed resource.
    assert len(h.store.list_resources("docs")) == before
    assert h.retrieve_ids() == []


def test_quarantine_is_per_engine_instance_not_global() -> None:
    a = _FlakyHarness()
    a.index.fail_index = True
    a.store.fail_delete = True
    a.ingest()

    b = _FlakyHarness()
    assert b.ingest().status is IngestionStatus.SUCCESS
    assert len(b.retrieve_ids()) == 1


# --------------------------------------------------------------------- #
# Model invariants
# --------------------------------------------------------------------- #


def test_rollback_incomplete_only_valid_on_failed_results() -> None:
    with pytest.raises(ValidationError):
        IngestionResult(
            operation_id="op",
            principal=ALICE,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
            status=IngestionStatus.REJECTED,
            error_category=KnowledgeErrorCategory.EXTRACTION_ERROR,
            rollback_incomplete=True,
            created_at=NOW,
        )
