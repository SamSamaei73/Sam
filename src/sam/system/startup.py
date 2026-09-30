"""Explicit startup phases and fail-closed storage bootstrap.

    BOOTSTRAP -> DATA_DIRECTORY_CHECK -> DATABASE_OPEN -> INTEGRITY_CHECK
    -> MIGRATION -> RECOVERY -> REPOSITORY_INIT -> SECURITY_BOUNDARY_INIT
    -> PROVIDER_INIT -> SCHEDULER_INIT -> READY

If a storage or security stage fails, Sam does NOT report READY and does not
serve owner data: it starts BLOCKED, answering only its status with a safe
reason code (no path, SQL, database content or exception text). In
particular:

* an unsafe data directory (wrong owner, too-open permissions, a symlink) is
  refused;
* a second Sam process for the same data directory is refused
  (``another_instance_running``);
* a database that fails ``PRAGMA integrity_check`` is left untouched
  (``database_corrupt``): never repaired, emptied or recreated automatically;
* a failed or future-schema migration is rolled back / refused
  (``migration_failed`` / ``schema_from_future``);
* production configured with in-memory storage is refused
  (``production_requires_durable_storage``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sam.career.attempts import AttemptLedger
from sam.core.config import Settings
from sam.permissions.models import utc_now
from sam.storage.attempts import SQLiteAttemptStore
from sam.storage.audit import DurableAuditLog
from sam.storage.backup import create_backup
from sam.storage.database import Database, StorageError
from sam.storage.lock import InstanceLock, InstanceLocked
from sam.storage.migrations import MigrationReport, migrate
from sam.storage.paths import (
    DATABASE_NAME,
    LOCK_NAME,
    UnsafeStoragePath,
    default_data_dir,
    ensure_private_dir,
)
from sam.storage.settings import SQLiteOwnerSettings


class StartupPhase(StrEnum):
    BOOTSTRAP = "bootstrap"
    DATA_DIRECTORY_CHECK = "data_directory_check"
    DATABASE_OPEN = "database_open"
    INTEGRITY_CHECK = "integrity_check"
    MIGRATION = "migration"
    RECOVERY = "recovery"
    REPOSITORY_INIT = "repository_init"
    SECURITY_BOUNDARY_INIT = "security_boundary_init"
    PROVIDER_INIT = "provider_init"
    SCHEDULER_INIT = "scheduler_init"
    READY = "ready"


@dataclass
class StartupReport:
    storage_mode: str = "memory"
    completed: list[StartupPhase] = field(default_factory=list)
    failed_phase: StartupPhase | None = None
    reason_code: str | None = None
    schema_version: int | None = None
    recovery: dict[str, int] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.failed_phase is not None

    @property
    def phase(self) -> StartupPhase:
        if self.failed_phase is not None:
            return self.failed_phase
        return self.completed[-1] if self.completed else StartupPhase.BOOTSTRAP

    def done(self, phase: StartupPhase) -> None:
        if not self.blocked and phase not in self.completed:
            self.completed.append(phase)

    def fail(self, phase: StartupPhase, code: str) -> None:
        if not self.blocked:
            self.failed_phase = phase
            self.reason_code = code


@dataclass
class DurableState:
    """Everything durable a runtime is composed from. Owned by the process;
    closed (and the instance lock released) on shutdown."""

    data_dir: Path
    db: Database
    lock: InstanceLock
    audit: DurableAuditLog
    owner_settings: SQLiteOwnerSettings
    attempt_store: SQLiteAttemptStore
    migration: MigrationReport

    def close(self) -> None:
        self.db.close()
        self.lock.release()


def data_dir_for(settings: Settings) -> Path:
    return Path(settings.sam_data_dir) if settings.sam_data_dir else default_data_dir()


def production_violations(settings: Settings) -> list[str]:
    """Production defaults fail closed; these configurations are refused."""

    problems: list[str] = []
    if settings.is_production:
        if settings.storage_backend != "sqlite":
            problems.append("production_requires_durable_storage")
        if settings.log_level.strip().upper() == "DEBUG":
            problems.append("production_debug_logging")
    return problems


def open_durable_state(
    settings: Settings, report: StartupReport
) -> DurableState | None:
    """Run DATA_DIRECTORY_CHECK .. RECOVERY. Returns ``None`` (with the report
    marked failed) on any failure, having left existing data untouched."""

    report.done(StartupPhase.BOOTSTRAP)
    violations = production_violations(settings)
    if violations:
        report.fail(StartupPhase.BOOTSTRAP, violations[0])
        return None
    if settings.storage_backend != "sqlite":
        report.storage_mode = "memory"
        return None
    report.storage_mode = "sqlite"
    phase = StartupPhase.DATA_DIRECTORY_CHECK
    lock: InstanceLock | None = None
    db: Database | None = None
    try:
        data_dir = ensure_private_dir(data_dir_for(settings))
        lock = InstanceLock(data_dir / LOCK_NAME).claim()
        report.done(phase)
        phase = StartupPhase.DATABASE_OPEN
        db = Database(data_dir / DATABASE_NAME)
        report.done(phase)
        phase = StartupPhase.INTEGRITY_CHECK
        if db.integrity_problems():
            raise StorageError("database_corrupt")
        report.done(phase)
        phase = StartupPhase.MIGRATION
        opened = db
        migration = migrate(
            opened,
            backup=lambda label: _backup_path(opened, data_dir, label),
        )
        report.schema_version = migration.to_version
        db.verify_files()
        report.done(phase)
        phase = StartupPhase.RECOVERY
        attempts = SQLiteAttemptStore(db)
        recovered = AttemptLedger(store=attempts).recover_after_restart(utc_now())
        report.recovery["attempts_unknown"] = len(recovered)
        audit = DurableAuditLog(db)
        if recovered:
            audit.append("system", "recovery", "outcome_unknown", reason_code="restart")
        report.done(phase)
        return DurableState(
            data_dir=data_dir,
            db=db,
            lock=lock,
            audit=audit,
            owner_settings=SQLiteOwnerSettings(db),
            attempt_store=attempts,
            migration=migration,
        )
    except InstanceLocked as error:
        report.fail(phase, error.code)
    except (UnsafeStoragePath, StorageError) as error:
        report.fail(phase, error.code)
    except OSError:
        report.fail(phase, "storage_unavailable")
    except Exception:
        report.fail(phase, "startup_failed")
    if db is not None:
        db.close()
    if lock is not None:
        lock.release()
    return None


def _backup_path(db: Database, data_dir: Path, label: str) -> Path:
    info = create_backup(db, data_dir, label=label)
    return data_dir / "backups" / f"{info.backup_id}.sqlite3"


__all__ = [
    "DurableState",
    "StartupPhase",
    "StartupReport",
    "data_dir_for",
    "open_durable_state",
    "production_violations",
]
