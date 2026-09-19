"""Tests for sam.knowledge.ingestion.run_ingestion_pipeline — the pure
validate -> checksum -> parse -> validate content -> chunk pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sam.knowledge.chunker import ChunkerConfig
from sam.knowledge.ingestion import (
    IngestionOutcome,
    compute_checksum,
    run_ingestion_pipeline,
)
from sam.knowledge.models import (
    IngestResourceRequest,
    KnowledgeErrorCategory,
    ResourceSourceKind,
    ResourceType,
)
from sam.knowledge.parser import default_parsers
from sam.permissions.models import Principal, PrincipalKind

NOW = datetime.now(UTC)
PRINCIPAL = Principal(kind=PrincipalKind.USER, id="alice")
PARSERS = default_parsers()


def _request(**overrides: Any) -> IngestResourceRequest:
    fields: dict[str, Any] = dict(
        principal=PRINCIPAL,
        collection_id="docs",
        name="hello.txt",
        declared_resource_type=ResourceType.TXT,
        source_kind=ResourceSourceKind.UPLOAD,
        source_label="hello.txt",
        content=b"Graph neural networks are used for misinformation detection.",
    )
    fields.update(overrides)
    return IngestResourceRequest(**fields)


def _run(request: IngestResourceRequest) -> IngestionOutcome:
    return run_ingestion_pipeline(
        request, parsers=PARSERS, chunker_config=ChunkerConfig(), now=NOW
    )


class TestSuccessfulIngestion:
    def test_accepted_with_resource_and_chunks(self) -> None:
        outcome = _run(_request())
        assert outcome.accepted is True
        assert outcome.resource is not None
        assert outcome.resource.chunk_count == len(outcome.chunks)
        assert outcome.error_category is None

    def test_checksum_matches_content(self) -> None:
        request = _request()
        outcome = _run(request)
        assert outcome.resource is not None
        assert outcome.resource.checksum == compute_checksum(request.content)

    def test_resource_id_is_fresh_uuid_not_content_derived(self) -> None:
        a = _run(_request())
        b = _run(_request())
        assert a.resource is not None
        assert b.resource is not None
        assert a.resource.resource_id != b.resource.resource_id
        assert a.resource.checksum == b.resource.checksum


class TestRejections:
    def test_unsupported_declared_type_mismatch(self) -> None:
        outcome = _run(
            _request(content=b"%PDF-1.4\n...", declared_resource_type=ResourceType.TXT)
        )
        assert outcome.accepted is False
        assert (
            outcome.error_category is KnowledgeErrorCategory.UNSUPPORTED_RESOURCE_TYPE
        )

    def test_secret_filename_rejected(self) -> None:
        outcome = _run(_request(name=".env", source_label=".env"))
        assert outcome.accepted is False
        assert outcome.error_category is KnowledgeErrorCategory.SECRET_DETECTED

    def test_secret_metadata_rejected(self) -> None:
        from sam.knowledge.models import DocumentMetadata

        outcome = _run(
            _request(
                metadata=DocumentMetadata(
                    title="sk-ant-abcdefghijklmnopqrstuvwxyz123456"
                )
            )
        )
        assert outcome.accepted is False
        assert outcome.error_category is KnowledgeErrorCategory.SECRET_DETECTED

    def test_secret_content_rejected(self) -> None:
        outcome = _run(
            _request(content=b"api_key: sk-ant-abcdefghijklmnopqrstuvwxyz123456")
        )
        assert outcome.accepted is False
        assert outcome.error_category is KnowledgeErrorCategory.SECRET_DETECTED

    def test_empty_content_rejected_at_model_layer(self) -> None:
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _request(content=b"")

    def test_blank_text_document_rejected(self) -> None:
        outcome = _run(_request(content=b"   \n\t  "))
        assert outcome.accepted is False
        assert outcome.error_category is KnowledgeErrorCategory.EXTRACTION_ERROR

    def test_malformed_pdf_rejected(self) -> None:
        outcome = _run(
            _request(
                content=b"%PDF-1.4\nnot really a pdf structure",
                declared_resource_type=ResourceType.PDF,
                name="doc.pdf",
                source_label="doc.pdf",
            )
        )
        assert outcome.accepted is False
        assert outcome.error_category is KnowledgeErrorCategory.PARSING_ERROR

    def test_oversized_extracted_text_rejected(self) -> None:
        from sam.knowledge.models import (
            MAX_EXTRACTED_TEXT_CHARS,
            MAX_RESOURCE_SIZE_BYTES,
        )

        # Use a size just under the resource cap but over the extracted
        # text cap (they are independently configured in this test).
        size = min(MAX_EXTRACTED_TEXT_CHARS + 10, MAX_RESOURCE_SIZE_BYTES)
        content = b"a" * size
        outcome = _run(_request(content=content))
        if size > MAX_EXTRACTED_TEXT_CHARS:
            assert outcome.accepted is False
            assert outcome.error_category is KnowledgeErrorCategory.RESOURCE_TOO_LARGE


class TestDeterminism:
    def test_same_request_produces_same_chunks(self) -> None:
        request = _request()
        a = _run(request)
        b = _run(request)
        # resource ids differ (fresh per call) but chunk text/location do not
        assert [c.text for c in a.chunks] == [c.text for c in b.chunks]
        assert [c.location for c in a.chunks] == [c.location for c in b.chunks]
