"""Phase 17 restart matrix and durable external-action semantics.

A "restart" closes the database and builds brand-new services (new permission
engine, new confirmation store) over the same file, exactly as a new Sam
process would. Synthetic data and local fakes only.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from sam.career.attempts import AttemptState, ExternalAction, ReconciliationState
from sam.career.models import ApplicationState, QuestionClass
from sam.career.readiness import ExternalActionReadiness
from sam.proactive.models import ConditionState
from sam.storage import attempts as attempts_module
from sam.storage import career as career_storage
from sam.storage import proactive as proactive_storage
from sam.storage import professional as professional_storage
from sam.storage.attempts import SQLiteAttemptStore
from sam.storage.career import SQLiteCareerRepository
from sam.storage.database import Database, StorageError
from sam.storage.migrations import migrate
from sam.storage.proactive import SQLiteProactiveRepository
from sam.storage.professional import SQLiteProfessionalRepository
from tests.career_support import (
    OWNER,
    FakeSubmitter,
    make_rig,
    must,
    ready_for_tests,
)
from tests.proactive_support import make_rig as make_proactive_rig
from tests.proactive_support import reminder, watch
from tests.professional_support import ingest_cv
from tests.professional_support import make_rig as make_pro_rig

S = ApplicationState


class Disk:
    """One database file; ``open()`` is a new process's connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.open_dbs: list[Database] = []

    def open(self) -> Database:
        db = Database(self.path)
        migrate(db)
        self.open_dbs.append(db)
        return db

    def close_all(self) -> None:
        for db in self.open_dbs:
            db.close()
        self.open_dbs.clear()


@pytest.fixture
def disk(tmp_path: Path) -> Iterator[Disk]:
    d = Disk(tmp_path / "sam.sqlite3")
    yield d
    d.close_all()


def career_process(disk: Disk, **kwargs: Any) -> Any:
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
    disk.close_all()
    rig = career_process(disk, with_cv=False, **kwargs)
    rig.service.recover_after_restart()
    return rig


# =================================================== external attempts


def test_a_verified_submission_survives_restart_and_is_never_repeated(
    disk: Disk,
) -> None:
    rig = career_process(disk)
    draft = rig.ready_draft()
    done = rig.submit(draft.draft_id)
    assert done.ok
    after = restart(disk)
    stored = must(after.service.repository.get_draft(draft.draft_id))
    assert stored.state is S.SUBMITTED and stored.submission_reference == "REF-1"
    (attempt,) = after.service.attempts.all()
    assert attempt.state is AttemptState.VERIFIED_SUCCESS
    assert attempt.receipt_id == "REF-1" and attempt.destination
    again = after.service.submit(OWNER, draft.draft_id)
    assert not again.ok and after.submitter.packages == []


def test_an_in_flight_attempt_becomes_outcome_unknown_after_a_crash(
    disk: Disk,
) -> None:
    rig = career_process(disk)
    draft = rig.ready_draft()
    conf = rig.approve(rig.service.submit(OWNER, draft.draft_id).confirmation_id)
    with rig.service.attempts.lock:  # crash after the durable reservation
        reserved = rig.service._reserve_submission(OWNER, draft.draft_id, conf)
    assert not hasattr(reserved, "ok")  # reserved, never dispatched
    after = restart(disk)
    (attempt,) = after.service.attempts.all()
    assert attempt.state is AttemptState.OUTCOME_UNKNOWN
    assert attempt.reconciliation is ReconciliationState.REQUIRED
    assert attempt.reason_code == "interrupted_by_restart"
    stored = must(after.service.repository.get_draft(draft.draft_id))
    assert stored.state is S.OUTCOME_UNKNOWN and stored.approved_binding is None
    retry = after.service.submit(OWNER, draft.draft_id)
    assert not retry.ok and retry.reason == "outcome_unknown"
    assert retry.confirmation_id is None and after.submitter.packages == []
    # a second restart never turns it into anything retryable
    later = restart(disk)
    assert must(later.service.repository.get_draft(draft.draft_id)).state is (
        S.OUTCOME_UNKNOWN
    )


