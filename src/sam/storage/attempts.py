"""Durable ``AttemptStore`` for the Career external-action ledger.

Every insert and update is its own committed transaction (or joins the
caller's), so when ``AttemptLedger.start`` returns, the IN_FLIGHT attempt is on
disk. Uniqueness of the blocking attempt per (action, item) is enforced by the
``one_blocking_attempt_per_item`` partial UNIQUE index, and the allowed state
transitions by the ``attempts_are_monotonic`` trigger: the database refuses a
duplicate or a regression even if Python were wrong, and across processes.
There is deliberately no Python pre-check here.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from sam.career.attempts import (
    AttemptConflict,
    AttemptState,
    ExternalAction,
    ExternalActionAttempt,
    IllegalTransition,
    ReconciliationState,
)
from sam.storage.database import Database, StorageError

_COLUMNS = (
    "attempt_id, action, owner_id, opportunity_id, item_id, item_version,"
    " manifest_hash, destination, state, reconciliation, receipt_id, reason_code,"
    " created_at, updated_at, finished_at"
)


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: Any) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _row(row: tuple[Any, ...]) -> ExternalActionAttempt:
    created = _dt(row[12])
    assert created is not None
    return ExternalActionAttempt(
        attempt_id=str(row[0]),
        action=ExternalAction(row[1]),
        owner_id=str(row[2]),
        opportunity_id=row[3],
        item_id=str(row[4]),
        item_version=int(row[5]),
        manifest_hash=str(row[6]),
        destination=row[7],
        state=AttemptState(row[8]),
        reconciliation=ReconciliationState(row[9]),
        receipt_id=row[10],
        reason_code=row[11],
        created_at=created,
        updated_at=_dt(row[13]),
        finished_at=_dt(row[14]),
    )


class SQLiteAttemptStore:
    durable = True

    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, attempt_id: str) -> ExternalActionAttempt | None:
        rows = self._db.query(
            f"SELECT {_COLUMNS} FROM external_attempts WHERE attempt_id = ?",
            (attempt_id,),
        )
        return _row(rows[0]) if rows else None

    def all(self) -> tuple[ExternalActionAttempt, ...]:
        rows = self._db.query(
            f"SELECT {_COLUMNS} FROM external_attempts ORDER BY created_at, attempt_id"
        )
        return tuple(_row(r) for r in rows)

    def blocking(
        self, action: ExternalAction, item_id: str
    ) -> ExternalActionAttempt | None:
        rows = self._db.query(
            f"SELECT {_COLUMNS} FROM external_attempts WHERE action = ?"
            " AND item_id = ? AND state IN"
            " ('in_flight','outcome_unknown','verified_success')",
            (action.value, item_id),
        )
        return _row(rows[0]) if rows else None

    def count(self) -> int:
        return int(self._db.query("SELECT count(*) FROM external_attempts")[0][0])

    def insert(self, attempt: ExternalActionAttempt) -> None:
        try:
            self._db.execute(
                f"INSERT INTO external_attempts ({_COLUMNS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt.attempt_id,
                    attempt.action.value,
                    attempt.owner_id,
                    attempt.opportunity_id,
                    attempt.item_id,
                    attempt.item_version,
                    attempt.manifest_hash,
                    attempt.destination,
                    attempt.state.value,
                    attempt.reconciliation.value,
                    attempt.receipt_id,
                    attempt.reason_code,
                    _ts(attempt.created_at),
                    _ts(attempt.updated_at or attempt.created_at),
                    _ts(attempt.finished_at),
                ),
            )
        except sqlite3.IntegrityError:
            raise AttemptConflict() from None

    def update(self, attempt: ExternalActionAttempt, *, expected: AttemptState) -> None:
        try:
            changed = self._db.execute(
                "UPDATE external_attempts SET state = ?, reconciliation = ?,"
                " receipt_id = ?, reason_code = ?, updated_at = ?, finished_at = ?"
                " WHERE attempt_id = ? AND state = ?",
                (
                    attempt.state.value,
                    attempt.reconciliation.value,
                    attempt.receipt_id,
                    attempt.reason_code,
                    _ts(attempt.updated_at),
                    _ts(attempt.finished_at),
                    attempt.attempt_id,
                    expected.value,
                ),
            )
        except sqlite3.IntegrityError:
            raise IllegalTransition() from None
        except StorageError:
            raise
        if changed != 1:
            raise IllegalTransition()


__all__ = ["SQLiteAttemptStore"]
