"""Adversarial / security tests for the Knowledge Layer.

Numbered to trace directly to the Phase 7 task's Section 31 "Adversarial
Tests" list and Section 43 "Final Security Review" questions. The goal
throughout: prove untrusted documents can never become an execution
path, never bypass the Permission Engine, never leak into Memory, and
never leak secret content into storage, the index, or audit.
"""

from __future__ import annotations

import inspect
import json
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from sam.knowledge.audit import InMemoryKnowledgeAuditSink
from sam.knowledge.chunker import ChunkerConfig
from sam.knowledge.engine import KnowledgeEngine
from sam.knowledge.index import InMemoryLexicalIndex
from sam.knowledge.ingestion import run_ingestion_pipeline
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
from sam.knowledge.parser import PDFParser, default_parsers
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
PARSERS = default_parsers()


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
        expires_at: datetime | None = None,
    ) -> None:
        self.pstore.create_grant(
            PermissionGrant(
                grant_id=f"g-{principal.id}-{scope}-{action}-{expires_at}",
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
                expires_at=expires_at,
            )
        )

    def grant_full_access(self, principal: Principal, collection_id: str) -> None:
        self.grant(principal, PermissionAction.WRITE, f"{collection_id}:ingest")
        self.grant(principal, PermissionAction.READ, f"{collection_id}:list")
        self.grant(principal, PermissionAction.READ, f"{collection_id}:retrieve")


def _ingest(
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


# --------------------------------------------------------------------- #
# 1-2: malicious / null-byte filenames
# --------------------------------------------------------------------- #


def test_01_malicious_filename_does_not_escape_collection_isolation() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    result = h.engine.ingest(
        _ingest(name="../../../etc/passwd", source_label="../../../etc/passwd")
    )
    assert result.status is IngestionStatus.SUCCESS
    assert _res(result).collection_id == "docs"
    # Still only visible through the "docs" collection, nothing escaped.
    listing = h.engine.list_resources(
        ListResourcesRequest(principal=ALICE, collection_id="docs")
    )
    assert len(listing.resources) == 1


def test_02_null_byte_in_filename_is_stripped_not_crashing() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    result = h.engine.ingest(_ingest(name="evil\x00.txt", source_label="evil\x00.txt"))
    assert result.status is IngestionStatus.SUCCESS
    assert "\x00" not in _res(result).name
    assert "\x00" not in _res(result).source


# --------------------------------------------------------------------- #
# 3: fake extension (declared type does not match content)
# --------------------------------------------------------------------- #


def test_03_fake_extension_declared_txt_actual_pdf_rejected() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    result = h.engine.ingest(
        _ingest(
            name="notes.txt",
            source_label="notes.txt",
            content=b"%PDF-1.4\nreal pdf bytes here",
        )
    )
    assert result.status is IngestionStatus.REJECTED
    assert result.error_category is KnowledgeErrorCategory.UNSUPPORTED_RESOURCE_TYPE


# --------------------------------------------------------------------- #
# 4-5: malformed / oversized PDF
# --------------------------------------------------------------------- #


def test_04_malformed_pdf_never_crashes_the_boundary() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    result = h.engine.ingest(
        _ingest(
            name="broken.pdf",
            source_label="broken.pdf",
            declared_resource_type=ResourceType.PDF,
            content=b"%PDF-1.4\ntotally corrupt structure with no objects",
        )
    )
    assert result.status is IngestionStatus.REJECTED
    assert result.error_category is KnowledgeErrorCategory.PARSING_ERROR


def test_05_oversized_content_rejected_at_the_model_boundary() -> None:
    from pydantic import ValidationError

    from sam.knowledge.models import MAX_RESOURCE_SIZE_BYTES

    with pytest.raises(ValidationError):
        _ingest(content=b"a" * (MAX_RESOURCE_SIZE_BYTES + 1))


# --------------------------------------------------------------------- #
# 6: decompression bomb (pathological PDF)
# --------------------------------------------------------------------- #


def test_06_pdf_decompression_bomb_rejected_end_to_end() -> None:
    huge = b"0" * 30_000_000
    compressed = zlib.compress(huge, 9)
    body = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << >> "
        b"/MediaBox [0 0 612 792] >>",
        4: b"<< /Filter /FlateDecode /Length "
        + str(len(compressed)).encode()
        + b" >>\nstream\n"
        + compressed
        + b"\nendstream",
    }
    out = bytearray(b"%PDF-1.4\n")
    for num in sorted(body):
        out += f"{num} 0 obj\n".encode() + body[num] + b"\nendobj\n"
    out += b"trailer << /Root 1 0 R >>\n%%EOF"
    assert len(bytes(out)) < 1_000_000  # tiny on the wire

    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    result = h.engine.ingest(
        _ingest(
            name="bomb.pdf",
            source_label="bomb.pdf",
            declared_resource_type=ResourceType.PDF,
            content=bytes(out),
        )
    )
    assert result.status is IngestionStatus.REJECTED
    assert result.error_category in (
        KnowledgeErrorCategory.EXTRACTION_ERROR,
        KnowledgeErrorCategory.PARSING_ERROR,
    )


