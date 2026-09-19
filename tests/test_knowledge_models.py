"""Model validation tests for sam.knowledge.models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from sam.knowledge.models import (
    ChunkLocation,
    DocumentChunk,
    DocumentMetadata,
    ExecutionOutcome,
    IngestionResult,
    IngestionStatus,
    KnowledgeAuditEvent,
    KnowledgeCollection,
    KnowledgeErrorCategory,
    KnowledgeOperation,
    KnowledgeResource,
    ParsedDocument,
    ParsedSegment,
    PermissionOutcomeSummary,
    RemovalResult,
    ResourceListResult,
    ResourceQueryResult,
    ResourceSourceKind,
    ResourceType,
    RetrievalOutcome,
    RetrievalQuery,
    new_id,
    sanitize_display_text,
    utc_now,
)
from sam.permissions.models import Principal, PrincipalKind, RiskLevel

NOW = datetime.now(UTC)
NAIVE = datetime(2024, 1, 1)
PRINCIPAL = Principal(kind=PrincipalKind.USER, id="alice")


def _resource(**overrides: Any) -> KnowledgeResource:
    fields: dict[str, Any] = dict(
        resource_id="r1",
        collection_id="docs",
        name="hello.txt",
        resource_type=ResourceType.TXT,
        source="hello.txt",
        source_kind=ResourceSourceKind.UPLOAD,
        size_bytes=10,
        checksum="a" * 64,
        metadata=DocumentMetadata(),
        chunk_count=1,
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return KnowledgeResource(**fields)


class TestUtcNowAndIds:
    def test_utc_now_is_tz_aware(self) -> None:
        assert utc_now().tzinfo is not None

    def test_new_id_is_unique(self) -> None:
        assert new_id() != new_id()

    def test_sanitize_display_text_strips_control_chars(self) -> None:
        assert sanitize_display_text("a\x00b\x1f c", max_length=10) == "ab c"

    def test_sanitize_display_text_none_stays_none(self) -> None:
        assert sanitize_display_text(None, max_length=10) is None

    def test_sanitize_display_text_blank_becomes_none(self) -> None:
        assert sanitize_display_text("   ", max_length=10) is None

    def test_sanitize_display_text_truncates(self) -> None:
        assert sanitize_display_text("abcdef", max_length=3) == "abc"


class TestKnowledgeCollection:
    def test_valid(self) -> None:
        c = KnowledgeCollection(
            collection_id="research", name="research", created_at=NOW
        )
        assert c.collection_id == "research"

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValidationError):
            KnowledgeCollection(collection_id="r", name="r", created_at=NAIVE)

    def test_blank_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            KnowledgeCollection(collection_id="   ", name="r", created_at=NOW)


class TestDocumentMetadata:
    def test_defaults(self) -> None:
        m = DocumentMetadata()
        assert m.title is None
        assert m.custom == {}

    def test_custom_bounded_entries(self) -> None:
        too_many = {f"k{i}": "v" for i in range(21)}
        with pytest.raises(ValidationError):
            DocumentMetadata(custom=too_many)

    def test_custom_cleans_control_chars(self) -> None:
        m = DocumentMetadata(custom={"k\x00ey": "va\x00lue"})
        assert m.custom == {"key": "value"}

    def test_custom_rejects_blank_key(self) -> None:
        with pytest.raises(ValidationError):
            DocumentMetadata(custom={"\x00": "value"})

    def test_title_sanitized(self) -> None:
        m = DocumentMetadata(title="  Deep Learning\x00  ")
        assert m.title == "Deep Learning"


class TestKnowledgeResource:
    def test_valid(self) -> None:
        resource = _resource()
        assert resource.resource_type is ResourceType.TXT

    def test_negative_size_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _resource(size_bytes=-1)

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _resource(created_at=NAIVE)

    def test_frozen(self) -> None:
        resource = _resource()
        with pytest.raises(ValidationError):
            resource.name = "other.txt"


class TestParsedSegmentAndDocument:
    def test_page_number_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            ParsedSegment(text="x", page_number=0)

    def test_has_extractable_text_false_for_blank_segments(self) -> None:
        doc = ParsedDocument(
            resource_type=ResourceType.TXT, segments=(ParsedSegment(text="   "),)
        )
        assert doc.has_extractable_text is False

    def test_has_extractable_text_true(self) -> None:
        doc = ParsedDocument(
            resource_type=ResourceType.TXT, segments=(ParsedSegment(text="hello"),)
        )
        assert doc.has_extractable_text is True

    def test_empty_segments_not_extractable(self) -> None:
        doc = ParsedDocument(resource_type=ResourceType.TXT, segments=())
        assert doc.has_extractable_text is False


class TestChunkLocation:
    def test_end_before_start_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChunkLocation(character_start=10, character_end=5)

    def test_equal_start_end_allowed(self) -> None:
        loc = ChunkLocation(character_start=5, character_end=5)
        assert loc.character_start == loc.character_end

    def test_all_none_allowed(self) -> None:
        loc = ChunkLocation()
        assert loc.page_number is None
        assert loc.character_start is None

    def test_negative_page_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChunkLocation(page_number=0)


class TestDocumentChunk:
    def test_valid(self) -> None:
        chunk = DocumentChunk(
            chunk_id="r1:000000",
            resource_id="r1",
            collection_id="docs",
            sequence_index=0,
            text="hello",
            location=ChunkLocation(),
            created_at=NOW,
        )
        assert chunk.sequence_index == 0

    def test_blank_text_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DocumentChunk(
                chunk_id="r1:000000",
                resource_id="r1",
                collection_id="docs",
                sequence_index=0,
                text="",
                location=ChunkLocation(),
                created_at=NOW,
            )

    def test_negative_sequence_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DocumentChunk(
                chunk_id="r1:000000",
                resource_id="r1",
                collection_id="docs",
                sequence_index=-1,
                text="hello",
                location=ChunkLocation(),
                created_at=NOW,
            )


class TestRetrievalQuery:
    def test_blank_query_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalQuery(query="   ")

    def test_top_k_bounds(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalQuery(query="x", top_k=0)
        with pytest.raises(ValidationError):
            RetrievalQuery(query="x", top_k=1000)

    def test_default_top_k(self) -> None:
        q = RetrievalQuery(query="x")
        assert q.top_k == 10

    def test_collection_id_none_allowed(self) -> None:
        q = RetrievalQuery(query="x", collection_id=None)
        assert q.collection_id is None


class TestIngestionResultInvariants:
    def test_success_requires_resource_and_chunks(self) -> None:
        with pytest.raises(ValidationError):
            IngestionResult(
                operation_id="op1",
                principal=PRINCIPAL,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                status=IngestionStatus.SUCCESS,
                created_at=NOW,
            )

    def test_success_with_resource_but_zero_chunks_rejected(self) -> None:
        with pytest.raises(ValidationError):
            IngestionResult(
                operation_id="op1",
                principal=PRINCIPAL,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                status=IngestionStatus.SUCCESS,
                resource=_resource(),
                chunk_count=0,
                created_at=NOW,
            )

    def test_success_with_error_category_rejected(self) -> None:
        with pytest.raises(ValidationError):
            IngestionResult(
                operation_id="op1",
                principal=PRINCIPAL,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                status=IngestionStatus.SUCCESS,
                resource=_resource(),
                chunk_count=1,
                error_category=KnowledgeErrorCategory.INTERNAL_ERROR,
                created_at=NOW,
            )

    def test_non_success_with_resource_rejected(self) -> None:
        with pytest.raises(ValidationError):
            IngestionResult(
                operation_id="op1",
                principal=PRINCIPAL,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                status=IngestionStatus.FAILED,
                resource=_resource(),
                error_category=KnowledgeErrorCategory.INTERNAL_ERROR,
                created_at=NOW,
            )

    def test_duplicate_requires_duplicate_of(self) -> None:
        with pytest.raises(ValidationError):
            IngestionResult(
                operation_id="op1",
                principal=PRINCIPAL,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                status=IngestionStatus.DUPLICATE,
                created_at=NOW,
            )

    def test_rejected_requires_error_category(self) -> None:
        with pytest.raises(ValidationError):
            IngestionResult(
                operation_id="op1",
                principal=PRINCIPAL,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                status=IngestionStatus.REJECTED,
                created_at=NOW,
            )

    def test_valid_success(self) -> None:
        result = IngestionResult(
            operation_id="op1",
            principal=PRINCIPAL,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
            status=IngestionStatus.SUCCESS,
            resource=_resource(),
            chunk_count=1,
            created_at=NOW,
        )
        assert result.status is IngestionStatus.SUCCESS

    def test_valid_duplicate(self) -> None:
        result = IngestionResult(
            operation_id="op1",
            principal=PRINCIPAL,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
            status=IngestionStatus.DUPLICATE,
            duplicate_of="r0",
            created_at=NOW,
        )
        assert result.duplicate_of == "r0"


@pytest.mark.parametrize(
    "model_cls",
    [ResourceQueryResult, ResourceListResult, RetrievalOutcome, RemovalResult],
)
class TestExecutionOutcomeInvariants:
    def _base_kwargs(self) -> dict[str, Any]:
        return dict(
            operation_id="op1",
            principal=PRINCIPAL,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
            created_at=NOW,
        )

    def test_failed_requires_error_category(self, model_cls: type) -> None:
        with pytest.raises(ValidationError):
            model_cls(**self._base_kwargs(), outcome=ExecutionOutcome.FAILED)

    def test_success_forbids_error_category(self, model_cls: type) -> None:
        with pytest.raises(ValidationError):
            model_cls(
                **self._base_kwargs(),
                outcome=ExecutionOutcome.SUCCESS,
                error_category=KnowledgeErrorCategory.INTERNAL_ERROR,
            )

    def test_success_with_no_category_ok(self, model_cls: type) -> None:
        kwargs = self._base_kwargs()
        extra: dict[str, Any] = {}
        if model_cls is RemovalResult:
            extra["removed"] = True
        model_cls(**kwargs, outcome=ExecutionOutcome.SUCCESS, **extra)


class TestRemovalResultInvariant:
    def test_success_requires_removed_true(self) -> None:
        with pytest.raises(ValidationError):
            RemovalResult(
                operation_id="op1",
                principal=PRINCIPAL,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
                outcome=ExecutionOutcome.SUCCESS,
                removed=False,
                created_at=NOW,
            )


class TestKnowledgeAuditEvent:
    def test_valid(self) -> None:
        event = KnowledgeAuditEvent(
            event_id="e1",
            occurred_at=NOW,
            operation_id="op1",
            principal=PRINCIPAL,
            collection_id="docs",
            operation=KnowledgeOperation.INGEST_RESOURCE,
            risk=RiskLevel.MEDIUM,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
        )
        assert event.operation is KnowledgeOperation.INGEST_RESOURCE

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValidationError):
            KnowledgeAuditEvent(
                event_id="e1",
                occurred_at=NAIVE,
                operation_id="op1",
                principal=PRINCIPAL,
                collection_id="docs",
                operation=KnowledgeOperation.INGEST_RESOURCE,
                risk=RiskLevel.MEDIUM,
                permission_outcome=PermissionOutcomeSummary.ALLOW,
            )

    def test_has_no_text_field(self) -> None:
        assert "text" not in KnowledgeAuditEvent.model_fields
        assert "content" not in KnowledgeAuditEvent.model_fields
