"""Phase 17 remediation: durable Memory and Knowledge.

Both keep their Phase 4 / Phase 7 domain logic and policies unchanged; the
durable stores persist only what those domains already accept. Restart =
close the database and rebuild the stores over the same file. Synthetic data
only.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sam.knowledge.audit import InMemoryKnowledgeAuditSink
from sam.knowledge.engine import KnowledgeEngine
from sam.knowledge.errors import SecretDetectedError
from sam.knowledge.index import InMemoryLexicalIndex
from sam.knowledge.models import (
    IngestionStatus,
    IngestResourceRequest,
    RemoveResourceRequest,
    ResourceSourceKind,
    ResourceType,
    RetrievalQuery,
    RetrieveRequest,
)
from sam.memory.engine import MemoryEngine
from sam.memory.errors import MemoryStoreError
from sam.memory.models import (
    MemoryCandidate,
    MemoryConfidence,
    MemorySource,
    MemoryType,
)
from sam.memory.models import RetrievalQuery as MemoryQuery
from sam.memory.working import InMemoryWorkingMemoryStore
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
from sam.storage.database import Database
from sam.storage.knowledge import SQLiteKnowledgeStore, rebuild_index
from sam.storage.memory import SQLiteMemoryStore
from sam.storage.migrations import migrate

OWNER = Principal(kind=PrincipalKind.USER, id="local-user")
OTHER = Principal(kind=PrincipalKind.USER, id="someone-else")
NOW = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
SECRET = "sk-ant-api03-" + "M" * 40  # synthetic


class Disk:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.dbs: list[Database] = []

    def open(self) -> Database:
        db = Database(self.path)
        migrate(db)
        self.dbs.append(db)
        return db

    def close(self) -> None:
        for db in self.dbs:
            db.close()
        self.dbs.clear()

    def raw(self) -> bytes:
        data = self.path.read_bytes()
        wal = Path(f"{self.path}-wal")
        return data + (wal.read_bytes() if wal.exists() else b"")


@pytest.fixture
def disk(tmp_path: Path) -> Iterator[Disk]:
    d = Disk(tmp_path / "sam.sqlite3")
    yield d
    d.close()


# ------------------------------------------------------------------ memory


def memory_engine(disk: Disk) -> tuple[MemoryEngine, SQLiteMemoryStore]:
    store = SQLiteMemoryStore(disk.open())
    engine = MemoryEngine(
        store=store, working_store=InMemoryWorkingMemoryStore(), clock=lambda: NOW
    )
    return engine, store


def candidate(content: str, **extra: Any) -> MemoryCandidate:
    fields: dict[str, Any] = {
        "principal": OWNER,
        "memory_type": MemoryType.SEMANTIC,
        "content": content,
        "source": MemorySource.USER_EXPLICIT,
        "confidence": MemoryConfidence.EXPLICIT,
    }
    fields.update(extra)
    return MemoryCandidate.model_validate(fields)


def test_valid_memory_and_its_scope_survive_restart(disk: Disk) -> None:
    engine, _ = memory_engine(disk)
    kept = engine.remember(candidate("Prefers Persian technical explanations."))
    project = engine.remember(
        candidate(
            "The thesis uses the synthetic NIMBUS corpus.",
            memory_type=MemoryType.PROJECT,
            project_id="thesis",
        )
    )
    assert kept.memory is not None and project.memory is not None
    disk.close()
    again, store = memory_engine(disk)
    # the stored record is identical (the engine's get() also records access)
    assert store.get(kept.memory.memory_id, principal=OWNER) == kept.memory
    assert again.get(kept.memory.memory_id, principal=OTHER) is None  # owner scope
    restored = again.get(project.memory.memory_id, principal=OWNER)
    assert restored is not None and restored.project_id == "thesis"
    found = again.retrieve(MemoryQuery(principal=OWNER, text="persian explanations"))
    assert [i.memory.memory_id for i in found.items] == [kept.memory.memory_id]


def test_deleted_memory_stays_deleted_after_restart(disk: Disk) -> None:
    engine, _ = memory_engine(disk)
    outcome = engine.remember(candidate("A fact that the owner removes later."))
    assert outcome.memory is not None
    engine.delete(outcome.memory.memory_id, principal=OWNER)
    disk.close()
    again, _ = memory_engine(disk)
    assert again.get(outcome.memory.memory_id, principal=OWNER) is None
    assert b"owner removes later" not in disk.raw()


def test_a_secret_memory_never_reaches_sqlite(disk: Disk) -> None:
    engine, store = memory_engine(disk)
    rejected = engine.remember(candidate(f"My api key is {SECRET}"))
    assert rejected.memory is None  # the unchanged Phase 4 policy refuses it
    # defence in depth: even a record that bypassed the policy is refused
    accepted = engine.remember(candidate("A harmless preference."))
    assert accepted.memory is not None
    bad = accepted.memory.model_copy(
        update={"memory_id": "m-bypass", "content": f"token={SECRET}"}
    )
    with pytest.raises(MemoryStoreError):
        store.save(bad)
    assert store.get("m-bypass", principal=OWNER) is None
    assert SECRET.encode() not in disk.raw()
    assert b"M" * 40 not in disk.raw()


def test_memory_stays_separate_from_knowledge_and_professional(disk: Disk) -> None:
    engine, _ = memory_engine(disk)
    engine.remember(candidate("Separate-domain marker for Memory."))
    db = disk.dbs[0]
    tables = {r[0] for r in db.query("SELECT DISTINCT 'memory' FROM memory_documents")}
    assert tables == {"memory"}
    assert db.query("SELECT count(*) FROM knowledge_documents")[0][0] == 0
    assert db.query("SELECT count(*) FROM documents")[0][0] == 0


# --------------------------------------------------------------- knowledge


class KnowledgeRig:
    def __init__(self, disk: Disk) -> None:
        grants = InMemoryPermissionStore()
        for action, scope in (
            (PermissionAction.WRITE, "docs:ingest"),
            (PermissionAction.READ, "docs:list"),
            (PermissionAction.READ, "docs:retrieve"),
        ):
            grants.create_grant(
                PermissionGrant(
                    grant_id=f"g-{scope}",
                    principal=OWNER,
                    resource=PermissionResource.KNOWLEDGE,
                    action=action,
                    scope=PermissionScope.identifier(scope),
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        grants.create_grant(
            PermissionGrant(
                grant_id="g-delete",
                principal=OWNER,
                resource=PermissionResource.KNOWLEDGE,
                action=PermissionAction.DELETE,
                scope=PermissionScope.from_path("docs"),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        self.confirmations = InMemoryConfirmationProvider()
        permissions = PermissionEngine(
            store=grants,
            confirmation_provider=self.confirmations,
            audit_sink=InMemoryAuditSink(),
        )
        self.store = SQLiteKnowledgeStore(disk.open())
        self.index = InMemoryLexicalIndex()
        self.rebuilt = rebuild_index(self.store, self.index)
        self.engine = KnowledgeEngine(
            store=self.store,
            index=self.index,
            permission_engine=permissions,
            audit_sink=InMemoryKnowledgeAuditSink(),
        )

    def ingest(self, name: str, content: bytes) -> Any:
        return self.engine.ingest(
            IngestResourceRequest(
                principal=OWNER,
                collection_id="docs",
                name=name,
                declared_resource_type=ResourceType.TXT,
                source_kind=ResourceSourceKind.UPLOAD,
                source_label=name,
                content=content,
            )
        )

    def search(self, query: str) -> Any:
        outcome = self.engine.retrieve(
            RetrieveRequest(
                principal=OWNER,
                query=RetrievalQuery(query=query, collection_id="docs", top_k=5),
            )
        )
        return outcome.results


DOCS = {
    "gnn.txt": b"Graph neural networks detect misinformation in health forums.",
    "rag.txt": b"Retrieval augmented generation grounds answers in health documents.",
    "misc.txt": b"Field robots use graph planning for misinformation-free maps.",
}


def test_knowledge_resources_provenance_and_retrieval_survive_restart(
    disk: Disk,
) -> None:
    rig = KnowledgeRig(disk)
    ids = {}
    for name, content in DOCS.items():
        result = rig.ingest(name, content)
        assert result.status is IngestionStatus.SUCCESS
        ids[name] = result.resource.resource_id
    before_resources = rig.store.list_resources("docs")
    before = rig.search("graph misinformation health")
    assert before
    disk.close()
    after = KnowledgeRig(disk)
    assert after.rebuilt == sum(r.chunk_count for r in before_resources)
    restored = after.store.list_resources("docs")
    assert sorted(restored, key=lambda r: r.resource_id) == sorted(
        before_resources, key=lambda r: r.resource_id
    )  # checksum, type, source label/kind, metadata, timestamps: provenance
    assert after.search("graph misinformation health") == before  # same identity
    assert after.search("retrieval augmented") == rig.search("retrieval augmented")


def test_a_deleted_resource_stays_deleted(disk: Disk) -> None:
    rig = KnowledgeRig(disk)
    result = rig.ingest("gnn.txt", DOCS["gnn.txt"])
    resource_id = result.resource.resource_id
    removal = rig.engine.remove_resource(
        RemoveResourceRequest(
            principal=OWNER, collection_id="docs", resource_id=resource_id
        )
    )
    pending = getattr(removal, "confirmation_id", None)
    if pending is not None:  # deletion is HIGH risk: the owner confirms
        rig.confirmations.decide(pending, approved=True, now=NOW)
        rig.engine.remove_resource(
            RemoveResourceRequest(
                principal=OWNER, collection_id="docs", resource_id=resource_id
            ),
            confirmation_id=pending,
        )
    assert rig.store.get_resource("docs", resource_id) is None
    disk.close()
    after = KnowledgeRig(disk)
    assert after.store.get_resource("docs", resource_id) is None
    assert after.search("graph neural") == ()
    assert b"Graph neural networks" not in disk.raw()


def test_a_secret_resource_never_persists_or_restores(disk: Disk) -> None:
    rig = KnowledgeRig(disk)
    refused = rig.ingest("notes.txt", f"deploy with api_key = {SECRET}".encode())
    assert refused.status is not IngestionStatus.SUCCESS
    assert SECRET.encode() not in disk.raw()
    # defence in depth: a chunk that bypassed ingestion is refused by the store
    ok = rig.ingest("gnn.txt", DOCS["gnn.txt"])
    chunks = rig.store.get_chunks(ok.resource.resource_id)
    poisoned = tuple(c.model_copy(update={"text": f"token={SECRET}"}) for c in chunks)
    with pytest.raises(SecretDetectedError):
        rig.store.save_chunks(ok.resource.resource_id, poisoned)
    assert SECRET.encode() not in disk.raw()
    # and a secret planted directly in the database fails closed on load
    db = disk.dbs[0]
    body = db.query("SELECT body FROM knowledge_documents WHERE kind = 'chunks'")[0][0]
    db.execute(
        "UPDATE knowledge_documents SET body = ? WHERE kind = 'chunks'",
        (body.replace("Graph neural", f"token={SECRET} Graph neural", 1),),
    )
    disk.close()
    with pytest.raises(SecretDetectedError):
        KnowledgeRig(disk)


def test_corrupt_persisted_knowledge_fails_closed(disk: Disk) -> None:
    rig = KnowledgeRig(disk)
    rig.ingest("gnn.txt", DOCS["gnn.txt"])
    db = disk.dbs[0]
    db.execute(
        'UPDATE knowledge_documents SET body = \'{"not": "a resource"}\''
        " WHERE kind = 'resource'"
    )
    disk.close()
    with pytest.raises(ValueError):
        KnowledgeRig(disk)


def test_an_interrupted_ingest_never_leaves_half_a_resource_on_disk(
    disk: Disk,
) -> None:
    rig = KnowledgeRig(disk)
    result = rig.ingest("gnn.txt", DOCS["gnn.txt"])
    resource = result.resource
    # simulate a crash between the engine's two calls: metadata saved, chunks not
    rig.store.save_resource(
        resource.model_copy(update={"resource_id": "r-half", "name": "half.txt"})
    )
    disk.close()
    after = KnowledgeRig(disk)
    assert after.store.get_resource("docs", "r-half") is None
    assert after.store.get_resource("docs", resource.resource_id) is not None


def test_knowledge_never_writes_memory(disk: Disk) -> None:
    rig = KnowledgeRig(disk)
    rig.ingest("gnn.txt", DOCS["gnn.txt"])
    db = disk.dbs[0]
    assert db.query("SELECT count(*) FROM memory_documents")[0][0] == 0
    assert db.query("SELECT count(*) FROM knowledge_documents")[0][0] > 0