# --------------------------------------------------------------------- #
# 7-8: huge JSON nesting / huge CSV field, at the pipeline level
# --------------------------------------------------------------------- #


def test_07_huge_json_nesting_rejected_via_ingestion_pipeline() -> None:
    nested = ("[" * 500 + "1" + "]" * 500).encode()
    outcome = run_ingestion_pipeline(
        _ingest(
            declared_resource_type=ResourceType.JSON,
            name="deep.json",
            source_label="deep.json",
            content=nested,
        ),
        parsers=PARSERS,
        chunker_config=ChunkerConfig(),
        now=NOW,
    )
    assert outcome.accepted is False
    assert outcome.error_category is KnowledgeErrorCategory.PARSING_ERROR


def test_08_huge_csv_field_rejected_via_ingestion_pipeline() -> None:
    huge_field = "x" * 500_000
    content = f"col\n{huge_field}\n".encode()
    outcome = run_ingestion_pipeline(
        _ingest(
            declared_resource_type=ResourceType.CSV,
            name="huge.csv",
            source_label="huge.csv",
            content=content,
        ),
        parsers=PARSERS,
        chunker_config=ChunkerConfig(),
        now=NOW,
    )
    assert outcome.accepted is False
    assert outcome.error_category in (
        KnowledgeErrorCategory.RESOURCE_TOO_LARGE,
        KnowledgeErrorCategory.PARSING_ERROR,
    )


# --------------------------------------------------------------------- #
# 9-10: secret-looking document / metadata
# --------------------------------------------------------------------- #


def test_09_secret_looking_document_rejected_and_never_stored() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    result = h.engine.ingest(
        _ingest(content=b"api_key: sk-ant-abcdefghijklmnopqrstuvwxyz123456")
    )
    assert result.status is IngestionStatus.REJECTED
    assert result.error_category is KnowledgeErrorCategory.SECRET_DETECTED
    assert h.store.list_resources("docs") == ()
    assert (
        h.index.search(collection_id="docs", resource_id=None, query="sk-ant", limit=10)
        == ()
    )


def test_10_secret_looking_metadata_rejected() -> None:
    from sam.knowledge.models import DocumentMetadata

    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    result = h.engine.ingest(
        _ingest(metadata=DocumentMetadata(custom={"note": "password is hunter2xyz123"}))
    )
    assert result.status is IngestionStatus.REJECTED
    assert result.error_category is KnowledgeErrorCategory.SECRET_DETECTED


def test_09b_dotenv_filename_rejected_regardless_of_declared_type() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    result = h.engine.ingest(
        _ingest(name=".env", source_label=".env", content=b"DATABASE_URL=postgres://x")
    )
    assert result.status is IngestionStatus.REJECTED
    assert result.error_category is KnowledgeErrorCategory.SECRET_DETECTED


# --------------------------------------------------------------------- #
# 11: Unicode edge cases
# --------------------------------------------------------------------- #


def test_11_unicode_edge_cases_do_not_crash_chunking_or_offsets() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    tricky = "é​‮\U0001f600 " * 200  # combining, ZWSP, RLO, emoji
    result = h.engine.ingest(_ingest(content=tricky.encode("utf-8")))
    assert result.status is IngestionStatus.SUCCESS
    for chunk in h.store.get_chunks(_res(result).resource_id):
        if (
            chunk.location.character_start is not None
            and chunk.location.character_end is not None
        ):
            assert chunk.location.character_end >= chunk.location.character_start


# --------------------------------------------------------------------- #
# 12: duplicate resources
# --------------------------------------------------------------------- #


def test_12_duplicate_upload_does_not_corrupt_the_store() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    request = _ingest()
    first = h.engine.ingest(request)
    for _ in range(5):
        again = h.engine.ingest(request)
        assert again.status is IngestionStatus.DUPLICATE
        assert again.duplicate_of == _res(first).resource_id
    assert len(h.store.list_resources("docs")) == 1


# --------------------------------------------------------------------- #
# 13-14: cross-collection retrieval / cross-resource leakage
# --------------------------------------------------------------------- #