def test_an_unknown_outcome_survives_restart_and_reconciles(disk: Disk) -> None:
    rig = career_process(disk, submitter=FakeSubmitter(raises=TimeoutError()))
    draft = rig.ready_draft()
    assert rig.submit(draft.draft_id).reason == "outcome_unknown"
    after = restart(disk)
    stored = must(after.service.repository.get_draft(draft.draft_id))
    assert stored.state is S.OUTCOME_UNKNOWN
    after.submitter.reconcile_status = FakeSubmitter().status
    done = after.service.reconcile_submission(OWNER, draft.draft_id)
    assert done.ok and must(done.data).state is S.SUBMITTED
    (attempt,) = after.service.attempts.all()
    assert attempt.state is AttemptState.VERIFIED_SUCCESS
    assert attempt.reconciliation is ReconciliationState.RECONCILED
    assert after.submitter.packages == []


def test_a_confirmation_never_survives_a_restart(disk: Disk) -> None:
    rig = career_process(disk)
    draft = rig.ready_draft()
    conf = rig.approve(rig.service.submit(OWNER, draft.draft_id).confirmation_id)
    after = restart(disk)
    stored = must(after.service.repository.get_draft(draft.draft_id))
    assert stored.state is S.APPROVED_FOR_SUBMISSION  # durable product state
    result = after.service.submit(OWNER, draft.draft_id, conf)
    assert not result.ok and result.reason == "confirmation_invalid"
    assert after.submitter.packages == [] and after.service.attempts.all() == ()


