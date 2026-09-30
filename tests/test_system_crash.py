"""Phase 17 process-crash simulations for durable external actions.

A child process (``tests/crash_child.py``) runs a real, durable, confirmed
Career submission against a fake adapter and is killed with ``os._exit`` at
a precise point. The parent then opens the same database as a new Sam
process would and runs startup recovery. No network, no real adapter.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sam.career.attempts import AttemptState
from sam.career.models import ApplicationState
from sam.storage.attempts import SQLiteAttemptStore
from sam.storage.career import SQLiteCareerRepository
from sam.storage.database import Database
from sam.storage.migrations import migrate
from sam.storage.professional import SQLiteProfessionalRepository
from tests.career_support import OWNER, SUCCESS, make_rig, must

S = ApplicationState
CHILD = Path(__file__).with_name("crash_child.py")


def crash(tmp_path: Path, scenario: str) -> tuple[Path, Path, int]:
    database, marker = tmp_path / "sam.sqlite3", tmp_path / "dispatched"
    done = subprocess.run(
        [sys.executable, str(CHILD), str(database), str(marker), scenario],
        capture_output=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return database, marker, done.returncode


def recover(database: Path) -> tuple[Any, str]:
    db = Database(database)
    migrate(db)
    rig = make_rig(
        repository=SQLiteCareerRepository(db),
        attempt_store=SQLiteAttemptStore(db),
        pro_repository=SQLiteProfessionalRepository(db),
        with_cv=False,
    )
    rig.service.attempts.recover_after_restart(rig.clock())  # RECOVERY phase
    rig.service.recover_after_restart()  # REPOSITORY_INIT reconciliation
    draft_id = Path(str(database.parent / "dispatched") + ".draft").read_text()
    return rig, draft_id


def test_crash_before_the_durable_commit_leaves_no_attempt_and_no_dispatch(
    tmp_path: Path,
) -> None:
    database, marker, code = crash(tmp_path, "before_commit")
    assert code == 9
    assert not marker.exists()
    rig, draft_id = recover(database)
    assert rig.service.attempts.all() == ()  # the uncommitted reservation is gone
    draft = must(rig.service.repository.get_draft(draft_id))
    assert draft.state is S.APPROVED_FOR_SUBMISSION and draft.attempt_id is None


@pytest.mark.parametrize(
    "scenario", ["after_commit_before_dispatch", "after_dispatch_before_response"]
)
def test_a_crash_after_the_durable_commit_is_outcome_unknown_never_retried(
    tmp_path: Path, scenario: str
) -> None:
    database, marker, code = crash(tmp_path, scenario)
    assert code == 9
    assert marker.exists() == (scenario == "after_dispatch_before_response")
    rig, draft_id = recover(database)
    (attempt,) = rig.service.attempts.all()
    assert attempt.state is AttemptState.OUTCOME_UNKNOWN
    assert attempt.reason_code == "interrupted_by_restart"
    draft = must(rig.service.repository.get_draft(draft_id))
    assert draft.state is S.OUTCOME_UNKNOWN
    retry = rig.service.submit(OWNER, draft_id)
    assert not retry.ok and retry.reason == "outcome_unknown"
    assert rig.submitter.packages == []  # never re-dispatched


def test_a_crash_after_a_verified_response_needs_reconciliation(
    tmp_path: Path,
) -> None:
    database, marker, code = crash(tmp_path, "after_response_before_persist")
    assert code == 9 and marker.exists()
    rig, draft_id = recover(database)
    (attempt,) = rig.service.attempts.all()
    assert attempt.state is AttemptState.OUTCOME_UNKNOWN  # the receipt was lost
    assert must(rig.service.repository.get_draft(draft_id)).state is S.OUTCOME_UNKNOWN
    assert rig.service.submit(OWNER, draft_id).reason == "outcome_unknown"
    rig.submitter.reconcile_status = SUCCESS
    settled = rig.service.reconcile_submission(OWNER, draft_id)
    assert settled.ok and must(settled.data).state is S.SUBMITTED
    assert rig.submitter.packages == []


def test_a_persisted_verified_result_survives_the_restart(tmp_path: Path) -> None:
    database, marker, code = crash(tmp_path, "persisted")
    assert code == 0 and marker.exists()
    rig, draft_id = recover(database)
    (attempt,) = rig.service.attempts.all()
    assert attempt.state is AttemptState.VERIFIED_SUCCESS
    assert attempt.attempt_id == marker.read_text()
    assert must(rig.service.repository.get_draft(draft_id)).state is S.SUBMITTED
    assert not rig.service.submit(OWNER, draft_id).ok
    assert rig.submitter.packages == []
