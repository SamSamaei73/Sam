"""One hardened SQLite connection for Sam's durable state.

Configuration (verified, not assumed):

* the database file is pre-created ``0600`` without following links; SQLite
  gives its ``-wal`` / ``-shm`` files the same mode, and Sam re-checks them;
* ``journal_mode=WAL`` with ``synchronous=FULL``: a committed transaction is
  on disk before ``commit`` returns, which is what the external-action ledger
  needs (a dispatch happens only after a durable IN_FLIGHT record);
* ``foreign_keys=ON``, ``secure_delete=ON`` (deleted content is overwritten),
  ``trusted_schema=OFF``, a bounded ``busy_timeout``;
* ONE connection per process, used under a lock (bounded connection usage);
* every statement is a fixed string with bound parameters. There is no
  arbitrary-SQL interface and no SQL is ever built from user or model text.

Transactions are ``BEGIN IMMEDIATE`` (the write lock is taken up front, so two
processes cannot interleave a read-check-write). They nest: an inner
``transaction()`` joins the outer one, and any exception rolls the whole
outer transaction back.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from sam.storage.errors import StorageError
from sam.storage.paths import ensure_private_file, verify_private_file

BUSY_TIMEOUT_MS = 5_000
Params = Sequence[Any]


class Database:
    def __init__(
        self, path: Path | str, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS
    ) -> None:
        self.path = Path(path) if str(path) != ":memory:" else None
        if self.path is not None:
            ensure_private_file(self.path)
        try:
            self._conn = sqlite3.connect(
                str(self.path) if self.path is not None else ":memory:",
                isolation_level=None,  # explicit transactions only
                check_same_thread=False,
                timeout=busy_timeout_ms / 1000,
            )
        except sqlite3.Error:
            raise StorageError("database_open_failed") from None
        self._lock = RLock()
        self._depth = 0
        try:
            self._configure(busy_timeout_ms)
        except sqlite3.Error:
            self._conn.close()
            raise StorageError("database_open_failed") from None
        self.verify_files()

    def _configure(self, busy_timeout_ms: int) -> None:
        c = self._conn
        c.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA trusted_schema = OFF")
        c.execute("PRAGMA secure_delete = ON")
        if self.path is not None:
            mode = c.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise sqlite3.OperationalError("wal unavailable")
        c.execute("PRAGMA synchronous = FULL")
        if c.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise sqlite3.OperationalError("foreign keys unavailable")

    def verify_files(self) -> None:
        if self.path is None:
            return
        for suffix in ("", "-wal", "-shm"):
            verify_private_file(Path(f"{self.path}{suffix}"))

    # ----------------------------------------------------------- access

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                except sqlite3.Error:
                    raise StorageError("database_busy") from None
            self._depth += 1
            try:
                yield self._conn
            except sqlite3.IntegrityError:
                self._depth -= 1
                if outer:
                    self._rollback()
                raise  # constraint violations stay distinguishable (ledger)
            except sqlite3.Error:
                self._depth -= 1
                if outer:
                    self._rollback()
                raise StorageError("database_write_failed") from None
            except BaseException:
                self._depth -= 1
                if outer:
                    self._rollback()
                raise
            self._depth -= 1
            if outer:
                try:
                    self._conn.execute("COMMIT")
                except sqlite3.Error:
                    self._rollback()
                    raise StorageError("database_write_failed") from None

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def execute(self, sql: str, params: Params = ()) -> int:
        """One write statement inside a transaction; returns the row count."""

        with self.transaction() as conn:
            try:
                return conn.execute(sql, tuple(params)).rowcount
            except sqlite3.IntegrityError:
                raise
            except sqlite3.Error:
                raise StorageError("database_write_failed") from None

    def query(self, sql: str, params: Params = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            try:
                return list(self._conn.execute(sql, tuple(params)).fetchall())
            except sqlite3.Error:
                raise StorageError("database_read_failed") from None

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    # --------------------------------------------------------- metadata

    def user_version(self) -> int:
        return int(self.query("PRAGMA user_version")[0][0])

    def integrity_problems(self) -> list[str]:
        """``[]`` when ``PRAGMA integrity_check`` reports ok. The problem
        strings stay internal: callers report only a reason code."""

        try:
            rows = self.query("PRAGMA integrity_check")
        except StorageError:
            return ["integrity_check_failed"]
        values = [str(r[0]) for r in rows]
        return [] if values == ["ok"] else values or ["integrity_check_failed"]

    def checkpoint(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                raise StorageError("database_checkpoint_failed") from None

    @property
    def connection(self) -> sqlite3.Connection:
        """For the backup API and migrations only (trusted Sam code)."""

        return self._conn

    @property
    def lock(self) -> RLock:
        return self._lock

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


__all__ = ["BUSY_TIMEOUT_MS", "Database", "StorageError"]