def test_a_failed_durable_reservation_dispatches_nothing(
    disk: Disk, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = career_process(disk)
    draft = rig.ready_draft()
    conf = rig.approve(rig.service.submit(OWNER, draft.draft_id).confirmation_id)

    def full_disk(self: Any, attempt: Any) -> None:
        raise StorageError("database_write_failed")

    monkeypatch.setattr(attempts_module.SQLiteAttemptStore, "insert", full_disk)
    result = rig.service.submit(OWNER, draft.draft_id, conf)
    assert not result.ok and result.reason == "attempt_not_recorded"
    assert rig.submitter.packages == []
    stored = must(rig.service.repository.get_draft(draft.draft_id))
    assert stored.state is S.APPROVED_FOR_SUBMISSION and stored.attempt_id is None
    assert rig.db.query("SELECT count(*) FROM external_attempts")[0][0] == 0


def test_a_database_write_failure_inside_the_reservation_dispatches_nothing(
    disk: Disk, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = career_process(disk)
    draft = rig.ready_draft()
    conf = rig.approve(rig.service.submit(OWNER, draft.draft_id).confirmation_id)
    real = Database.execute

    def disk_full(self: Database, sql: str, params: Any = ()) -> int:
        if sql.startswith("INSERT INTO external_attempts"):
            raise StorageError("database_write_failed")
        return real(self, sql, params)

    monkeypatch.setattr(Database, "execute", disk_full)
    result = rig.service.submit(OWNER, draft.draft_id, conf)
    assert not result.ok and result.reason == "attempt_not_recorded"
    assert rig.submitter.packages == []
    monkeypatch.undo()
    assert rig.db.query("SELECT count(*) FROM external_attempts")[0][0] == 0
    stored = must(rig.service.repository.get_draft(draft.draft_id))
    assert stored.state is S.APPROVED_FOR_SUBMISSION


def test_a_lost_result_write_leaves_a_durable_in_flight_record(
    disk: Disk, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = career_process(disk)
    draft = rig.ready_draft()
    conf = rig.approve(rig.service.submit(OWNER, draft.draft_id).confirmation_id)

    def disk_fails(self: Any, attempt: Any, *, expected: Any) -> None:
        raise StorageError("database_write_failed")

    monkeypatch.setattr(attempts_module.SQLiteAttemptStore, "update", disk_fails)
    result = rig.service.submit(OWNER, draft.draft_id, conf)
    assert not result.ok and result.reason == "result_not_recorded"
    assert len(rig.submitter.packages) == 1  # it really went out
    row = rig.db.query("SELECT state FROM external_attempts")
    assert row == [("in_flight",)]
    retry = rig.service.submit(OWNER, draft.draft_id)
    assert retry.reason == "already_in_flight" and len(rig.submitter.packages) == 1
    monkeypatch.undo()
    after = restart(disk)
    stored = must(after.service.repository.get_draft(draft.draft_id))
    assert stored.state is S.OUTCOME_UNKNOWN  # never SUBMITTED, never retryable
    assert after.service.submit(OWNER, draft.draft_id).reason == "outcome_unknown"


def test_a_second_process_cannot_start_a_duplicate_attempt(disk: Disk) -> None:
    first = career_process(disk)
    draft = first.ready_draft()
    conf = first.approve(first.service.submit(OWNER, draft.draft_id).confirmation_id)
    second = career_process(disk, with_cv=False)  # loaded before the reservation
    conf2 = second.approve(second.service.submit(OWNER, draft.draft_id).confirmation_id)
    with first.service.attempts.lock:
        first.service._reserve_submission(OWNER, draft.draft_id, conf)
    result = second.service.submit(OWNER, draft.draft_id, conf2)
    assert not result.ok and result.reason == "already_in_flight"
    assert first.submitter.packages == [] and second.submitter.packages == []


def test_the_durable_store_refuses_a_duplicate_even_without_python_checks(
    disk: Disk,
) -> None:
    from sam.career.attempts import AttemptConflict, AttemptLedger

    a = AttemptLedger(store=SQLiteAttemptStore(disk.open()))
    b = AttemptLedger(store=SQLiteAttemptStore(disk.open()))
    fields: dict[str, Any] = {
        "action": ExternalAction.SEND,
        "owner_id": OWNER.id,
        "opportunity_id": None,
        "item_id": "or_1",
        "item_version": 1,
        "manifest_hash": "a" * 64,
    }
    from sam.permissions.models import utc_now

    a.start(now=utc_now(), **fields)
    with pytest.raises(AttemptConflict):
        b._store.insert(  # bypass every Python-level check
            a.all()[0].__class__(
                attempt_id="at_" + "9" * 24, created_at=utc_now(), **fields
            )
        )


def test_the_real_adapter_gate_requires_every_condition() -> None:
    fields = {
        name: True
        for name in (
            "durable_store_ready",
            "migrations_ready",
            "idempotency_ready",
            "reconciliation_ready",
            "destination_validation_ready",
            "adapter_configured",
            "permission_ready",
        )
    }
    assert ExternalActionReadiness(**fields).ready
    for name in fields:
        partial = ExternalActionReadiness(**{**fields, name: False})
        assert not partial.ready and partial.missing == (name,)
    rig = make_rig(readiness=None)  # no probe: never ready, even with an adapter
    draft = rig.ready_draft()
    assert rig.service.submit(OWNER, draft.draft_id).reason == (
        "external_actions_not_ready"
    )
    rig2 = make_rig(
        readiness=lambda a, c: ExternalActionReadiness(
            **{**fields, "durable_store_ready": False}
        )
    )
    draft2 = rig2.ready_draft()
    assert rig2.service.submit(OWNER, draft2.draft_id).reason == (
        "external_actions_not_ready"
    )
    assert rig.submitter.packages == [] and rig2.submitter.packages == []
    assert ready_for_tests(ExternalAction.SUBMIT, True).ready


# ============================================== career data retention


def test_career_state_survives_restart_without_sensitive_answers(disk: Disk) -> None:
    rig = career_process(disk)
    rig.contact_details()
    job = rig.job()
    draft = must(
        rig.service.create_draft(
            OWNER,
            job.opportunity_id,
            questions=["Full name", "What are your salary expectations?"],
        ).data
    )
    salary = next(
        q
        for q in draft.questions
        if q.classification is QuestionClass.OWNER_REVIEW_REQUIRED
    )
    rig.service.answer_question(OWNER, draft.draft_id, salary.question_id, "GBP 97,531")
    prefs = must(rig.service.overview(OWNER).data).preferences.model_copy(
        update={"salary_preference": "GBP 88,888", "needs_sponsorship": True}
    )
    rig.service.set_preferences(OWNER, prefs)
    for doc in must(rig.service.repository.get_draft(draft.draft_id)).document_ids:
        rig.service.approve_document(OWNER, doc)
    approved = rig.service.approve_for_submission(OWNER, draft.draft_id)
    assert approved.ok
    raw = disk.path.read_bytes() + Path(f"{disk.path}-wal").read_bytes()
    assert b"97,531" not in raw and b"88,888" not in raw
    after = restart(disk)
    stored = must(after.service.repository.get_draft(draft.draft_id))
    assert [a.question_id for a in stored.answers] != []  # safe answers kept
    assert all(not a.sensitive for a in stored.answers)
    assert stored.state is S.NEEDS_OWNER_INPUT and stored.approved_binding is None
    assert must(after.service.repository.get_opportunity(job.opportunity_id))
    assert len(after.service.repository.list_documents()) >= 2
    kept = after.service.repository.get_preferences(OWNER.id)
    assert kept.salary_preference is None and kept.needs_sponsorship is None
    assert kept.contact.email == "jordan@example.test"


def test_a_failed_write_rolls_the_repository_back(
    disk: Disk, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = career_process(disk)
    job = rig.job()
    before = rig.service.repository.list_drafts()
    from sam.storage import documents

    def fail(self: Any, before_state: Any, after_state: Any) -> None:
        raise StorageError("database_write_failed")

    monkeypatch.setattr(documents.DocumentMirror, "apply", fail)
    with pytest.raises(StorageError):
        rig.service.repository.put_draft(_any_draft(rig, job))
    assert rig.service.repository.list_drafts() == before  # memory rolled back
    monkeypatch.undo()
    after = restart(disk)
    assert after.service.repository.get_draft("ap_x") is None  # nothing on disk


def _any_draft(rig: Any, job: Any) -> Any:
    from sam.career.models import ApplicationDraft

    return ApplicationDraft(
        draft_id="ap_x",
        opportunity_id=job.opportunity_id,
        owner_id=OWNER.id,
        opportunity_checksum=job.checksum,
        created_at=job.last_verified,
        updated_at=job.last_verified,
    )


@pytest.mark.parametrize(
    ("module", "cls"),
    [
        (career_storage, SQLiteCareerRepository),
        (professional_storage, SQLiteProfessionalRepository),
        (proactive_storage, SQLiteProactiveRepository),
    ],
)
def test_every_mutating_repository_method_is_durable(module: Any, cls: Any) -> None:
    for name in module.MUTATORS:
        method = getattr(cls, name)
        assert getattr(method, "__wrapped__", None) is not None, name
    public = {
        n
        for n in dir(cls)
        if not n.startswith("_")
        and callable(getattr(cls, n))
        and n.split("_", 1)[0]
        in (
            "put",
            "add",
            "set",
            "update",
            "delete",
            "replace",
            "remove",
            "confirm",
            "commit",
        )
    }
    assert public <= set(module.MUTATORS), public - set(module.MUTATORS)


# ========================================================== professional


def test_professional_evidence_survives_restart_with_the_same_meaning(
    disk: Disk,
) -> None:
    db = disk.open()
    pro = make_pro_rig(repository=SQLiteProfessionalRepository(db))
    ingest_cv(pro)
    claims = pro.repo.list_claims()
    evidence = pro.repo.list_all_evidence()
    sources = pro.repo.list_sources()
    profile = pro.service.get_profile(OWNER).data
    disk.close_all()
    again = make_pro_rig(repository=SQLiteProfessionalRepository(disk.open()))
    assert again.repo.list_claims() == claims
    assert again.repo.list_all_evidence() == evidence
    assert again.repo.list_sources() == sources
    assert again.service.get_profile(OWNER).data == profile


def test_a_professional_refresh_is_atomic_on_disk(
    disk: Disk, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = disk.open()
    pro = make_pro_rig(repository=SQLiteProfessionalRepository(db))
    ingest_cv(pro)
    evidence = pro.repo.list_all_evidence()
    from sam.storage import documents

    def fail(self: Any, before_state: Any, after_state: Any) -> None:
        raise StorageError("database_write_failed")

    monkeypatch.setattr(documents.DocumentMirror, "apply", fail)
    source = pro.repo.list_sources()[0]
    with pytest.raises(StorageError):
        pro.repo.delete_source(source.source_id)
    assert pro.repo.list_all_evidence() == evidence  # memory rolled back
    monkeypatch.undo()
    disk.close_all()
    again = SQLiteProfessionalRepository(disk.open())
    assert again.list_all_evidence() == evidence  # disk untouched


# =============================================================== proactive


def proactive_process(disk: Disk, **kwargs: Any) -> Any:
    return make_proactive_rig(
        repository=SQLiteProactiveRepository(disk.open()), **kwargs
    )


def test_proactive_tasks_state_dedup_and_cooldown_survive_restart(disk: Disk) -> None:
    rig = proactive_process(disk)
    task = rig.create(watch(cooldown_hours=24))
    rig.flag.met = True
    rig.advance_and_tick(hours=1)
    first = rig.notifications()
    assert len(first) == 1
    state = rig.service.repository.get_condition(task.task_id)
    assert state.last_notified_at is not None
    disk.close_all()
    after = proactive_process(disk, start=rig.clock())
    after.flag.met = True
    assert after.task(task.task_id) == rig.task(task.task_id)
    assert after.service.repository.get_condition(task.task_id) == state
    assert after.notifications() == first
    after.advance_and_tick(hours=1)
    assert after.notifications() == first  # no re-notification after restart
    assert len(after.service.repository.dedup) >= 1


def test_missed_runs_are_not_replayed_after_restart(disk: Disk) -> None:
    rig = proactive_process(disk)
    task = rig.create(reminder(recurring=True))
    next_run = must(rig.task(task.task_id).next_run_at)
    disk.close_all()
    # Sam was off for ten days
    after = proactive_process(disk, start=next_run + timedelta(days=10))
    after.tick()
    runs = [r for r in after.history(task.task_id) if r.kind.value == "run"]
    assert len(runs) <= 1
    upcoming = must(after.task(task.task_id).next_run_at)
    assert upcoming > after.clock()


def test_a_notification_and_its_dedup_key_are_written_together(
    disk: Disk, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = proactive_process(disk)
    task = rig.create(watch(cooldown_hours=24))
    rig.flag.met = True
    from sam.storage import documents

    original = documents.DocumentMirror.apply
    calls = {"n": 0}

    def flaky(self: Any, before: Any, after: Any) -> None:
        kinds = {k for k in after if after.get(k) != before.get(k)}
        if "notification" in kinds:
            calls["n"] += 1
            raise StorageError("database_write_failed")
        original(self, before, after)

    monkeypatch.setattr(documents.DocumentMirror, "apply", flaky)
    try:
        rig.advance_and_tick(hours=1)
    except StorageError:
        pass
    monkeypatch.undo()
    assert calls["n"] >= 1
    disk.close_all()
    after = proactive_process(disk, start=rig.clock())
    stored_dedup = len(after.service.repository.dedup)
    assert (stored_dedup == 0) == (after.notifications() == ())
    state = after.service.repository.get_condition(task.task_id)
    assert isinstance(state, ConditionState)


def test_the_dedup_ledger_itself_survives_restart(disk: Disk) -> None:
    rig = proactive_process(disk)
    now = rig.clock()
    rig.service.repository.dedup.record("k" * 64, now)
    disk.close_all()
    after = proactive_process(disk, start=now)
    assert after.service.repository.dedup.is_duplicate("k" * 64, now)
    assert not after.service.repository.dedup.is_duplicate("z" * 64, now)