def test_13_cross_collection_retrieval_isolated_without_global_grant() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "research")
    h.grant_full_access(ALICE, "personal")
    h.engine.ingest(
        _ingest(collection_id="research", content=b"graph neural network paper text")
    )
    h.engine.ingest(
        _ingest(collection_id="personal", content=b"graph neural network private note")
    )

    result = h.engine.retrieve(
        RetrieveRequest(
            principal=ALICE,
            query=RetrievalQuery(query="graph neural", collection_id="research"),
        )
    )
    assert result.outcome is ExecutionOutcome.SUCCESS
    assert all(r.resource.collection_id == "research" for r in result.results)


def test_14_cross_resource_leakage_chunks_always_match_their_own_resource() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    h.engine.ingest(
        _ingest(
            name="a.txt",
            source_label="a.txt",
            content=b"graph misinformation paper alpha",
        )
    )
    h.engine.ingest(
        _ingest(
            name="b.txt",
            source_label="b.txt",
            content=b"graph misinformation paper beta",
        )
    )

    result = h.engine.retrieve(
        RetrieveRequest(
            principal=ALICE,
            query=RetrievalQuery(query="graph misinformation", collection_id="docs"),
        )
    )
    for item in result.results:
        assert item.chunk.resource_id == item.resource.resource_id
        assert item.chunk.collection_id == item.resource.collection_id


# --------------------------------------------------------------------- #
# 15: permission bypass — DENY must never reach store/index for any op
# --------------------------------------------------------------------- #


def test_15_denied_ingest_never_touches_store_or_index() -> None:
    h = _Harness()
    h.engine.ingest(_ingest())
    assert h.store.list_resources("docs") == ()
    assert (
        h.index.search(collection_id=None, resource_id=None, query="graph", limit=10)
        == ()
    )


def test_15b_denied_retrieve_never_touches_index() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    h.engine.ingest(_ingest())
    # Retrieve as Bob, who has no grants at all.
    result = h.engine.retrieve(
        RetrieveRequest(
            principal=BOB, query=RetrievalQuery(query="graph", collection_id="docs")
        )
    )
    assert result.permission_outcome is PermissionOutcomeSummary.DENY
    assert result.results == ()


def test_15c_llm_cannot_self_authorize_no_approval_field_exists() -> None:
    for name in ("IngestResourceRequest", "RetrieveRequest", "RemoveResourceRequest"):
        import sam.knowledge.models as models_module

        model = getattr(models_module, name)
        fields = set(model.model_fields)
        assert "approved" not in fields
        assert "authorized" not in fields
        assert "allow" not in fields


# --------------------------------------------------------------------- #
# 16: store/index inconsistency — index failure must not touch the store
# --------------------------------------------------------------------- #


def test_16_remove_resource_index_failure_leaves_store_untouched() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    rid = _res(h.engine.ingest(_ingest())).resource_id
    h.grant(ALICE, PermissionAction.DELETE, f"docs/{rid}", path=True)

    class _BrokenIndex(InMemoryLexicalIndex):
        def remove_resource(self, resource_id: str) -> None:
            raise RuntimeError("index unavailable")

    h.engine = KnowledgeEngine(
        store=h.store,
        index=_BrokenIndex(),
        permission_engine=h.pengine,
        audit_sink=h.audit,
    )
    pending = h.engine.remove_resource(
        RemoveResourceRequest(principal=ALICE, collection_id="docs", resource_id=rid)
    )
    h.confirmations.decide(_cid(pending), approved=True, now=NOW)
    result = h.engine.remove_resource(
        RemoveResourceRequest(principal=ALICE, collection_id="docs", resource_id=rid),
        confirmation_id=pending.confirmation_id,
    )
    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error_category is KnowledgeErrorCategory.INDEXING_ERROR
    # The store must still have the resource — no partial deletion.
    assert h.store.get_resource("docs", rid) is not None


# --------------------------------------------------------------------- #
# 17-18: parser / index raising an unexpected (non-domain) exception
# --------------------------------------------------------------------- #


def test_17_unexpected_parser_exception_fails_closed_not_success() -> None:
    class _BrokenParser:
        def supports(self, resource_type: ResourceType) -> bool:
            return resource_type is ResourceType.TXT

        def parse(self, source: bytes, metadata: object) -> object:
            raise RuntimeError("parser bug")

    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    h.engine = KnowledgeEngine(
        store=h.store,
        index=h.index,
        permission_engine=h.pengine,
        audit_sink=h.audit,
        parsers=cast(Any, (_BrokenParser(),)),
    )
    result = h.engine.ingest(_ingest())
    assert result.status is IngestionStatus.FAILED
    assert result.error_category is KnowledgeErrorCategory.INTERNAL_ERROR
    assert h.store.list_resources("docs") == ()


