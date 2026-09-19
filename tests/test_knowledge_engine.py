"""Tests for sam.knowledge.engine.KnowledgeEngine — the sole orchestration
entry point tying policy, the Permission Engine, storage, the index, and
retrieval together."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sam.knowledge.audit import InMemoryKnowledgeAuditSink
from sam.knowledge.engine import KnowledgeEngine
from sam.knowledge.index import InMemoryLexicalIndex
from sam.knowledge.models import (
    ExecutionOutcome,
    GetResourceRequest,
    IngestionStatus,
    IngestResourceRequest,
    KnowledgeErrorCategory,
    KnowledgeResource,
    ListResourcesRequest,
    PermissionOutcomeSummary,
    RemoveResourceRequest,
    ResourceSourceKind,
    ResourceType,
    RetrievalQuery,
    RetrieveRequest,
)
from sam.knowledge.store import InMemoryKnowledgeStore
from sam.permissions.audit import InMemoryAuditSink
from sam.permissions.confirmation import InMemoryConfirmationProvider
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    PermissionAction,
    PermissionGrant,
    PermissionResource,
    PermissionScope,
    Principal,
    PrincipalKind,
)
from sam.permissions.store import InMemoryPermissionStore


def _res(result: Any) -> KnowledgeResource:
    resource = result.resource
    assert resource is not None
    return cast(KnowledgeResource, resource)


def _cid(result: Any) -> str:
    confirmation_id = result.confirmation_id
    assert confirmation_id is not None
    return cast(str, confirmation_id)


NOW = datetime.now(UTC)
ALICE = Principal(kind=PrincipalKind.USER, id="alice")
BOB = Principal(kind=PrincipalKind.USER, id="bob")


class _Harness:
    def __init__(self) -> None:
        self.pstore = InMemoryPermissionStore()
        self.confirmations = InMemoryConfirmationProvider()
        self.pengine = PermissionEngine(
            store=self.pstore,
            confirmation_provider=self.confirmations,
            audit_sink=InMemoryAuditSink(),
        )
        self.store = InMemoryKnowledgeStore()
        self.index = InMemoryLexicalIndex()
        self.audit = InMemoryKnowledgeAuditSink()
        self.engine = KnowledgeEngine(
            store=self.store,
            index=self.index,
            permission_engine=self.pengine,
            audit_sink=self.audit,
        )

    def grant(
        self,
        principal: Principal,
        action: PermissionAction,
        scope: str,
        *,
        path: bool = False,
    ) -> None:
        self.pstore.create_grant(
            PermissionGrant(
                grant_id=f"g-{principal.id}-{scope}-{action}",
                principal=principal,
                resource=PermissionResource.KNOWLEDGE,
                action=action,
                scope=(
                    PermissionScope.from_path(scope)
                    if path
                    else PermissionScope.identifier(scope)
                ),
                created_at=NOW,
                updated_at=NOW,
            )
        )

    def grant_full_access(self, principal: Principal, collection_id: str) -> None:
        self.grant(principal, PermissionAction.WRITE, f"{collection_id}:ingest")
        self.grant(principal, PermissionAction.READ, f"{collection_id}:list")
        self.grant(principal, PermissionAction.READ, f"{collection_id}:retrieve")

    def approve_pending(self, confirmation_id: str) -> None:
        self.confirmations.decide(confirmation_id, approved=True, now=NOW)


def _ingest_request(
    principal: Principal = ALICE, collection_id: str = "docs", **overrides: Any
) -> IngestResourceRequest:
    fields: dict[str, Any] = dict(
        principal=principal,
        collection_id=collection_id,
        name="hello.txt",
        declared_resource_type=ResourceType.TXT,
        source_kind=ResourceSourceKind.UPLOAD,
        source_label="hello.txt",
        content=b"Graph neural networks are used for misinformation detection.",
    )
    fields.update(overrides)
    return IngestResourceRequest(**fields)


class TestIngest:
    def test_success(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        result = h.engine.ingest(_ingest_request())
        assert result.status is IngestionStatus.SUCCESS
        assert result.permission_outcome is PermissionOutcomeSummary.ALLOW
        assert result.resource is not None
        assert result.chunk_count >= 1

    def test_denied_without_grant(self) -> None:
        h = _Harness()
        result = h.engine.ingest(_ingest_request())
        assert result.status is IngestionStatus.FAILED
        assert result.permission_outcome is PermissionOutcomeSummary.DENY
        assert result.error_category is KnowledgeErrorCategory.PERMISSION_DENIED
        assert h.store.list_resources("docs") == ()

    def test_duplicate_detected_by_checksum(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        request = _ingest_request()
        first = h.engine.ingest(request)
        second = h.engine.ingest(request)
        assert second.status is IngestionStatus.DUPLICATE
        assert second.duplicate_of == _res(first).resource_id
        assert len(h.store.list_resources("docs")) == 1

    def test_rejected_pipeline_failure_persists_nothing(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        result = h.engine.ingest(_ingest_request(content=b"   "))
        assert result.status is IngestionStatus.REJECTED
        assert result.error_category is KnowledgeErrorCategory.EXTRACTION_ERROR
        assert h.store.list_resources("docs") == ()

    def test_indexing_failure_rolls_back_store(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")

        class _BrokenIndex(InMemoryLexicalIndex):
            def index_chunks(self, chunks: object) -> None:
                raise RuntimeError("index unavailable")

        h.engine = KnowledgeEngine(
            store=h.store,
            index=_BrokenIndex(),
            permission_engine=h.pengine,
            audit_sink=h.audit,
        )
        result = h.engine.ingest(_ingest_request())
        assert result.status is IngestionStatus.FAILED
        assert result.error_category is KnowledgeErrorCategory.INDEXING_ERROR
        assert h.store.list_resources("docs") == ()

    def test_unexpected_permission_engine_failure_fails_closed(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")

        class _BrokenEngine:
            def evaluate(self, *args: object, **kwargs: object) -> object:
                raise RuntimeError("boom")

        h.engine = KnowledgeEngine(
            store=h.store,
            index=h.index,
            permission_engine=cast(Any, _BrokenEngine()),
            audit_sink=h.audit,
        )
        result = h.engine.ingest(_ingest_request())
        assert result.status is IngestionStatus.FAILED
        assert result.error_category is KnowledgeErrorCategory.INTERNAL_ERROR

    def test_audit_never_contains_document_text(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        h.engine.ingest(_ingest_request())
        for event in h.audit.list_events():
            dumped = event.model_dump()
            assert "text" not in dumped
            assert "Graph neural networks" not in str(dumped)


class TestGetResource:
    def test_success(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        ingested = h.engine.ingest(_ingest_request())
        rid = _res(ingested).resource_id
        h.grant(ALICE, PermissionAction.READ, f"docs/{rid}", path=True)

        result = h.engine.get_resource(
            GetResourceRequest(principal=ALICE, collection_id="docs", resource_id=rid)
        )
        assert result.outcome is ExecutionOutcome.SUCCESS
        assert _res(result).resource_id == rid

    def test_not_found(self) -> None:
        h = _Harness()
        h.grant(ALICE, PermissionAction.READ, "docs/missing", path=True)
        result = h.engine.get_resource(
            GetResourceRequest(
                principal=ALICE, collection_id="docs", resource_id="missing"
            )
        )
        assert result.outcome is ExecutionOutcome.FAILED
        assert result.error_category is KnowledgeErrorCategory.RESOURCE_NOT_FOUND

    def test_denied_without_grant(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        ingested = h.engine.ingest(_ingest_request())
        rid = _res(ingested).resource_id
        # No per-resource READ grant issued.
        result = h.engine.get_resource(
            GetResourceRequest(principal=ALICE, collection_id="docs", resource_id=rid)
        )
        assert result.outcome is ExecutionOutcome.FAILED
        assert result.permission_outcome is PermissionOutcomeSummary.DENY

    def test_wrong_scope_grant_does_not_authorize_other_resource(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        r1 = _res(h.engine.ingest(_ingest_request())).resource_id
        r2 = _res(
            h.engine.ingest(_ingest_request(content=b"different content entirely here"))
        ).resource_id
        h.grant(ALICE, PermissionAction.READ, f"docs/{r1}", path=True)

        result = h.engine.get_resource(
            GetResourceRequest(principal=ALICE, collection_id="docs", resource_id=r2)
        )
        assert result.permission_outcome is PermissionOutcomeSummary.DENY


class TestListResources:
    def test_success(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        h.engine.ingest(_ingest_request())
        result = h.engine.list_resources(
            ListResourcesRequest(principal=ALICE, collection_id="docs")
        )
        assert result.outcome is ExecutionOutcome.SUCCESS
        assert len(result.resources) == 1

    def test_denied_without_grant(self) -> None:
        h = _Harness()
        result = h.engine.list_resources(
            ListResourcesRequest(principal=ALICE, collection_id="docs")
        )
        assert result.permission_outcome is PermissionOutcomeSummary.DENY

    def test_cross_collection_isolation(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "research")
        h.grant_full_access(ALICE, "personal")
        h.engine.ingest(_ingest_request(collection_id="research"))
        h.engine.ingest(
            _ingest_request(
                collection_id="personal", content=b"unrelated personal note text"
            )
        )

        result = h.engine.list_resources(
            ListResourcesRequest(principal=ALICE, collection_id="research")
        )
        assert len(result.resources) == 1
        assert result.resources[0].collection_id == "research"


class TestRetrieve:
    def test_success_with_citation(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        h.engine.ingest(_ingest_request())
        result = h.engine.retrieve(
            RetrieveRequest(
                principal=ALICE,
                query=RetrievalQuery(
                    query="graph neural networks", collection_id="docs"
                ),
            )
        )
        assert result.outcome is ExecutionOutcome.SUCCESS
        assert len(result.results) == 1
        assert result.results[0].resource.name == "hello.txt"

    def test_denied_without_grant(self) -> None:
        h = _Harness()
        result = h.engine.retrieve(
            RetrieveRequest(
                principal=ALICE, query=RetrievalQuery(query="x", collection_id="docs")
            )
        )
        assert result.permission_outcome is PermissionOutcomeSummary.DENY

    def test_global_retrieval_requires_its_own_grant(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")  # collection-scoped only
        result = h.engine.retrieve(
            RetrieveRequest(principal=ALICE, query=RetrievalQuery(query="graph"))
        )
        assert result.permission_outcome is PermissionOutcomeSummary.DENY

    def test_cross_resource_retrieval_keeps_identity(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        h.engine.ingest(_ingest_request(name="a.txt", source_label="a.txt"))
        h.engine.ingest(
            _ingest_request(
                name="b.txt",
                source_label="b.txt",
                content=b"graph misinformation detection paper",
            )
        )
        result = h.engine.retrieve(
            RetrieveRequest(
                principal=ALICE,
                query=RetrievalQuery(
                    query="graph misinformation", collection_id="docs"
                ),
            )
        )
        names = {r.resource.name for r in result.results}
        assert "b.txt" in names


class TestRemoveResource:
    def test_requires_confirmation(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        rid = _res(h.engine.ingest(_ingest_request())).resource_id
        h.grant(ALICE, PermissionAction.DELETE, f"docs/{rid}", path=True)

        result = h.engine.remove_resource(
            RemoveResourceRequest(
                principal=ALICE, collection_id="docs", resource_id=rid
            )
        )
        assert result.outcome is ExecutionOutcome.FAILED
        assert result.permission_outcome is PermissionOutcomeSummary.CONFIRM_REQUIRED
        assert result.confirmation_id is not None
        # DELETE must not have happened.
        assert h.store.get_resource("docs", rid) is not None

    def test_succeeds_after_confirmation(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        rid = _res(h.engine.ingest(_ingest_request())).resource_id
        h.grant(ALICE, PermissionAction.DELETE, f"docs/{rid}", path=True)

        pending = h.engine.remove_resource(
            RemoveResourceRequest(
                principal=ALICE, collection_id="docs", resource_id=rid
            )
        )
        h.approve_pending(_cid(pending))
        result = h.engine.remove_resource(
            RemoveResourceRequest(
                principal=ALICE, collection_id="docs", resource_id=rid
            ),
            confirmation_id=pending.confirmation_id,
        )
        assert result.outcome is ExecutionOutcome.SUCCESS
        assert result.removed is True
        assert h.store.get_resource("docs", rid) is None
        assert (
            h.index.search(collection_id=None, resource_id=rid, query="graph", limit=10)
            == ()
        )

    def test_confirmation_cannot_be_replayed(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        rid = _res(h.engine.ingest(_ingest_request())).resource_id
        h.grant(ALICE, PermissionAction.DELETE, f"docs/{rid}", path=True)

        pending = h.engine.remove_resource(
            RemoveResourceRequest(
                principal=ALICE, collection_id="docs", resource_id=rid
            )
        )
        h.approve_pending(_cid(pending))
        h.engine.remove_resource(
            RemoveResourceRequest(
                principal=ALICE, collection_id="docs", resource_id=rid
            ),
            confirmation_id=pending.confirmation_id,
        )
        # Re-ingest a new resource then try to reuse the same (consumed)
        # confirmation id against it.
        rid2 = _res(
            h.engine.ingest(_ingest_request(content=b"another distinct document body"))
        ).resource_id
        h.grant(ALICE, PermissionAction.DELETE, f"docs/{rid2}", path=True)
        replay = h.engine.remove_resource(
            RemoveResourceRequest(
                principal=ALICE, collection_id="docs", resource_id=rid2
            ),
            confirmation_id=pending.confirmation_id,
        )
        assert replay.permission_outcome is PermissionOutcomeSummary.DENY

    def test_remove_not_found_after_confirmation(self) -> None:
        # DELETE is HIGH risk and always requires confirmation, even for
        # a resource that turns out not to exist — the permission gate
        # runs before any store lookup (see KnowledgeEngine._gate).
        h = _Harness()
        h.grant(ALICE, PermissionAction.DELETE, "docs/missing", path=True)
        pending = h.engine.remove_resource(
            RemoveResourceRequest(
                principal=ALICE, collection_id="docs", resource_id="missing"
            )
        )
        assert pending.error_category is KnowledgeErrorCategory.CONFIRMATION_REQUIRED
        h.approve_pending(_cid(pending))
        result = h.engine.remove_resource(
            RemoveResourceRequest(
                principal=ALICE, collection_id="docs", resource_id="missing"
            ),
            confirmation_id=pending.confirmation_id,
        )
        assert result.error_category is KnowledgeErrorCategory.RESOURCE_NOT_FOUND

    def test_denied_without_grant(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        rid = _res(h.engine.ingest(_ingest_request())).resource_id
        result = h.engine.remove_resource(
            RemoveResourceRequest(
                principal=ALICE, collection_id="docs", resource_id=rid
            )
        )
        assert result.permission_outcome is PermissionOutcomeSummary.DENY
        assert h.store.get_resource("docs", rid) is not None

    def test_revoked_grant_denies(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        rid = _res(h.engine.ingest(_ingest_request())).resource_id
        h.grant(ALICE, PermissionAction.DELETE, f"docs/{rid}", path=True)
        grant_id = f"g-{ALICE.id}-docs/{rid}-{PermissionAction.DELETE}"
        h.pstore.revoke_grant(grant_id, now=NOW)

        result = h.engine.remove_resource(
            RemoveResourceRequest(
                principal=ALICE, collection_id="docs", resource_id=rid
            )
        )
        assert result.permission_outcome is PermissionOutcomeSummary.DENY


class TestCrossPrincipalIsolation:
    def test_bobs_grant_does_not_authorize_alice(self) -> None:
        h = _Harness()
        h.grant_full_access(BOB, "docs")
        result = h.engine.ingest(_ingest_request(principal=ALICE))
        assert result.permission_outcome is PermissionOutcomeSummary.DENY


class TestMemoryIsolation:
    def test_knowledge_engine_never_imports_memory_engine(self) -> None:
        import sam.knowledge.engine as engine_module

        source = engine_module.__file__
        assert source is not None
        with open(source, encoding="utf-8") as handle:
            contents = handle.read()
        assert "sam.memory.engine" not in contents
        assert "MemoryEngine" not in contents

    def test_ingest_does_not_touch_any_memory_store(self) -> None:
        h = _Harness()
        h.grant_full_access(ALICE, "docs")
        # If KnowledgeEngine ever called into a memory store, this would
        # be observable only via source inspection (there is no shared
        # collaborator to spy on) — see the module-boundary test above
        # for the structural guarantee. This test documents the intent.
        result = h.engine.ingest(_ingest_request())
        assert result.status is IngestionStatus.SUCCESS
