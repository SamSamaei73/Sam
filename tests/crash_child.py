"""Child process for the Phase 17 crash tests (not a test module).

    python tests/crash_child.py <database> <marker> <scenario>

Runs a durable, fully confirmed Career submission against a FAKE adapter and
kills the process (``os._exit``, no cleanup, no finally blocks) at the point
named by ``scenario``. The adapter "dispatches" by writing ``marker``. Nothing
leaves the machine.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1]),
    str(Path(__file__).resolve().parents[1] / "src"),
]

from sam.storage import attempts as attempts_module  # noqa: E402
from sam.storage.attempts import SQLiteAttemptStore  # noqa: E402
from sam.storage.career import SQLiteCareerRepository  # noqa: E402
from sam.storage.database import Database  # noqa: E402
from sam.storage.migrations import migrate  # noqa: E402
from sam.storage.professional import SQLiteProfessionalRepository  # noqa: E402
from tests.career_support import OWNER, FakeSubmitter, make_rig  # noqa: E402


def die() -> None:
    os._exit(9)


def main(database: str, marker: str, scenario: str) -> None:
    db = Database(database)
    migrate(db)

    class Adapter(FakeSubmitter):
        def submit(self, package: Any, guard: Any) -> Any:
            if scenario == "after_commit_before_dispatch":
                die()
            Path(marker).write_text(package.attempt.attempt_id)  # "dispatched"
            if scenario == "after_dispatch_before_response":
                die()
            return super().submit(package, guard)

    rig = make_rig(
        repository=SQLiteCareerRepository(db),
        attempt_store=SQLiteAttemptStore(db),
        pro_repository=SQLiteProfessionalRepository(db),
        submitter=Adapter(),
    )
    draft = rig.ready_draft()
    Path(marker + ".draft").write_text(draft.draft_id)
    conf = rig.approve(rig.service.submit(OWNER, draft.draft_id).confirmation_id)
    store_class: Any = attempts_module.SQLiteAttemptStore  # crash hooks below
    if scenario == "before_commit":
        store_class.insert = lambda self, a: die()
    if scenario == "after_response_before_persist":
        store_class.update = lambda self, a, *, expected: die()
    result = rig.service.submit(OWNER, draft.draft_id, conf)
    if scenario == "persisted" and result.ok:
        db.close()
        os._exit(0)
    os._exit(3)  # the scenario did not crash where intended


if __name__ == "__main__":
    main(*sys.argv[1:4])
