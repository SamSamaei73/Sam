"""Phase 17 storage foundation: owner-only paths, hardened SQLite, explicit
migrations and the storage-level constraints of the external-action ledger.
Temporary directories only."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from sam.storage.database import Database, StorageError
from sam.storage.migrations import (
    CURRENT_VERSION,
    MIGRATIONS,
    Migration,
    MigrationError,
    migrate,
)
from sam.storage.paths import (
    UnsafeStoragePath,
    default_data_dir,
    ensure_private_dir,
    ensure_private_file,
)
from tests.storage_support import fresh_db, mode

# ------------------------------------------------------------------ paths


def test_the_data_dir_is_the_platform_app_data_dir_never_the_repo() -> None:
    home = Path("/Users/someone")
    mac = default_data_dir("darwin", home)
    assert mac == home / "Library" / "Application Support" / "app.sam.desktop"
    repo = Path(__file__).resolve().parents[1]
    assert repo not in default_data_dir("darwin").parents


def test_private_directories_and_files_are_owner_only(tmp_path: Path) -> None:
    previous = os.umask(0o022)  # a permissive umask must not matter
    try:
        folder = ensure_private_dir(tmp_path / "a" / "b")
    finally:
        os.umask(previous)
    assert mode(folder) == 0o700
    os.chmod(folder, 0o755)
    ensure_private_dir(folder)  # re-tightened, not trusted
    assert mode(folder) == 0o700
    file = ensure_private_file(folder / "x.db")
    assert mode(file) == 0o600


def test_symlinks_are_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "link").symlink_to(target)
    with pytest.raises(UnsafeStoragePath) as error:
        ensure_private_dir(tmp_path / "link")
    assert error.value.code == "storage_path_is_symlink"
    (tmp_path / "file-link").symlink_to(tmp_path / "victim")
    with pytest.raises(UnsafeStoragePath):
        ensure_private_file(tmp_path / "file-link")
    assert not (tmp_path / "victim").exists()
    with pytest.raises(UnsafeStoragePath):
        ensure_private_dir(Path("relative/dir"))


# --------------------------------------------------------------- database


def test_sqlite_is_configured_safely(tmp_path: Path) -> None:
    db = fresh_db(tmp_path / "sam.sqlite3")
    pragmas = {
        name: db.query(f"PRAGMA {name}")[0][0]
        for name in (
            "foreign_keys",
            "journal_mode",
            "synchronous",
            "secure_delete",
            "busy_timeout",
            "trusted_schema",
        )
    }
    assert pragmas["foreign_keys"] == 1
    assert pragmas["journal_mode"] == "wal"
    assert pragmas["synchronous"] == 2  # FULL
    assert pragmas["secure_delete"] == 1
    assert pragmas["busy_timeout"] == 5000
    assert pragmas["trusted_schema"] == 0
    db.execute(
        "INSERT INTO owner_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ("proactive.scheduler.persistent", "false", "t"),
    )
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{db.path}{suffix}")
        assert path.exists() and mode(path) == 0o600, suffix


def test_transactions_are_all_or_nothing_and_nest(tmp_path: Path) -> None:
    db = fresh_db(tmp_path / "sam.sqlite3")
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO revoked_bootstrap_grants VALUES (?, ?)", ("a", "t")
            )
            with db.transaction() as inner:
                inner.execute(
                    "INSERT INTO revoked_bootstrap_grants VALUES (?, ?)", ("b", "t")
                )
            raise RuntimeError("boom")
    assert db.query("SELECT count(*) FROM revoked_bootstrap_grants")[0][0] == 0


def test_a_full_disk_rolls_back_and_never_reports_success(tmp_path: Path) -> None:
    db = fresh_db(tmp_path / "sam.sqlite3")
    pages = db.query("PRAGMA page_count")[0][0]
    db.connection.execute(f"PRAGMA max_page_count = {pages}")  # simulated full disk
    with pytest.raises(StorageError) as error:
        with db.transaction() as conn:
            for i in range(2_000):
                conn.execute(
                    "INSERT INTO revoked_bootstrap_grants VALUES (?, ?)",
                    (f"grant-{i}-" + "x" * 200, "t"),
                )
    assert isinstance(error.value, StorageError)
    assert db.query("SELECT count(*) FROM revoked_bootstrap_grants")[0][0] == 0


def test_integrity_problems_are_reported_not_repaired(tmp_path: Path) -> None:
    path = tmp_path / "sam.sqlite3"
    db = fresh_db(path)
    with db.transaction() as conn:
        for i in range(400):
            conn.execute(
                "INSERT INTO audit_events (occurred_at, domain, operation, status,"
                " reason_code) VALUES ('t', 'system', 'x', 'ok', ?)",
                (f"r{i:05d}",),
            )
    assert db.integrity_problems() == []
    db.checkpoint()
    db.close()
    raw = bytearray(path.read_bytes())
    middle = len(raw) // 2
    raw[middle : middle + 3000] = b"\xff" * 3000  # damage table pages
    path.write_bytes(bytes(raw))
    try:
        damaged = Database(path)
        assert damaged.integrity_problems() != []
        damaged.close()
    except StorageError:
        pass  # refusing to open is also acceptable: never silently fine


# ------------------------------------------------------------- migrations


def test_migrations_are_explicit_ordered_and_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "sam.sqlite3")
    first = migrate(db)
    assert (first.from_version, first.to_version) == (0, CURRENT_VERSION)
    assert first.applied == tuple(range(1, CURRENT_VERSION + 1))
    again = migrate(db)
    assert again.applied == () and db.user_version() == CURRENT_VERSION
    rows = db.query("SELECT version, name FROM schema_migrations")
    assert rows == [
        (1, "initial_durable_state"),
        (2, "durable_idempotency_memory_knowledge"),
        (3, "owner_setting_voice_activation"),
    ]
    assert [m.version for m in MIGRATIONS] == list(range(1, CURRENT_VERSION + 1))


def test_a_database_from_the_future_is_refused(tmp_path: Path) -> None:
    db = fresh_db(tmp_path / "sam.sqlite3")
    db.connection.execute("PRAGMA user_version = 99")
    with pytest.raises(MigrationError) as error:
        migrate(db)
    assert error.value.code == "schema_from_future"


def test_a_failing_migration_rolls_back_completely(tmp_path: Path) -> None:
    db = fresh_db(tmp_path / "sam.sqlite3")
    broken = (
        *MIGRATIONS,
        Migration(
            CURRENT_VERSION + 1,
            "broken",
            (
                "CREATE TABLE new_table (x INTEGER)",
                "INSERT INTO no_such_table VALUES (1)",
            ),
        ),
    )
    with pytest.raises(MigrationError) as error:
        migrate(db, migrations=broken)
    assert error.value.code == "migration_failed"
    assert db.user_version() == CURRENT_VERSION
    tables = {r[0] for r in db.query("SELECT name FROM sqlite_master")}
    assert "new_table" not in tables
    assert db.query("SELECT max(version) FROM schema_migrations")[0][0] == (
        CURRENT_VERSION
    )


def test_a_destructive_migration_needs_a_verified_backup(tmp_path: Path) -> None:
    db = fresh_db(tmp_path / "sam.sqlite3")
    destructive = (
        *MIGRATIONS,
        Migration(
            CURRENT_VERSION + 1,
            "drop",
            ("DROP TABLE revoked_bootstrap_grants",),
            True,
        ),
    )
    with pytest.raises(MigrationError) as error:
        migrate(db, migrations=destructive)
    assert error.value.code == "backup_required_for_destructive_migration"
    assert db.user_version() == CURRENT_VERSION
    made: list[str] = []

    def backup(label: str) -> Path:
        made.append(label)
        return tmp_path

    report = migrate(db, migrations=destructive, backup=backup)
    assert made == [f"pre-migration-v{CURRENT_VERSION}"]
    assert report.to_version == CURRENT_VERSION + 1

    def failing(label: str) -> Path:
        raise OSError("disk full")

    db2 = fresh_db(tmp_path / "other.sqlite3")
    with pytest.raises(MigrationError) as error:
        migrate(db2, migrations=destructive, backup=failing)
    assert error.value.code == "pre_migration_backup_failed"
    assert db2.user_version() == CURRENT_VERSION


def test_no_table_or_column_can_hold_a_credential(tmp_path: Path) -> None:
    db = fresh_db(tmp_path / "sam.sqlite3")
    names: list[str] = []
    for (table,) in db.query("SELECT name FROM sqlite_master WHERE type='table'"):
        names.append(str(table))
        names += [str(r[1]) for r in db.query(f"PRAGMA table_info('{table}')")]
    for name in names:
        for banned in (
            "secret",
            "credential",
            "password",
            "token",
            "api_key",
            "apikey",
        ):
            assert banned not in name.lower(), name


# ------------------------------------------- attempt constraints (storage)

INSERT = (
    "INSERT INTO external_attempts (attempt_id, action, owner_id, item_id,"
    " item_version, manifest_hash, state, created_at, updated_at)"
    " VALUES (?, 'submit', 'local-user', ?, 1, ?, ?, 't', 't')"
)


def test_the_database_allows_one_unresolved_attempt_per_item(tmp_path: Path) -> None:
    path = tmp_path / "sam.sqlite3"
    fresh_db(path)
    process_a, process_b = Database(path), Database(path)  # two connections
    process_a.execute(INSERT, ("at_" + "a" * 24, "ap_1", "1" * 64, "in_flight"))
    for state in ("in_flight", "outcome_unknown"):
        with pytest.raises(sqlite3.IntegrityError):  # any manifest: unresolved
            process_b.execute(INSERT, ("at_" + "b" * 24, "ap_1", "2" * 64, state))
    # other items are independent
    process_b.execute(INSERT, ("at_" + "d" * 24, "ap_2", "3" * 64, "in_flight"))


def test_the_database_never_allows_the_same_manifest_after_success(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sam.sqlite3"
    fresh_db(path)
    a, b = Database(path), Database(path)
    a.execute(INSERT, ("at_" + "1" * 24, "ap_1", "1" * 64, "verified_success"))
    for state in ("in_flight", "outcome_unknown", "verified_success"):
        with pytest.raises(sqlite3.IntegrityError):
            b.execute(INSERT, ("at_" + "2" * 24, "ap_1", "1" * 64, state))
    # a CHANGED manifest for the item is a new, separately authorized action
    b.execute(INSERT, ("at_" + "3" * 24, "ap_1", "9" * 64, "in_flight"))
    # a proven failure blocks nothing: the same manifest may be retried
    a.execute(INSERT, ("at_" + "4" * 24, "ap_7", "7" * 64, "verified_failure"))
    b.execute(INSERT, ("at_" + "5" * 24, "ap_7", "7" * 64, "in_flight"))


@pytest.mark.parametrize(
    ("start", "to"),
    [
        ("outcome_unknown", "in_flight"),
        ("outcome_unknown", "released_by_owner_x"),
        ("verified_success", "in_flight"),
        ("verified_failure", "in_flight"),
        ("released_by_owner", "in_flight"),
        ("in_flight", "released_by_owner"),
    ],
)
def test_the_database_refuses_non_monotonic_attempt_changes(
    tmp_path: Path, start: str, to: str
) -> None:
    db = fresh_db(tmp_path / "sam.sqlite3")
    db.execute(INSERT, ("at_" + "e" * 24, "ap_1", "1" * 64, start))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE external_attempts SET state = ? WHERE attempt_id = ?",
            (to, "at_" + "e" * 24),
        )
    assert db.query("SELECT state FROM external_attempts")[0][0] == start


def test_attempts_are_never_deleted(tmp_path: Path) -> None:
    db = fresh_db(tmp_path / "sam.sqlite3")
    db.execute(INSERT, ("at_" + "f" * 24, "ap_1", "1" * 64, "outcome_unknown"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM external_attempts")
    assert db.query("SELECT count(*) FROM external_attempts")[0][0] == 1
