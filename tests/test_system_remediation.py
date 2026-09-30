"""Phase 17 independent-review remediation: durable idempotency including
VERIFIED_SUCCESS, production credential precedence, exclusive and fully
validated restore, secret input, and the expanded restart and backup/restore
matrices across every durable domain. Synthetic data, temporary directories,
local fakes only."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from sam.career.attempts import (
    AttemptConflict,
    AttemptLedger,
    AttemptState,
    ExternalAction,
    ExternalActionStatus,
    Interpretation,
)
from sam.career.models import ApplicationState, OpportunityType, SourceKind
from sam.career.sources import RawListing
from sam.core.config import Settings
from sam.knowledge.models import (
    IngestionStatus,
    IngestResourceRequest,
    ResourceSourceKind,
    ResourceType,
    RetrievalQuery,
    RetrieveRequest,
)
from sam.memory.models import (
    MemoryCandidate,
    MemoryConfidence,
    MemorySource,
    MemoryType,
)
from sam.models.models import PrivacyClass
from sam.permissions.models import (
    PermissionAction,
    PermissionRequest,
    PermissionResource,
    PermissionScope,
    utc_now,
)
from sam.storage import backup as backup_module
from sam.storage.attempts import SQLiteAttemptStore
from sam.storage.backup import (
    BackupError,
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)
from sam.storage.career import SQLiteCareerRepository
from sam.storage.database import Database
from sam.storage.lock import InstanceLocked
from sam.storage.migrations import migrate
from sam.storage.paths import DATABASE_NAME, LOCK_NAME
from sam.storage.professional import SQLiteProfessionalRepository
from sam.system import cli, secrets
from sam.system import startup as startup_module
from sam.system.secrets import (
    CredentialSource,
    MacOSKeychainSecretStore,
    resolve_credentials,
)
from tests.career_support import (
    FAILURE,
    JOB_OFFICIAL,
    OFFICIAL_URL,
    OWNER,
    SUCCESS,
    FakeSubmitter,
    make_rig,
    must,
)
from tests.proactive_support import reminder
from tests.professional_support import CV_TEXT
from tests.storage_support import Sam, db_path, durable_settings
from tests.test_system_security import FakeKeyring

S = ApplicationState
KEYCHAIN_KEY = "AIza" + "K" * 35  # synthetic
AMBIENT_KEY = "AIza" + "E" * 35  # synthetic


# ================================ BLOCKER 1: durable idempotency incl. success


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


@pytest.fixture
def disk(tmp_path: Path) -> Iterator[Disk]:
    d = Disk(tmp_path / "sam.sqlite3")
    yield d
    d.close()


def process(disk: Disk, **kwargs: Any) -> Any:
    db = disk.open()
    rig = make_rig(
        repository=SQLiteCareerRepository(db),
        attempt_store=SQLiteAttemptStore(db),
        pro_repository=SQLiteProfessionalRepository(db),
        **kwargs,
    )
    rig.db = db
    return rig


def restart(disk: Disk, **kwargs: Any) -> Any:
    disk.close()
    rig = process(disk, with_cv=False, **kwargs)
    rig.service.recover_after_restart()
    return rig


def same_manifest(rig: Any, attempt: Any) -> None:
    """Try to start the SAME manifest again through the ledger (the path any
    future dispatcher would use): the database must refuse it."""

    rig.service.attempts.start(
        action=attempt.action,
        owner_id=attempt.owner_id,
        opportunity_id=attempt.opportunity_id,
        item_id=attempt.item_id,
        item_version=attempt.item_version,
        manifest_hash=attempt.manifest_hash,
        now=rig.clock(),
    )


def test_a_successful_submit_is_never_dispatched_again_after_restart(
    disk: Disk,
) -> None:
    rig = process(disk)
    draft = rig.ready_draft()
    assert rig.submit(draft.draft_id).ok
    after = restart(disk)
    (attempt,) = after.service.attempts.all()
    assert attempt.state is AttemptState.VERIFIED_SUCCESS
    with pytest.raises(AttemptConflict):
        same_manifest(after, attempt)
    assert not after.service.submit(OWNER, draft.draft_id).ok
    assert after.submitter.packages == []


def test_a_successful_send_is_never_dispatched_again_after_restart(
    disk: Disk,
) -> None:
    from tests.test_career_external_actions import approved_email, send_confirmations

    rig = process(disk)
    message = approved_email(rig)
    pair = send_confirmations(rig, message.outreach_id)
    assert rig.service.send_outreach(OWNER, message.outreach_id, *pair).ok
    after = restart(disk)
    (attempt,) = after.service.attempts.all()
    assert attempt.action is ExternalAction.SEND
    assert attempt.state is AttemptState.VERIFIED_SUCCESS
    with pytest.raises(AttemptConflict):
        same_manifest(after, attempt)
    assert not after.service.send_outreach(OWNER, message.outreach_id).ok
    assert after.email.sent == []


@pytest.mark.parametrize("how", ["unknown", "recovered_in_flight"])
def test_an_unresolved_manifest_stays_blocked_across_restart(
    disk: Disk, how: str
) -> None:
    rig = process(
        disk,
        submitter=FakeSubmitter(raises=TimeoutError()) if how == "unknown" else None,
    )
    draft = rig.ready_draft()
    if how == "unknown":
        assert rig.submit(draft.draft_id).reason == "outcome_unknown"
    else:
        conf = rig.approve(rig.service.submit(OWNER, draft.draft_id).confirmation_id)
        with rig.service.attempts.lock:
            rig.service._reserve_submission(OWNER, draft.draft_id, conf)
    after = restart(disk)
    (attempt,) = after.service.attempts.all()
    assert attempt.state is AttemptState.OUTCOME_UNKNOWN
    with pytest.raises(AttemptConflict):
        same_manifest(after, attempt)
    with pytest.raises(AttemptConflict):  # any manifest while unresolved
        same_manifest(after, replace(attempt, manifest_hash="e" * 64))
    assert after.submitter.packages == []


def test_a_second_process_duplicate_after_success_is_rejected_by_the_database(
    disk: Disk,
) -> None:
    a = AttemptLedger(store=SQLiteAttemptStore(disk.open()))
    fields: dict[str, Any] = {
        "action": ExternalAction.SUBMIT,
        "owner_id": OWNER.id,
        "opportunity_id": "op_1",
        "item_id": "ap_1",
        "item_version": 1,
        "manifest_hash": "a" * 64,
    }
    first = a.start(now=utc_now(), **fields)
    a.finish(
        first.attempt_id,
        Interpretation(ExternalActionStatus.VERIFIED_SUCCESS, "R-1", "verified"),
        utc_now(),
    )
    other_process = Database(disk.path)
    disk.dbs.append(other_process)
    with pytest.raises(sqlite3.IntegrityError):  # raw SQL: no Python in the way
        other_process.execute(
            "INSERT INTO external_attempts (attempt_id, action, owner_id, item_id,"
            " item_version, manifest_hash, state, created_at, updated_at)"
            " VALUES (?, 'submit', 'local-user', 'ap_1', 1, ?, 'in_flight', 't', 't')",
            ("at_" + "f" * 24, "a" * 64),
        )
    with pytest.raises(AttemptConflict):
        AttemptLedger(store=SQLiteAttemptStore(other_process)).start(
            now=utc_now(), **fields
        )


def test_a_changed_manifest_may_start_a_new_attempt_after_success(
    disk: Disk,
) -> None:
    ledger = AttemptLedger(store=SQLiteAttemptStore(disk.open()))
    base: dict[str, Any] = {
        "action": ExternalAction.SUBMIT,
        "owner_id": OWNER.id,
        "opportunity_id": "op_1",
        "item_id": "ap_1",
        "item_version": 1,
    }
    first = ledger.start(now=utc_now(), manifest_hash="a" * 64, **base)
    ledger.finish(
        first.attempt_id,
        Interpretation(ExternalActionStatus.VERIFIED_SUCCESS, "R-1", "verified"),
        utc_now(),
    )
    second = ledger.start(
        now=utc_now(), manifest_hash="b" * 64, **{**base, "item_version": 2}
    )
    assert second.state is AttemptState.IN_FLIGHT


def test_a_proven_failure_may_be_retried_with_fresh_review_and_authorization(
    disk: Disk,
) -> None:
    rig = process(disk, submitter=FakeSubmitter(status=FAILURE))
    draft = rig.ready_draft()
    failed = rig.submit(draft.draft_id)
    assert must(failed.data).state is S.SUBMISSION_FAILED
    after = restart(disk)
    after.submitter.status = SUCCESS
    # a fresh review: the owner edits, the draft is re-approved, and only a
    # NEW confirmation can authorize the new attempt
    stored = must(after.service.repository.get_draft(draft.draft_id))
    first_q = stored.questions[0].question_id
    after.service.answer_question(OWNER, draft.draft_id, first_q, "Jordan Example")
    for doc in must(after.service.repository.get_draft(draft.draft_id)).document_ids:
        after.service.approve_document(OWNER, doc)
    assert after.service.approve_for_submission(OWNER, draft.draft_id).ok
    done = after.submit(draft.draft_id)
    assert done.ok and len(after.submitter.packages) == 1
    states = sorted(a.state.value for a in after.service.attempts.all())
    assert states == ["verified_failure", "verified_success"]


# ================================= BLOCKER 4B: production credentials


def test_in_production_the_keychain_wins_and_ambient_env_is_ignored() -> None:
    keyring = FakeKeyring()
    store = MacOSKeychainSecretStore(keyring_module=keyring, platform="darwin")
    store.set("gemini_api_key", KEYCHAIN_KEY)
    production = Settings(app_env="production", gemini_api_key=SecretStr(AMBIENT_KEY))
    resolved, report = resolve_credentials(production, store, "ok")
    assert must(resolved.gemini_api_key).get_secret_value() == KEYCHAIN_KEY
    assert report.sources["gemini_api_key"] is CredentialSource.KEYCHAIN
    # nothing in the Keychain: the ambient value is still NOT used
    keyring.items.clear()
    resolved, report = resolve_credentials(production, store, "ok")
    assert resolved.gemini_api_key is None
    assert report.sources["gemini_api_key"] is CredentialSource.NOT_CONFIGURED
    # the Keychain fails: unavailable, never the plaintext environment value
    keyring.fail = True
    resolved, report = resolve_credentials(production, store, "ok")
    assert resolved.gemini_api_key is None
    assert report.sources["gemini_api_key"] is CredentialSource.UNAVAILABLE
    # production with the Keychain switched off: nothing, not the environment
    resolved, report = resolve_credentials(production, None, "disabled")
    assert resolved.gemini_api_key is None


def test_in_development_the_environment_still_wins() -> None:
    keyring = FakeKeyring()
    store = MacOSKeychainSecretStore(keyring_module=keyring, platform="darwin")
    store.set("gemini_api_key", KEYCHAIN_KEY)
    development = Settings(gemini_api_key=SecretStr(AMBIENT_KEY))
    resolved, report = resolve_credentials(development, store, "ok")
    assert must(resolved.gemini_api_key).get_secret_value() == AMBIENT_KEY
    assert report.sources["gemini_api_key"] is CredentialSource.ENVIRONMENT


def test_the_production_app_never_uses_an_ambient_provider_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", AMBIENT_KEY)
    sam = Sam(tmp_path)
    assert sam.app.state.settings.gemini_api_key is None
    sam.stop()


def test_secret_set_reads_the_value_without_echo_never_from_argv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    keyring = FakeKeyring()
    monkeypatch.setattr(
        cli,
        "MacOSKeychainSecretStore",
        lambda: MacOSKeychainSecretStore(keyring_module=keyring, platform="darwin"),
    )
    prompts: list[str] = []

    def hidden_input(prompt: str) -> str:
        prompts.append(prompt)
        return KEYCHAIN_KEY

    monkeypatch.setattr("getpass.getpass", hidden_input)
    with pytest.raises(SystemExit) as error:  # a value on the command line
        cli.main(["secret-set", "gemini_api_key", KEYCHAIN_KEY], settings=Settings())
    assert error.value.code == 2
    assert cli.main(["secret-set", "gemini_api_key"], settings=Settings()) == 0
    out = capsys.readouterr()
    assert '"reason_code": "usage"' in out.out  # the value was not echoed
    assert prompts == ["gemini_api_key: "]
    assert KEYCHAIN_KEY not in out.out + out.err
    assert keyring.items[(secrets.KEYCHAIN_SERVICE, "gemini_api_key")] == KEYCHAIN_KEY


# ================================= BLOCKER 4A: exclusive, validated restore


def durable_db(data_dir: Path) -> Database:
    data_dir.mkdir(mode=0o700, exist_ok=True)
    db = Database(data_dir / DATABASE_NAME)
    migrate(db)
    return db


def test_restore_is_refused_while_sam_runs(tmp_path: Path) -> None:
    sam = Sam(tmp_path)
    info = create_backup(sam.app.state.durable.db, tmp_path)
    before = db_path(tmp_path).read_bytes()
    with pytest.raises(InstanceLocked):
        restore_backup(tmp_path, db_path(tmp_path), info.backup_id)
    assert db_path(tmp_path).read_bytes() == before
    sam.stop()
    restore_backup(tmp_path, db_path(tmp_path), info.backup_id)  # stopped: allowed


def test_restore_is_refused_while_a_backend_process_holds_the_lock(
    tmp_path: Path,
) -> None:
    db = durable_db(tmp_path)
    info = create_backup(db, tmp_path)
    db.close()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; sys.path.insert(0, sys.argv[2]);"
            "from sam.storage.lock import InstanceLock;"
            "from pathlib import Path;"
            "InstanceLock(Path(sys.argv[1])).claim(); print('held', flush=True);"
            "time.sleep(60)",
            str(tmp_path / LOCK_NAME),
            str(Path(__file__).resolve().parents[1] / "src"),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        with pytest.raises(InstanceLocked):
            restore_backup(tmp_path, tmp_path / DATABASE_NAME, info.backup_id)
        assert (
            cli.main(
                ["restore", info.backup_id, "--confirm", info.backup_id],
                settings=durable_settings(tmp_path),
            )
            == 2
        )
    finally:
        holder.kill()
        holder.wait(10)
        if holder.stdout is not None:
            holder.stdout.close()
    time.sleep(0.1)
    restore_backup(tmp_path, tmp_path / DATABASE_NAME, info.backup_id)


def test_restore_is_refused_while_a_migration_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = durable_db(tmp_path)
    info = create_backup(db, tmp_path)
    db.close()
    seen: list[str] = []
    real = migrate

    def migrating(database: Database, backup: Any = None) -> Any:
        try:  # someone tries to restore in the middle of the migration
            restore_backup(tmp_path, tmp_path / DATABASE_NAME, info.backup_id)
            seen.append("restored")
        except InstanceLocked:
            seen.append("refused")
        return real(database, backup=backup)

    monkeypatch.setattr(startup_module, "migrate", migrating)
    sam = Sam(tmp_path)
    assert seen == ["refused"] and not sam.report.blocked
    sam.stop()


def test_a_backup_with_invalid_domain_content_is_refused_before_activation(
    tmp_path: Path,
) -> None:
    import hashlib

    db = durable_db(tmp_path)
    db.execute(
        "INSERT INTO documents (domain, kind, key, body, updated_at)"
        " VALUES ('career', 'draft', 'ap_bad', '{\"not\": \"a draft\"}', 't')"
    )
    info = create_backup(db, tmp_path)  # a valid SQLite file, invalid content
    db.execute("DELETE FROM documents")
    db.close()
    live = tmp_path / DATABASE_NAME
    before = live.read_bytes()
    with pytest.raises(BackupError) as error:
        restore_backup(tmp_path, live, info.backup_id)
    assert error.value.code == "backup_contents_invalid"
    assert live.read_bytes() == before
    assert [b.label for b in list_backups(tmp_path)] == ["manual"]  # no swap began
    copy = tmp_path / "backups" / f"{info.backup_id}.sqlite3"
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == info.sha256


def test_duplicate_attempts_in_a_backup_are_refused(tmp_path: Path) -> None:
    """A tampered backup whose uniqueness index was replaced and which holds
    the same manifest twice as VERIFIED_SUCCESS: migrating it to this schema
    fails on the duplicates, so the restore is refused before activation."""

    import hashlib

    db = durable_db(tmp_path)
    info = create_backup(db, tmp_path)
    db.close()
    copy = tmp_path / "backups" / f"{info.backup_id}.sqlite3"
    raw = sqlite3.connect(copy)
    raw.execute("PRAGMA user_version = 1")
    raw.execute("DROP INDEX one_attempt_per_manifest")
    raw.execute("DROP INDEX one_unresolved_attempt_per_item")
    raw.execute("DROP TABLE memory_documents")
    raw.execute("DROP TABLE knowledge_documents")
    raw.execute("DELETE FROM schema_migrations WHERE version = 2")
    raw.execute(  # the v1 index name, but no longer unique
        "CREATE INDEX one_blocking_attempt_per_item ON external_attempts"
        " (action, item_id)"
    )
    for n in "12":
        raw.execute(
            "INSERT INTO external_attempts (attempt_id, action, owner_id, item_id,"
            " item_version, manifest_hash, state, created_at, updated_at) VALUES"
            " (?, 'submit', 'u', 'ap_1', 1, ?, 'verified_success', 't', 't')",
            ("at_" + n * 24, "a" * 64),
        )
    raw.commit()
    raw.close()
    manifest_path = tmp_path / "backups" / f"{info.backup_id}.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sha256"] = hashlib.sha256(copy.read_bytes()).hexdigest()
    manifest["size"] = copy.stat().st_size
    manifest["schema_version"] = 1
    manifest_path.write_text(json.dumps(manifest))
    live = tmp_path / DATABASE_NAME
    before = live.read_bytes()
    with pytest.raises(BackupError) as error:
        restore_backup(tmp_path, live, info.backup_id)
    assert error.value.code == "backup_contents_invalid"
    assert live.read_bytes() == before


def test_the_safety_backup_is_itself_valid(tmp_path: Path) -> None:
    db = durable_db(tmp_path)
    info = create_backup(db, tmp_path)
    db.execute(
        "INSERT INTO revoked_bootstrap_grants VALUES ('career:read:career', 't')"
    )
    db.close()
    safety = restore_backup(tmp_path, tmp_path / DATABASE_NAME, info.backup_id)
    assert safety.label == "pre-restore"
    assert verify_backup(tmp_path, safety.backup_id) == safety
    backup_module.validate_candidate(  # the same full validation applies to it
        tmp_path, tmp_path / "backups" / f"{safety.backup_id}.sqlite3"
    )


# ================================= expanded restart + backup/restore matrix


def populate(sam: Sam) -> dict[str, Any]:
    """Create synthetic state in EVERY durable domain through the runtime."""

    rt = sam.runtime
    owner = rt.principal
    memory = rt.memory.remember(
        MemoryCandidate.model_validate(
            {
                "principal": owner,
                "memory_type": MemoryType.SEMANTIC,
                "content": "Matrix marker: prefers concise answers.",
                "source": MemorySource.USER_EXPLICIT,
                "confidence": MemoryConfidence.EXPLICIT,
            }
        )
    ).memory
    knowledge = rt.knowledge.ingest(
        IngestResourceRequest(
            principal=owner,
            collection_id="default",
            name="matrix.txt",
            declared_resource_type=ResourceType.TXT,
            source_kind=ResourceSourceKind.UPLOAD,
            source_label="matrix.txt",
            content=b"Matrix marker: graph models detect misinformation.",
        )
    )
    assert knowledge.status is IngestionStatus.SUCCESS
    pro = rt.professional.ingest_source(
        owner,
        name="cv.txt",
        source_type="master_cv",
        privacy_class=PrivacyClass.PERSONAL,
        resource_type="txt",
        content=CV_TEXT.encode(),
    )
    assert pro.ok, pro.reason
    task = rt.proactive.create_task(owner, reminder(recurring=True)).data
    assert task is not None
    rt.proactive.repository.set_condition(
        task.task_id,
        rt.proactive.repository.get_condition(task.task_id).model_copy(
            update={"met": True, "episode": 2, "last_notified_at": utc_now()}
        ),
    )
    rt.proactive.repository.dedup.record("d" * 64, utc_now())
    opportunity = rt.career.import_listing(
        owner,
        RawListing(
            kind=SourceKind.OFFICIAL_CAREER_PAGE,
            opportunity_type=OpportunityType.JOB,
            url=OFFICIAL_URL,
            text=JOB_OFFICIAL,
            retrieved_at=utc_now(),
        ),
    ).data
    assert opportunity is not None
    draft = rt.career.create_draft(owner, opportunity.opportunity_id).data
    assert draft is not None
    ledger = rt.career.attempts
    common: dict[str, Any] = {
        "owner_id": owner.id,
        "opportunity_id": opportunity.opportunity_id,
        "item_version": 1,
        "now": utc_now(),
    }
    success = ledger.start(
        action=ExternalAction.SEND, item_id="or_ok", manifest_hash="1" * 64, **common
    )
    ledger.finish(
        success.attempt_id,
        Interpretation(ExternalActionStatus.VERIFIED_SUCCESS, "MSG-1", "verified"),
        utc_now(),
    )
    unknown = ledger.start(
        action=ExternalAction.SUBMIT, item_id="ap_x", manifest_hash="2" * 64, **common
    )
    ledger.recover_after_restart(utc_now())  # -> OUTCOME_UNKNOWN
    rt.owner_settings.set_scheduler_persistent(True)
    decision = rt.permissions.evaluate(
        PermissionRequest(
            principal=owner,
            action=PermissionAction.DELETE,
            resource=PermissionResource.CAREER,
            scope=PermissionScope.from_path("career/applications/ap_1"),
        )
    )
    confirmation_id = must(decision.confirmation).confirmation_id
    rt.confirmations.decide(confirmation_id, approved=True, now=rt.clock())
    assert rt.check_step_up("probe", "wrong-step-up") in ("failed", "unavailable")
    results = rt.knowledge.retrieve(
        RetrieveRequest(
            principal=owner,
            query=RetrievalQuery(query="graph misinformation", collection_id="default"),
        )
    ).results
    return {
        "memory": must(memory),
        "resource": must(knowledge.resource),
        "results": results,
        "task": task,
        "condition": rt.proactive.repository.get_condition(task.task_id),
        "opportunity": must(
            rt.career.repository.get_opportunity(opportunity.opportunity_id)
        ),
        "draft": must(rt.career.repository.get_draft(draft.draft_id)),
        "success": success.attempt_id,
        "unknown": unknown.attempt_id,
        "confirmation": confirmation_id,
        "evidence": _evidence(rt),
    }


def _evidence(rt: Any) -> Any:
    profile = rt.professional.get_profile(rt.principal)
    return profile.data


def assert_restored(sam: Sam, state: dict[str, Any]) -> None:
    rt = sam.runtime
    owner = rt.principal
    # 1 Memory
    assert (
        rt.memory._store.get(state["memory"].memory_id, principal=owner)
        == (state["memory"])
    )
    # 2 + 14 Knowledge and its provenance, identical retrieval identity
    resource = rt.knowledge._store.get_resource(
        "default", state["resource"].resource_id
    )
    assert resource == state["resource"]
    again = rt.knowledge.retrieve(
        RetrieveRequest(
            principal=owner,
            query=RetrievalQuery(query="graph misinformation", collection_id="default"),
        )
    ).results
    assert again == state["results"]
    # 3 + 13 Professional claims, evidence and provenance
    assert _evidence(rt) == state["evidence"]
    # 4-8 Proactive task, condition state, cooldown, dedup
    assert rt.proactive.repository.get_task(state["task"].task_id) == state["task"]
    assert (
        rt.proactive.repository.get_condition(state["task"].task_id)
        == (state["condition"])
    )
    assert rt.proactive.repository.dedup.is_duplicate("d" * 64, utc_now())
    # 9-10 Career opportunity and draft version
    assert (
        rt.career.repository.get_opportunity(state["opportunity"].opportunity_id)
        == state["opportunity"]
    )
    draft = must(rt.career.repository.get_draft(state["draft"].draft_id))
    assert draft.version == state["draft"].version
    # 11-12 attempts
    assert must(rt.career.attempts.get(state["success"])).state is (
        AttemptState.VERIFIED_SUCCESS
    )
    assert must(rt.career.attempts.get(state["unknown"])).state is (
        AttemptState.OUTCOME_UNKNOWN
    )
    # 15 scheduler owner preference
    assert rt.owner_settings.scheduler_persistent()
    # 16-17 confirmations and step-up state are gone
    assert rt.confirmations.get(state["confirmation"]) is None
    assert rt._step_up_failures == {}


def test_the_expanded_restart_matrix(tmp_path: Path) -> None:
    sam = Sam(tmp_path)
    state = populate(sam)
    sam.stop()
    again = Sam(tmp_path)
    assert not again.report.blocked, again.report.reason_code
    assert_restored(again, state)
    raw = db_path(tmp_path).read_bytes()
    wal = Path(f"{db_path(tmp_path)}-wal")
    raw += wal.read_bytes() if wal.exists() else b""
    assert state["confirmation"].encode() not in raw
    again.stop()


def test_the_backup_restore_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", AMBIENT_KEY)
    sam = Sam(tmp_path)
    state = populate(sam)
    info = create_backup(sam.app.state.durable.db, tmp_path)
    # alter live data after the backup
    rt = sam.runtime
    rt.memory._store.delete(state["memory"].memory_id, principal=rt.principal)
    rt.owner_settings.set_scheduler_persistent(False)
    sam.stop()
    copy = (tmp_path / "backups" / f"{info.backup_id}.sqlite3").read_bytes()
    manifest = (tmp_path / "backups" / f"{info.backup_id}.json").read_text()
    for secret in (AMBIENT_KEY, state["confirmation"]):
        assert secret.encode() not in copy and secret not in manifest
    safety = restore_backup(tmp_path, db_path(tmp_path), info.backup_id)
    assert verify_backup(tmp_path, safety.backup_id).label == "pre-restore"
    assert verify_backup(tmp_path, info.backup_id).sha256 == info.sha256
    restored = Sam(tmp_path)
    assert not restored.report.blocked
    assert_restored(restored, state)
    restored.stop()


def test_step_up_lockout_state_does_not_survive_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Production reads the step-up secret from the Keychain only (a fake here).
    class _Keychain:
        def get(self, name: str) -> str | None:
            return "s" * 20 if name == "desktop_step_up_secret" else None

    monkeypatch.setattr(
        "sam.main.credential_store_for", lambda _s: (_Keychain(), "ok")
    )
    sam = Sam(tmp_path, desktop_step_up_secret=SecretStr("s" * 20))
    for _ in range(3):
        sam.runtime.check_step_up("confirm-1", "wrong")
    assert sam.runtime.check_step_up("confirm-1", "s" * 20) == "locked"
    sam.stop()
    again = Sam(tmp_path, desktop_step_up_secret=SecretStr("s" * 20))
    assert again.runtime._step_up_failures == {}
    again.stop()


# ================================= the production backend entry point


def test_the_production_entry_refuses_non_production_and_bad_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sam.system import backend

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("API_PORT", "8123")
    assert backend.main() == 2
    monkeypatch.setenv("APP_ENV", "production")
    for bad in ("80", "0", "70000", "8000; rm", ""):
        monkeypatch.setenv("API_PORT", bad)
        assert backend.main() == 2
    assert backend.LOOPBACK == "127.0.0.1"


def test_the_backend_exits_when_its_parent_closes_stdin() -> None:
    import io

    from sam.system import backend

    class Server:
        should_exit = False

    server = Server()
    stream = io.TextIOWrapper(io.BytesIO(b"anything"))
    backend._exit_when_parent_goes(server, stream)  # type: ignore[arg-type]
    assert server.should_exit
