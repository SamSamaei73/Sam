"""Tests for sam.knowledge.policy — the KnowledgeOperation -> PermissionRequest
mapping. Makes no authorization decision; only verifies the pure mapping."""

from __future__ import annotations

from typing import Any

from sam.knowledge.models import (
    GetResourceRequest,
    IngestResourceRequest,
    KnowledgeOperation,
    ListResourcesRequest,
    RemoveResourceRequest,
    ResourceSourceKind,
    ResourceType,
    RetrievalQuery,
    RetrieveRequest,
)
from sam.knowledge.policy import (
    GLOBAL_COLLECTION_ID,
    build_request,
    collection_for,
    operation_for,
    risk_for,
)
from sam.permissions.models import (
    PermissionAction,
    PermissionResource,
    Principal,
    PrincipalKind,
    RiskLevel,
)

PRINCIPAL = Principal(kind=PrincipalKind.USER, id="alice")


def _ingest_request(**overrides: Any) -> IngestResourceRequest:
    fields: dict[str, Any] = dict(
        principal=PRINCIPAL,
        collection_id="docs",
        name="hello.txt",
        declared_resource_type=ResourceType.TXT,
        source_kind=ResourceSourceKind.UPLOAD,
        source_label="hello.txt",
        content=b"hello world",
    )
    fields.update(overrides)
    return IngestResourceRequest(**fields)


class TestOperationFor:
    def test_ingest(self) -> None:
        assert operation_for(_ingest_request()) is KnowledgeOperation.INGEST_RESOURCE

    def test_get(self) -> None:
        req = GetResourceRequest(
            principal=PRINCIPAL, collection_id="docs", resource_id="r1"
        )
        assert operation_for(req) is KnowledgeOperation.GET_RESOURCE

    def test_list(self) -> None:
        req = ListResourcesRequest(principal=PRINCIPAL, collection_id="docs")
        assert operation_for(req) is KnowledgeOperation.LIST_RESOURCES

    def test_retrieve(self) -> None:
        req = RetrieveRequest(principal=PRINCIPAL, query=RetrievalQuery(query="x"))
        assert operation_for(req) is KnowledgeOperation.RETRIEVE

    def test_remove(self) -> None:
        req = RemoveResourceRequest(
            principal=PRINCIPAL, collection_id="docs", resource_id="r1"
        )
        assert operation_for(req) is KnowledgeOperation.REMOVE_RESOURCE


class TestRiskFor:
    def test_ingest_is_write_medium(self) -> None:
        assert risk_for(KnowledgeOperation.INGEST_RESOURCE) is RiskLevel.MEDIUM

    def test_get_is_read_low(self) -> None:
        assert risk_for(KnowledgeOperation.GET_RESOURCE) is RiskLevel.LOW

    def test_list_is_read_low(self) -> None:
        assert risk_for(KnowledgeOperation.LIST_RESOURCES) is RiskLevel.LOW

    def test_retrieve_is_read_low(self) -> None:
        assert risk_for(KnowledgeOperation.RETRIEVE) is RiskLevel.LOW

    def test_remove_is_delete_high(self) -> None:
        assert risk_for(KnowledgeOperation.REMOVE_RESOURCE) is RiskLevel.HIGH


class TestBuildRequest:
    def test_ingest_maps_to_knowledge_write(self) -> None:
        req = build_request(_ingest_request())
        assert req.resource is PermissionResource.KNOWLEDGE
        assert req.action is PermissionAction.WRITE
        assert req.scope.as_text() == "docs:ingest"

    def test_get_scopes_to_exact_resource(self) -> None:
        req = build_request(
            GetResourceRequest(
                principal=PRINCIPAL, collection_id="docs", resource_id="r1"
            )
        )
        assert req.action is PermissionAction.READ
        assert req.scope.as_text() == "docs/r1"

    def test_remove_scopes_to_exact_resource_and_delete(self) -> None:
        req = build_request(
            RemoveResourceRequest(
                principal=PRINCIPAL, collection_id="docs", resource_id="r1"
            )
        )
        assert req.action is PermissionAction.DELETE
        assert req.scope.as_text() == "docs/r1"

    def test_different_resource_ids_produce_different_scopes(self) -> None:
        req_a = build_request(
            GetResourceRequest(
                principal=PRINCIPAL, collection_id="docs", resource_id="r1"
            )
        )
        req_b = build_request(
            GetResourceRequest(
                principal=PRINCIPAL, collection_id="docs", resource_id="r2"
            )
        )
        assert req_a.scope != req_b.scope
        assert not req_a.scope.contains(req_b.scope)
        assert not req_b.scope.contains(req_a.scope)

    def test_different_collections_produce_different_scopes(self) -> None:
        req_a = build_request(
            ListResourcesRequest(principal=PRINCIPAL, collection_id="docs")
        )
        req_b = build_request(
            ListResourcesRequest(principal=PRINCIPAL, collection_id="research")
        )
        assert req_a.scope != req_b.scope

    def test_retrieve_with_collection_scopes_to_it(self) -> None:
        req = build_request(
            RetrieveRequest(
                principal=PRINCIPAL,
                query=RetrievalQuery(query="x", collection_id="docs"),
            )
        )
        assert req.scope.as_text() == "docs:retrieve"

    def test_retrieve_without_collection_uses_global_scope(self) -> None:
        req = build_request(
            RetrieveRequest(principal=PRINCIPAL, query=RetrievalQuery(query="x"))
        )
        assert req.scope.as_text() == f"{GLOBAL_COLLECTION_ID}:retrieve"

    def test_global_retrieve_scope_distinct_from_any_real_collection(self) -> None:
        global_req = build_request(
            RetrieveRequest(principal=PRINCIPAL, query=RetrievalQuery(query="x"))
        )
        docs_req = build_request(
            RetrieveRequest(
                principal=PRINCIPAL,
                query=RetrievalQuery(query="x", collection_id="docs"),
            )
        )
        assert global_req.scope != docs_req.scope
        assert not docs_req.scope.contains(global_req.scope)
        assert not global_req.scope.contains(docs_req.scope)

    def test_reason_passed_through(self) -> None:
        req = build_request(_ingest_request(reason="learning material"))
        assert req.reason == "learning material"


class TestCollectionFor:
    def test_ingest(self) -> None:
        assert collection_for(_ingest_request()) == "docs"

    def test_retrieve_global(self) -> None:
        req = RetrieveRequest(principal=PRINCIPAL, query=RetrievalQuery(query="x"))
        assert collection_for(req) == GLOBAL_COLLECTION_ID

    def test_retrieve_scoped(self) -> None:
        req = RetrieveRequest(
            principal=PRINCIPAL,
            query=RetrievalQuery(query="x", collection_id="research"),
        )
        assert collection_for(req) == "research"


class TestEveryOperationMapped:
    def test_all_operations_have_a_mapping(self) -> None:
        for operation in KnowledgeOperation:
            risk_for(operation)  # must not raise