def test_18_unexpected_index_search_exception_fails_closed() -> None:
    class _BrokenIndex(InMemoryLexicalIndex):
        def search(self, **kwargs: object) -> tuple[Any, ...]:
            raise RuntimeError("index search bug")

    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    h.engine = KnowledgeEngine(
        store=h.store,
        index=_BrokenIndex(),
        permission_engine=h.pengine,
        audit_sink=h.audit,
    )
    result = h.engine.retrieve(
        RetrieveRequest(
            principal=ALICE, query=RetrievalQuery(query="x", collection_id="docs")
        )
    )
    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error_category in (
        KnowledgeErrorCategory.RETRIEVAL_ERROR,
        KnowledgeErrorCategory.INTERNAL_ERROR,
    )


# --------------------------------------------------------------------- #
# 19: audit exception never changes the operation result
# --------------------------------------------------------------------- #


def test_19_failing_audit_sink_never_changes_the_result() -> None:
    from sam.knowledge.audit import FailingKnowledgeAuditSink

    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    h.engine = KnowledgeEngine(
        store=h.store,
        index=h.index,
        permission_engine=h.pengine,
        audit_sink=FailingKnowledgeAuditSink(),
    )
    result = h.engine.ingest(_ingest())
    assert result.status is IngestionStatus.SUCCESS


# --------------------------------------------------------------------- #
# 20: models are frozen (no in-place mutation of a persisted resource)
# --------------------------------------------------------------------- #


def test_20_resource_and_chunk_models_are_immutable() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    result = h.engine.ingest(_ingest())
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _res(result).name = "renamed.txt"
    chunk = h.store.get_chunks(_res(result).resource_id)[0]
    with pytest.raises(ValidationError):
        chunk.text = "tampered"


# --------------------------------------------------------------------- #
# 21-23: no execution path, no network calls, no shell
# --------------------------------------------------------------------- #


def test_21_no_eval_exec_or_shell_execution_anywhere_in_knowledge() -> None:
    package_dir = Path(inspect.getfile(default_parsers)).parent
    forbidden = ("eval(", "exec(", "os.system(", "subprocess.", "shell=True")
    for path in package_dir.glob("*.py"):
        content = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in content, f"{token!r} found in {path}"


def test_22_no_network_or_filesystem_calls_in_knowledge_source() -> None:
    package_dir = Path(inspect.getfile(default_parsers)).parent
    forbidden = ("urllib.request", "requests.", "socket.", "open(", "Path.open")
    for path in package_dir.glob("*.py"):
        content = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in content, f"{token!r} found in {path}"


def test_23_no_import_of_memory_engine_anywhere_in_knowledge() -> None:
    package_dir = Path(inspect.getfile(default_parsers)).parent
    forbidden_imports = (
        "import sam.memory.engine",
        "from sam.memory.engine",
        "from sam.memory import engine",
    )
    for path in package_dir.glob("*.py"):
        content = path.read_text(encoding="utf-8")
        for token in forbidden_imports:
            assert token not in content, f"{token!r} found in {path}"
        # The only permitted sam.memory reference anywhere in this
        # package is the single pure function sam.knowledge.metadata
        # imports from sam.memory.sanitization.
        for line in content.splitlines():
            stripped = line.strip()
            is_import_line = stripped.startswith(
                "from sam.memory"
            ) or stripped.startswith("import sam.memory")
            if is_import_line:
                assert "sanitization" in stripped, (
                    f"unexpected sam.memory import in {path}: {line}"
                )


# --------------------------------------------------------------------- #
# 24-25: confirmation replay / revoked grant
# --------------------------------------------------------------------- #


def test_24_delete_confirmation_scoped_to_exact_resource_not_replayable() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    rid1 = _res(
        h.engine.ingest(_ingest(content=b"first distinct document body text"))
    ).resource_id
    rid2 = _res(
        h.engine.ingest(_ingest(content=b"second distinct document body text"))
    ).resource_id
    h.grant(ALICE, PermissionAction.DELETE, f"docs/{rid1}", path=True)
    h.grant(ALICE, PermissionAction.DELETE, f"docs/{rid2}", path=True)

    pending = h.engine.remove_resource(
        RemoveResourceRequest(principal=ALICE, collection_id="docs", resource_id=rid1)
    )
    h.confirmations.decide(_cid(pending), approved=True, now=NOW)
    h.engine.remove_resource(
        RemoveResourceRequest(principal=ALICE, collection_id="docs", resource_id=rid1),
        confirmation_id=pending.confirmation_id,
    )
    replay = h.engine.remove_resource(
        RemoveResourceRequest(principal=ALICE, collection_id="docs", resource_id=rid2),
        confirmation_id=pending.confirmation_id,
    )
    assert replay.permission_outcome is PermissionOutcomeSummary.DENY
    assert h.store.get_resource("docs", rid2) is not None


