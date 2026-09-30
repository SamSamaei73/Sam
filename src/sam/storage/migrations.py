"""Explicit, ordered schema migrations.

* The schema version is an integer (``PRAGMA user_version``), and every applied
  migration is also recorded in ``schema_migrations``.
* Migrations run in order, each in ONE transaction (SQLite DDL is
  transactional): a failure rolls that migration back completely and startup
  stops (MIGRATION_FAILED). Sam never continues against a partial schema.
* A database from the future (a higher version than this build knows) is
  refused: no silent downgrade.
* A migration marked ``destructive`` runs only after a VERIFIED backup; there
  is no way to run it without one.
* Nothing is ever inferred from the models: every statement is written here.

Schema v1 highlights (all enforced by SQLite itself, not only by Python):

* ``external_attempts``: at most ONE blocking attempt (in_flight,
  outcome_unknown or verified_success) per (action, item): a partial UNIQUE
  index, so a second thread, a stale retry or a second Sam process cannot
  create a duplicate consequential attempt. A trigger allows only the
  monotonic transitions (in_flight -> verified_success | verified_failure |
  outcome_unknown; outcome_unknown -> verified_success | verified_failure |
  released_by_owner): nothing can ever turn an unknown or in-flight attempt
  back into something retryable. Attempts are never deleted.
* No table stores credentials: provider keys live in the Keychain.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sam.storage.database import Database, StorageError


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    destructive: bool = False


BLOCKING_STATES = "('in_flight','outcome_unknown','verified_success')"
# Durable idempotency (from schema v2): a verified success keeps its manifest
# blocked forever; an unresolved attempt keeps its whole item blocked.
UNRESOLVED_STATES = ("in_flight", "outcome_unknown")
MANIFEST_BLOCKING_STATES = ("in_flight", "outcome_unknown", "verified_success")

V1 = Migration(
    1,
    "initial_durable_state",
    (
        """CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )""",
        """CREATE TABLE documents (
            domain TEXT NOT NULL
                CHECK (domain IN ('professional','career','proactive')),
            kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 40),
            key TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 200),
            body TEXT NOT NULL CHECK (length(body) <= 2000000),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (domain, kind, key)
        ) WITHOUT ROWID""",
        """CREATE TABLE external_attempts (
            attempt_id TEXT PRIMARY KEY
                CHECK (attempt_id GLOB 'at_[0-9a-f]*' AND length(attempt_id) <= 64),
            action TEXT NOT NULL CHECK (action IN ('submit','send')),
            owner_id TEXT NOT NULL CHECK (length(owner_id) BETWEEN 1 AND 200),
            opportunity_id TEXT CHECK (length(opportunity_id) <= 64),
            item_id TEXT NOT NULL CHECK (length(item_id) BETWEEN 1 AND 64),
            item_version INTEGER NOT NULL CHECK (item_version >= 1),
            manifest_hash TEXT NOT NULL CHECK (length(manifest_hash) = 64),
            destination TEXT CHECK (length(destination) <= 300),
            state TEXT NOT NULL CHECK (state IN ('in_flight','verified_success',
                'verified_failure','outcome_unknown','released_by_owner')),
            reconciliation TEXT NOT NULL DEFAULT 'none' CHECK (reconciliation IN
                ('none','required','reconciled','released')),
            receipt_id TEXT CHECK (length(receipt_id) <= 128),
            reason_code TEXT CHECK (length(reason_code) <= 64),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT
        )""",
        f"""CREATE UNIQUE INDEX one_blocking_attempt_per_item
            ON external_attempts (action, item_id)
            WHERE state IN {BLOCKING_STATES}""",
        """CREATE INDEX attempts_by_state ON external_attempts (state)""",
        """CREATE TRIGGER attempts_are_monotonic
            BEFORE UPDATE OF state ON external_attempts
            WHEN NOT (
                OLD.state = NEW.state
                OR (OLD.state = 'in_flight' AND NEW.state IN
                    ('verified_success','verified_failure','outcome_unknown'))
                OR (OLD.state = 'outcome_unknown' AND NEW.state IN
                    ('verified_success','verified_failure','released_by_owner'))
            )
            BEGIN SELECT RAISE(ABORT, 'illegal_attempt_transition'); END""",
        """CREATE TRIGGER attempts_are_never_deleted
            BEFORE DELETE ON external_attempts
            BEGIN SELECT RAISE(ABORT, 'attempts_are_retained'); END""",
        """CREATE TABLE audit_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            domain TEXT NOT NULL CHECK (length(domain) BETWEEN 1 AND 32),
            operation TEXT NOT NULL CHECK (length(operation) BETWEEN 1 AND 40),
            status TEXT NOT NULL CHECK (length(status) BETWEEN 1 AND 40),
            reason_code TEXT CHECK (length(reason_code) <= 64),
            item_id TEXT CHECK (length(item_id) <= 64),
            attempt_id TEXT CHECK (length(attempt_id) <= 64),
            manifest_hash TEXT CHECK (length(manifest_hash) <= 64)
        )""",
        """CREATE TABLE owner_settings (
            key TEXT PRIMARY KEY CHECK (key IN ('proactive.scheduler.persistent')),
            value TEXT NOT NULL CHECK (value IN ('true','false')),
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE revoked_bootstrap_grants (
            identity TEXT PRIMARY KEY CHECK (length(identity) BETWEEN 1 AND 300),
            revoked_at TEXT NOT NULL
        )""",
    ),
)

_DOCUMENT_TABLE = """CREATE TABLE {name} (
            kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 40),
            key TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 300),
            body TEXT NOT NULL CHECK (length(body) <= 2000000),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (kind, key)
        ) WITHOUT ROWID"""

V2 = Migration(
    2,
    "durable_idempotency_memory_knowledge",
    (
        # Idempotency (independent-review remediation): the SAME immutable
        # manifest can never be attempted again once an attempt for it is in
        # flight, of unknown outcome, or VERIFIED_SUCCESS: across restarts and
        # processes. Separately, at most one UNRESOLVED (in flight or unknown)
        # attempt exists per item, whatever its manifest. A proven
        # VERIFIED_FAILURE blocks nothing: a freshly reviewed and confirmed
        # retry may follow. Dropping an index removes no data.
        "DROP INDEX one_blocking_attempt_per_item",
        """CREATE UNIQUE INDEX one_unresolved_attempt_per_item
            ON external_attempts (action, item_id)
            WHERE state IN ('in_flight','outcome_unknown')""",
        """CREATE UNIQUE INDEX one_attempt_per_manifest
            ON external_attempts (action, item_id, manifest_hash)
            WHERE state IN ('in_flight','outcome_unknown','verified_success')""",
        # Durable Memory and Knowledge (separate tables: separate domains).
        _DOCUMENT_TABLE.format(name="memory_documents"),
        _DOCUMENT_TABLE.format(name="knowledge_documents"),
    ),
)

V3 = Migration(
    3,
    "owner_setting_voice_activation",
    (
        # One more owner setting: hands-free voice activation. SQLite cannot
        # alter a CHECK, so the (tiny) table is rebuilt inside this migration's
        # transaction with every existing row copied: no data is lost, and the
        # allow-list stays closed (only these two keys can ever be stored).
        """CREATE TABLE owner_settings_v3 (
            key TEXT PRIMARY KEY CHECK (key IN (
                'proactive.scheduler.persistent', 'voice.activation')),
            value TEXT NOT NULL CHECK (value IN ('true','false')),
            updated_at TEXT NOT NULL
        )""",
        """INSERT INTO owner_settings_v3 (key, value, updated_at)
            SELECT key, value, updated_at FROM owner_settings""",
        "DROP TABLE owner_settings",
        "ALTER TABLE owner_settings_v3 RENAME TO owner_settings",
    ),
)

MIGRATIONS: tuple[Migration, ...] = (V1, V2, V3)
CURRENT_VERSION = MIGRATIONS[-1].version

assert [m.version for m in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))


class MigrationError(StorageError):
    pass


@dataclass(frozen=True)
class MigrationReport:
    from_version: int
    to_version: int
    applied: tuple[int, ...]
    backup: Path | None = None


BackupFn = Callable[[str], Path]


def migrate(
    db: Database,
    *,
    migrations: tuple[Migration, ...] = MIGRATIONS,
    backup: BackupFn | None = None,
) -> MigrationReport:
    """Bring ``db`` to the newest schema, or raise ``MigrationError`` with the
    database exactly as it was before the failing migration."""

    target = migrations[-1].version
    current = db.user_version()
    if current > target:
        raise MigrationError("schema_from_future")
    pending = [m for m in migrations if m.version > current]
    backup_path: Path | None = None
    if any(m.destructive for m in pending):
        if backup is None:
            raise MigrationError("backup_required_for_destructive_migration")
        try:
            backup_path = backup(f"pre-migration-v{current}")
        except Exception:
            raise MigrationError("pre_migration_backup_failed") from None
    applied: list[int] = []
    for migration in pending:
        try:
            with db.transaction() as conn:
                for statement in migration.statements:
                    conn.execute(statement)
                if migration.version == 1 or _has_migrations_table(conn):
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at)"
                        " VALUES (?, ?, ?)",
                        (migration.version, migration.name, _now()),
                    )
                conn.execute(f"PRAGMA user_version = {int(migration.version)}")
        except MigrationError:
            raise
        except Exception:
            raise MigrationError("migration_failed") from None
        applied.append(migration.version)
    return MigrationReport(current, db.user_version(), tuple(applied), backup_path)


def _has_migrations_table(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchall()
    return bool(rows)


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "CURRENT_VERSION",
    "MIGRATIONS",
    "Migration",
    "MigrationError",
    "MigrationReport",
    "migrate",
]
