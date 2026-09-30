"""Phase 17 startup, first run, restart, BLOCKED modes and health, through the
real application factory over temporary data directories."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sam.permissions.models import (
    PermissionAction,
    PermissionRequest,
    PermissionResource,
    PermissionScope,
)
from sam.storage.database import Database
from sam.storage.migrations import CURRENT_VERSION, MIGRATIONS, Migration, migrate
from sam.system import startup as startup_module
from tests.desktop_support import HEADERS
from tests.storage_support import Sam, client_for, db_path, mode


def lifespan(sam: Sam) -> TestClient:
    return client_for(sam.app)  # entering it runs the lifespan


# ------------------------------------------------------------- first run


def test_first_run_is_secure_honest_and_opts_into_nothing(tmp_path: Path) -> None:
    data = tmp_path / "data"
    sam = Sam(data)
    with lifespan(sam) as client:
        assert mode(data) == 0o700 and mode(db_path(data)) == 0o600
        assert mode(data / "sam.lock") == 0o600
        assert mode(data / "logs") == 0o700
        status = client.get("/desktop/v1/status", headers=HEADERS).json()
        health = status["health"]
        assert health["status"] == "ready" and health["phase"] == "ready"
        assert (
            health["storage_mode"] == "sqlite"
            and health["schema_version"] == CURRENT_VERSION
        )
        assert health["scheduler"] == "off" and health["reconciliation_required"] == 0
        assert health["backup_count"] == 0 and health["last_backup_at"] is None
        subsystems = {s["name"]: s for s in health["subsystems"]}
        assert subsystems["database"]["status"] == "ok"
        assert subsystems["migrations"]["status"] == "ok"
        assert subsystems["keychain"]["status"] == "disabled"
        assert subsystems["career"]["reason_code"] == "external_actions_unavailable"
        assert not sam.runtime.proactive.scheduler_enabled
        career = client.get("/desktop/v1/career/overview", headers=HEADERS).json()
        assert career["submission_available"] is False
        assert career["sending_available"] is False
        assert career["opportunities"] == []
        text = json.dumps(status)
        assert str(data) not in text and "sqlite3" not in text
    for action in ("submit", "send"):
        from sam.career.attempts import ExternalAction

        readiness = sam.runtime.career.readiness(ExternalAction(action))
        assert not readiness.ready
        assert "adapter_configured" in readiness.missing
        assert "reconciliation_ready" in readiness.missing
    sam.stop()


def test_development_stays_in_memory_and_never_touches_disk(tmp_path: Path) -> None:
    data = tmp_path / "dev"
    sam = Sam(data, app_env="development")
    assert sam.app.state.durable is None
    health = sam.status()["health"]
    assert health["storage_mode"] == "memory"
    assert {s["name"]: s["status"] for s in health["subsystems"]}["database"] == (
        "in_memory"
    )
    assert not data.exists()


# ------------------------------------------------------------ BLOCKED


def assert_blocked(sam: Sam, reason: str) -> None:
    status = sam.status()
    assert status["health"]["status"] == "blocked"
    assert status["health"]["reason_code"] == reason
    assert status["backend"]["status"] == "degraded"
    for path in ("/career/overview", "/permissions", "/proactive/overview"):
        response = sam.get(path)
        assert response.status_code == 503, path
        assert response.json()["detail"]["code"] == "storage_unavailable"
    chat = sam.post("/chat", {"message": "hi"})
    assert chat.status_code == 503


def test_a_second_sam_process_is_refused(tmp_path: Path) -> None:
    first = Sam(tmp_path)
    second = Sam(tmp_path)
    assert_blocked(second, "another_instance_running")
    first.stop()
    third = Sam(tmp_path)
    assert not third.report.blocked
    third.stop()


def test_a_corrupt_database_blocks_startup_and_is_never_recreated(
    tmp_path: Path,
) -> None:
    sam = Sam(tmp_path)
    for i in range(400):
        sam.app.state.durable.audit.append("system", "fill", "ok", reason_code=f"r{i}")
    db = sam.app.state.durable.db
    db.checkpoint()
    root = db.query("SELECT rootpage FROM sqlite_master WHERE name = 'audit_events'")
    page_size = db.query("PRAGMA page_size")[0][0]
    sam.stop()
    path = db_path(tmp_path)
    raw = bytearray(path.read_bytes())
    start = (root[0][0] - 1) * page_size
    raw[start + 8 : start + page_size] = b"\xff" * (page_size - 8)  # a damaged table
    path.write_bytes(bytes(raw))
    before = path.read_bytes()
    blocked = Sam(tmp_path)
    assert blocked.report.blocked
    assert blocked.report.reason_code == "database_corrupt"
    assert blocked.report.failed_phase.value == "integrity_check"
    assert path.read_bytes() == before  # untouched: not repaired, not replaced
    assert blocked.status()["health"]["status"] == "blocked"
    blocked.stop()


def test_a_failed_migration_blocks_startup_and_keeps_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Sam(tmp_path).stop()
    broken = (
        *MIGRATIONS,
        Migration(CURRENT_VERSION + 1, "broken", ("INSERT INTO nowhere VALUES (1)",)),
    )
    real = migrate
    monkeypatch.setattr(
        startup_module,
        "migrate",
        lambda db, backup=None: real(db, migrations=broken, backup=backup),
    )
    sam = Sam(tmp_path)
    assert sam.report.failed_phase.value == "migration"
    assert_blocked(sam, "migration_failed")
    sam.stop()
    db = Database(db_path(tmp_path))
    assert db.user_version() == CURRENT_VERSION and db.integrity_problems() == []


def test_a_database_from_a_newer_sam_is_refused(tmp_path: Path) -> None:
    Sam(tmp_path).stop()
    db = Database(db_path(tmp_path))
    db.connection.execute("PRAGMA user_version = 7")
    db.close()
    sam = Sam(tmp_path)
    assert_blocked(sam, "schema_from_future")
    sam.stop()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"sam_storage": "memory"}, "production_requires_durable_storage"),
        ({"log_level": "DEBUG"}, "production_debug_logging"),
    ],
)
def test_unsafe_production_configuration_is_refused(
    tmp_path: Path, overrides: dict[str, Any], reason: str
) -> None:
    sam = Sam(tmp_path, **overrides)
    assert_blocked(sam, reason)


def test_a_symlinked_data_directory_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    sam = Sam(tmp_path / "link")
    assert_blocked(sam, "storage_path_is_symlink")
    assert not (real / "sam.sqlite3").exists()


# ------------------------------------------------------------- restart


def test_the_scheduler_resumes_only_if_the_owner_asked_to_keep_it_on(
    tmp_path: Path,
) -> None:
    sam = Sam(tmp_path)
    with lifespan(sam) as client:
        body = {"enabled": True}
        response = client.post(
            "/desktop/v1/proactive/scheduler", headers=HEADERS, json=body
        ).json()
        assert response["scheduler_enabled"] and not response["scheduler_persistent"]
    sam.stop()
    again = Sam(tmp_path)
    with lifespan(again) as client:
        assert not again.runtime.proactive.scheduler_enabled  # not remembered
        response = client.post(
            "/desktop/v1/proactive/scheduler",
            headers=HEADERS,
            json={"enabled": True, "remember": True},
        ).json()
        assert response["scheduler_persistent"]
    again.stop()
    third = Sam(tmp_path)
    with lifespan(third) as client:
        assert third.runtime.proactive.scheduler_enabled
        status = client.get("/desktop/v1/status", headers=HEADERS).json()
        assert status["health"]["scheduler"] == "on_persistent"
        client.post(
            "/desktop/v1/proactive/scheduler", headers=HEADERS, json={"enabled": False}
        )
    third.stop()
    fourth = Sam(tmp_path)
    with lifespan(fourth):
        assert not fourth.runtime.proactive.scheduler_enabled
    fourth.stop()


def test_a_revoked_default_grant_stays_revoked_after_restart(tmp_path: Path) -> None:
    sam = Sam(tmp_path)
    grants = sam.get("/permissions").json()["grants"]
    career_read = next(
        g for g in grants if g["resource"] == "career" and g["action"] == "read"
    )
    assert (
        sam.post("/permissions/revoke", {"grant_id": career_read["grant_id"]}).json()[
            "status"
        ]
        == "ok"
    )
    sam.stop()
    again = Sam(tmp_path)
    grants = again.get("/permissions").json()["grants"]
    after = next(
        g for g in grants if g["resource"] == "career" and g["action"] == "read"
    )
    assert after["status"] == "revoked"
    overview = again.get("/career/overview").json()
    assert overview["status"] != "ok"
    again.stop()


def test_pending_and_approved_confirmations_die_with_the_process(
    tmp_path: Path,
) -> None:
    sam = Sam(tmp_path)
    runtime = sam.runtime
    decision = runtime.permissions.evaluate(
        PermissionRequest(
            principal=runtime.principal,
            action=PermissionAction.DELETE,
            resource=PermissionResource.CAREER,
            scope=PermissionScope.from_path("career/applications/ap_1"),
        )
    )
    assert decision.confirmation is not None
    confirmation_id = decision.confirmation.confirmation_id
    runtime.confirmations.decide(confirmation_id, approved=True, now=runtime.clock())
    sam.stop()
    again = Sam(tmp_path)
    assert again.runtime.confirmations.get(confirmation_id) is None
    raw = db_path(tmp_path).read_bytes()
    assert confirmation_id.encode() not in raw
    again.stop()


def test_durable_audit_holds_metadata_only(tmp_path: Path) -> None:
    sam = Sam(tmp_path)
    sam.post(
        "/career/preferences",
        {"salary_preference": "GBP 97,531", "needs_sponsorship": True},
    )
    rows = sam.app.state.durable.audit.recent()
    assert rows
    assert "97,531" not in json.dumps([list(map(str, r)) for r in rows])
    sam.stop()
    raw = db_path(tmp_path).read_bytes()
    wal = Path(f"{db_path(tmp_path)}-wal")
    raw += wal.read_bytes() if wal.exists() else b""
    assert b"97,531" not in raw


def test_an_upgrade_preserves_every_kind_of_state(tmp_path: Path) -> None:
    """A database written by this version, upgraded by a later (test-only)
    schema migration: nothing is lost or changed."""

    sam = Sam(tmp_path)
    runtime = sam.runtime
    sam.post("/career/preferences", {"preferred_roles": ["ML engineer"]})
    runtime.owner_settings.set_scheduler_persistent(True)
    runtime.owner_settings.revoke_bootstrap_grant("career:read:career")
    sam.stop()
    db = Database(db_path(tmp_path))
    before = db.query("SELECT domain, kind, key, body FROM documents ORDER BY 1,2,3")
    v2 = Migration(
        CURRENT_VERSION + 1,
        "add_column",
        ("ALTER TABLE audit_events ADD COLUMN extra TEXT",),
    )
    report = migrate(db, migrations=(*MIGRATIONS, v2))
    assert report.applied == (CURRENT_VERSION + 1,)
    after = db.query("SELECT domain, kind, key, body FROM documents ORDER BY 1,2,3")
    assert after == before
    assert db.query("SELECT value FROM owner_settings") == [("true",)]
    assert db.query("SELECT identity FROM revoked_bootstrap_grants") == [
        ("career:read:career",)
    ]
    db.close()


def test_no_production_data_lands_in_the_repository(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    sam = Sam(tmp_path)
    assert repo not in sam.app.state.durable.data_dir.parents
    sam.stop()
    assert not any(repo.glob("*.sqlite3")) and not (repo / "backups").exists()
    assert os.environ["SAM_DATA_DIR"] != str(repo)