def test_25_revoked_ingest_grant_denies_immediately() -> None:
    h = _Harness()
    h.grant(ALICE, PermissionAction.WRITE, "docs:ingest")
    grant_id = f"g-{ALICE.id}-docs:ingest-{PermissionAction.WRITE}-None"
    h.pstore.revoke_grant(grant_id, now=NOW)
    result = h.engine.ingest(_ingest())
    assert result.permission_outcome is PermissionOutcomeSummary.DENY


# --------------------------------------------------------------------- #
# 26: expired grant denies
# --------------------------------------------------------------------- #


def test_26_expired_grant_denies() -> None:
    h = _Harness()
    h.grant(
        ALICE, PermissionAction.WRITE, "docs:ingest", expires_at=NOW - timedelta(days=1)
    )
    result = h.engine.ingest(_ingest())
    assert result.permission_outcome is PermissionOutcomeSummary.DENY


# --------------------------------------------------------------------- #
# 27: wrong-collection grant does not authorize
# --------------------------------------------------------------------- #


def test_27_grant_for_one_collection_does_not_authorize_another() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "research")
    result = h.engine.ingest(_ingest(collection_id="personal"))
    assert result.permission_outcome is PermissionOutcomeSummary.DENY


# --------------------------------------------------------------------- #
# 28: never fabricate page/section/citation metadata
# --------------------------------------------------------------------- #


def test_28_pdf_page_metadata_never_fabricated_beyond_what_exists() -> None:
    parser = PDFParser()
    from sam.knowledge.models import DocumentMetadata as DM

    body = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << >> "
        b"/MediaBox [0 0 612 792] >>",
        4: b"<< /Length 24 >>\nstream\nBT (Only page) Tj ET\nendstream",
    }
    out = bytearray(b"%PDF-1.4\n")
    for num in sorted(body):
        out += f"{num} 0 obj\n".encode() + body[num] + b"\nendobj\n"
    out += b"trailer << /Root 1 0 R >>\n%%EOF"
    parsed = parser.parse(bytes(out), DM())
    assert len(parsed.segments) == 1
    assert parsed.segments[0].page_number == 1
    # No second page exists — nothing invents one.
    assert all(s.page_number in (None, 1) for s in parsed.segments)


# --------------------------------------------------------------------- #
# 29: stale/deleted resources never remain retrievable
# --------------------------------------------------------------------- #


def test_29_deleted_resource_never_retrievable_again() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    rid = _res(h.engine.ingest(_ingest())).resource_id
    h.grant(ALICE, PermissionAction.DELETE, f"docs/{rid}", path=True)
    h.grant(ALICE, PermissionAction.READ, f"docs/{rid}", path=True)
    pending = h.engine.remove_resource(
        RemoveResourceRequest(principal=ALICE, collection_id="docs", resource_id=rid)
    )
    h.confirmations.decide(_cid(pending), approved=True, now=NOW)
    h.engine.remove_resource(
        RemoveResourceRequest(principal=ALICE, collection_id="docs", resource_id=rid),
        confirmation_id=pending.confirmation_id,
    )
    result = h.engine.retrieve(
        RetrieveRequest(
            principal=ALICE, query=RetrievalQuery(query="graph", collection_id="docs")
        )
    )
    assert all(r.resource.resource_id != rid for r in result.results)

    get = h.engine.get_resource(
        GetResourceRequest(principal=ALICE, collection_id="docs", resource_id=rid)
    )
    assert get.error_category is KnowledgeErrorCategory.RESOURCE_NOT_FOUND


# --------------------------------------------------------------------- #
# 30: audit stays content-free even on a secret-rejection path
# --------------------------------------------------------------------- #


def test_30_audit_content_free_even_when_rejecting_a_secret() -> None:
    h = _Harness()
    h.grant_full_access(ALICE, "docs")
    secret_value = "sk-ant-abcdefghijklmnopqrstuvwxyz123456"
    h.engine.ingest(_ingest(content=f"api_key: {secret_value}".encode()))
    for event in h.audit.list_events():
        dumped_text = json.dumps(event.model_dump(mode="json"))
        assert secret_value not in dumped_text
