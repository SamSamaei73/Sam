"""Phase 17 local backup and verified restore (synthetic data, temp dirs)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sam.career.attempts import AttemptLedger, AttemptState, ExternalAction
from sam.permissions.models import utc_now
from sam.storage.attempts import SQLiteAttemptStore
from sam.storage.backup import (
    MAX_BACKUPS,
    BackupError,
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)
from sam.storage.database import Database
from sam.storage.lock import InstanceLock, InstanceLocked
from sam.storage.migrations import CURRENT_VERSION, migrate
from sam.storage.paths import DATABASE_NAME, LOCK_NAME
from sam.system import cli
from tests.storage_support import durable_settings, mode

SECRET = "sk-ant-api03-" + "Q" * 40  # synthetic


def live_db(data_dir: Path) -> Database:
    data_dir.mkdir(mode=0o700, exist_ok=True)
    db = Database(data_dir / DATABASE_NAME)
    migrate(db)
    return db


def add_unknown_attempt(db: Database) -> str:
    ledger = AttemptLedger(store=SQLiteAttemptStore(db))
    attempt = ledger.start(
        action=ExternalAction.SUBMIT,
        owner_id="local-user",
        opportunity_id="op_1",
        item_id="ap_1",
        item_version=2,
        manifest_hash="a" * 64,
        now=utc_now(),
    )
    ledger.recover_after_restart(utc_now())
    return attempt.attempt_id


def test_a_backup_is_verified_owner_only_and_credential_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)  # present in the environment
    db = live_db(tmp_path)
    add_unknown_attempt(db)
    info = create_backup(db, tmp_path)
    folder = tmp_path / "backups"
    copy = folder / f"{info.backup_id}.sqlite3"
    manifest = json.loads((folder / f"{info.backup_id}.json").read_text())
    assert (
        manifest["format"] == "sam.backup/1"
        and manifest["schema_version"] == CURRENT_VERSION
    )
    assert manifest["sha256"] == info.sha256 and manifest["size"] == copy.stat().st_size
    assert mode(folder) == 0o700 and mode(copy) == 0o600
    assert mode(folder / f"{info.backup_id}.json") == 0o600
    assert SECRET.encode() not in copy.read_bytes()
    assert SECRET not in (folder / f"{info.backup_id}.json").read_text()
    assert verify_backup(tmp_path, info.backup_id) == info
    assert not list(folder.glob(".*partial"))


def test_backups_are_bounded(tmp_path: Path) -> None:
    db = live_db(tmp_path)
    for _ in range(MAX_BACKUPS + 3):
        create_backup(db, tmp_path)
    assert len(list_backups(tmp_path)) == MAX_BACKUPS


@pytest.mark.parametrize(
    "backup_id",
    [
        "../../etc/passwd",
        "sam-20260101T000000000000Z-x/../../x",
        "/tmp/evil",
        "sam-2026",
        "",
    ],
)
def test_restore_accepts_only_sam_generated_ids(tmp_path: Path, backup_id: str) -> None:
    live_db(tmp_path)
    with pytest.raises(BackupError) as error:
        verify_backup(tmp_path, backup_id)
    assert error.value.code in ("backup_id_invalid", "backup_not_found")


def test_a_tampered_or_corrupt_backup_is_refused_before_anything_changes(
    tmp_path: Path,
) -> None:
    db = live_db(tmp_path)
    info = create_backup(db, tmp_path)
    db.close()
    copy = tmp_path / "backups" / f"{info.backup_id}.sqlite3"
    live = tmp_path / DATABASE_NAME
    before = live.read_bytes()
    data = bytearray(copy.read_bytes())
    data[-100] ^= 0xFF
    copy.write_bytes(bytes(data))
    with pytest.raises(BackupError) as error:
        restore_backup(tmp_path, live, info.backup_id)
    assert error.value.code == "backup_hash_mismatch"
    assert live.read_bytes() == before
    assert len(list_backups(tmp_path)) == 1  # no safety backup: nothing started


def test_a_corrupt_backup_with_a_matching_manifest_is_refused(tmp_path: Path) -> None:
    """Hash checks alone are not enough: SQLite integrity is verified too."""

    import hashlib

    db = live_db(tmp_path)
    with db.transaction() as conn:
        for i in range(400):
            conn.execute(
                "INSERT INTO audit_events (occurred_at, domain, operation, status)"
                " VALUES ('t', 'system', ?, 'ok')",
                (f"op{i}",),
            )
    info = create_backup(db, tmp_path)
    db.close()
    folder = tmp_path / "backups"
    copy = folder / f"{info.backup_id}.sqlite3"
    data = bytearray(copy.read_bytes())
    middle = len(data) // 2
    data[middle : middle + 3000] = b"\xff" * 3000
    copy.write_bytes(bytes(data))
    manifest_path = folder / f"{info.backup_id}.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sha256"] = hashlib.sha256(bytes(data)).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    live = tmp_path / DATABASE_NAME
    before = live.read_bytes()
    with pytest.raises(BackupError) as error:
        restore_backup(tmp_path, live, info.backup_id)
    assert error.value.code == "backup_integrity_failed"
    assert live.read_bytes() == before and len(list_backups(tmp_path)) == 1


def test_a_backup_from_a_future_schema_is_refused(tmp_path: Path) -> None:
    db = live_db(tmp_path)
    db.connection.execute("PRAGMA user_version = 99")
    info = create_backup(db, tmp_path)
    with pytest.raises(BackupError) as error:
        verify_backup(tmp_path, info.backup_id)
    assert error.value.code == "backup_schema_incompatible"


def test_backup_mutate_restore_round_trip_keeps_unknown_outcomes(
    tmp_path: Path,
) -> None:
    db = live_db(tmp_path)
    attempt_id = add_unknown_attempt(db)
    db.execute(
        "INSERT INTO revoked_bootstrap_grants VALUES (?, ?)",
        ("career:read:career", "t"),
    )
    info = create_backup(db, tmp_path)
    db.execute("DELETE FROM revoked_bootstrap_grants")  # later local change
    db.close()
    safety = restore_backup(tmp_path, tmp_path / DATABASE_NAME, info.backup_id)
    assert safety.label == "pre-restore" and safety.backup_id != info.backup_id
    restored = Database(tmp_path / DATABASE_NAME)
    assert restored.integrity_problems() == []
    assert restored.query("SELECT identity FROM revoked_bootstrap_grants") == [
        ("career:read:career",)
    ]
    attempt = SQLiteAttemptStore(restored).get(attempt_id)
    assert attempt is not None and attempt.state is AttemptState.OUTCOME_UNKNOWN
    restored.close()
    # the safety backup holds the pre-restore state
    assert verify_backup(tmp_path, safety.backup_id).label == "pre-restore"


def test_restore_refuses_while_sam_is_running(tmp_path: Path) -> None:
    db = live_db(tmp_path)
    info = create_backup(db, tmp_path)
    running = InstanceLock(tmp_path / LOCK_NAME).claim()
    try:
        with pytest.raises(InstanceLocked):
            InstanceLock(tmp_path / LOCK_NAME).claim()
        code = cli.main(
            ["restore", info.backup_id, "--confirm", info.backup_id],
            settings=durable_settings(tmp_path),
        )
        assert code == 2
    finally:
        running.release()


def test_the_cli_backs_up_verifies_and_restores_with_explicit_confirmation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = live_db(tmp_path)
    db.close()
    settings = durable_settings(tmp_path)
    assert cli.main(["backup", "--label", "before-change"], settings=settings) == 0
    backup_id = json.loads(capsys.readouterr().out)["backup_id"]
    assert cli.main(["verify-backup", backup_id], settings=settings) == 0
    capsys.readouterr()
    assert (
        cli.main(["restore", backup_id, "--confirm", "wrong"], settings=settings) == 2
    )
    assert "restore_not_confirmed" in capsys.readouterr().out
    assert (
        cli.main(["restore", backup_id, "--confirm", backup_id], settings=settings) == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["safety_backup"].endswith("pre-restore")
    assert cli.main(["check"], settings=settings) == 0
    assert '"integrity": "ok"' in capsys.readouterr().out
