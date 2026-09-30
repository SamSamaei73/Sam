"""Owner-only local administration: ``python -m sam.system.cli <command>``.

Runs in the owner's own shell (the operating-system account IS the owner
authorization) and prints reason codes and metadata only: never database
content, a secret, a secret's length or its prefix.

    check                    storage status (schema version, integrity, backups)
    backup [--label L]       verified local backup (works while Sam runs)
    list-backups             newest first
    verify-backup ID         hash + integrity + schema check, touches nothing
    restore ID --confirm ID  Sam must be STOPPED; validates first, keeps a
                             safety backup of the current state, then swaps
    secret-set NAME          read a credential from the terminal (no echo) into
                             the macOS Keychain; NAME is one of KNOWN_SECRETS
    secret-delete NAME       remove it from the Keychain
    secret-status            which credentials are configured, and where
    voice-activation STATE   hands-free "Sam" wake word: on | off | status

There is no command that exports data or credentials, and none that deletes
the database.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from collections.abc import Sequence
from typing import NoReturn

from sam.core.config import Settings
from sam.storage.backup import (
    BackupError,
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)
from sam.storage.database import Database, StorageError
from sam.storage.lock import InstanceLocked
from sam.storage.migrations import CURRENT_VERSION
from sam.storage.paths import (
    DATABASE_NAME,
    UnsafeStoragePath,
    ensure_private_dir,
)
from sam.storage.settings import SQLiteOwnerSettings
from sam.system.secrets import (
    KNOWN_SECRETS,
    MacOSKeychainSecretStore,
    SecretUnavailable,
    credential_store_for,
    resolve_credentials,
)
from sam.system.startup import data_dir_for


def _print(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _open(settings: Settings) -> Database:
    path = ensure_private_dir(data_dir_for(settings)) / DATABASE_NAME
    if not path.exists():
        raise StorageError("database_missing")
    return Database(path)


class _QuietParser(argparse.ArgumentParser):
    """Usage errors never echo the offending arguments: a secret typed on the
    command line by mistake is not printed back (or logged)."""

    def error(self, message: str) -> NoReturn:
        _print({"status": "error", "reason_code": "usage"})
        sys.exit(2)


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    parser = _QuietParser(prog="python -m sam.system.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    backup = commands.add_parser("backup")
    backup.add_argument("--label", default="manual")
    commands.add_parser("list-backups")
    verify = commands.add_parser("verify-backup")
    verify.add_argument("backup_id")
    restore = commands.add_parser("restore")
    restore.add_argument("backup_id")
    restore.add_argument("--confirm", required=True)
    for name in ("secret-set", "secret-delete"):
        command = commands.add_parser(name)
        command.add_argument("name", choices=KNOWN_SECRETS)
    commands.add_parser("secret-status")
    activation = commands.add_parser("voice-activation")
    activation.add_argument("state", choices=("on", "off", "status"))
    args = parser.parse_args(argv)
    settings = settings or Settings()
    try:
        return _run(args, settings)
    except (StorageError, UnsafeStoragePath, SecretUnavailable, InstanceLocked) as e:
        _print({"status": "error", "reason_code": str(getattr(e, "code", "error"))})
        return 2


def _run(args: argparse.Namespace, settings: Settings) -> int:
    data_dir = data_dir_for(settings)
    if args.command == "voice-activation":
        # Trusted local configuration by the owner (like secret-set): the
        # hands-free wake word. It grants no permission.
        db = _open(settings)
        try:
            owner = SQLiteOwnerSettings(db)
            if args.state != "status":
                owner.set_voice_activation(args.state == "on")
            state = "on" if owner.voice_activation() else "off"
        finally:
            db.close()
        _print({"status": "ok", "voice_activation": state})
        return 0
    if args.command == "check":
        db = _open(settings)
        try:
            problems = db.integrity_problems()
            version = db.user_version()
        finally:
            db.close()
        backups = list_backups(data_dir)
        _print(
            {
                "status": "ok"
                if not problems and version == CURRENT_VERSION
                else "degraded",
                "integrity": "ok" if not problems else "failed",
                "schema_version": version,
                "supported_schema_version": CURRENT_VERSION,
                "backup_count": len(backups),
                "last_backup_at": backups[0].created_at if backups else None,
            }
        )
        return 0 if not problems else 1
    if args.command == "backup":
        db = _open(settings)
        try:
            info = create_backup(db, data_dir, label=args.label)
        finally:
            db.close()
        _print({"status": "ok", "backup_id": info.backup_id, "sha256": info.sha256})
        return 0
    if args.command == "list-backups":
        _print(
            {
                "backups": [
                    {
                        "backup_id": b.backup_id,
                        "created_at": b.created_at,
                        "schema_version": b.schema_version,
                        "size": b.size,
                    }
                    for b in list_backups(data_dir)
                ]
            }
        )
        return 0
    if args.command == "verify-backup":
        info = verify_backup(data_dir, args.backup_id)
        _print({"status": "ok", "backup_id": info.backup_id})
        return 0
    if args.command == "restore":
        if args.confirm != args.backup_id:
            raise BackupError("restore_not_confirmed")
        ensure_private_dir(data_dir)
        # restore_backup claims the instance lock itself: refused while Sam runs
        safety = restore_backup(data_dir, data_dir / DATABASE_NAME, args.backup_id)
        _print(
            {
                "status": "ok",
                "restored": args.backup_id,
                "safety_backup": safety.backup_id,
            }
        )
        return 0
    if args.command in ("secret-set", "secret-delete"):
        store = MacOSKeychainSecretStore()
        if args.command == "secret-set":
            value = getpass.getpass(f"{args.name}: ")
            store.set(args.name, value)
        else:
            store.delete(args.name)
        _print({"status": "ok", "name": args.name})
        return 0
    if args.command == "secret-status":
        chosen, status = credential_store_for(settings)
        _, report = resolve_credentials(settings, chosen, status)
        _print(
            {
                "keychain": report.keychain,
                "credentials": {k: v.value for k, v in report.sources.items()},
            }
        )
        return 0
    return 2  # pragma: no cover - argparse enforces the choices


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
