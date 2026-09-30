"""Owner settings that are INTENTIONALLY persistent, and nothing else.

* ``proactive.scheduler.persistent``: whether background scheduling resumes
  after a restart. Absent (fresh install, and every migrated database) means
  OFF. It is set only when the owner explicitly asks the scheduler to stay on
  across restarts, and cleared whenever the owner turns scheduling off.
* ``voice.activation``: hands-free wake-word listening ("Sam"). Absent means
  OFF. Only the owner can change it (never a guest, the model or remote
  content). Turning it on grants no permission: it only lets Sam listen
  LOCALLY for its name.
* revoked bootstrap grants: when the owner revokes one of the desktop's
  trusted default grants, that choice survives a restart (otherwise a restart
  would silently hand back access the owner removed). Only narrowing is ever
  persisted: no grant is ever created from the database.

Session grants, one-time confirmations and step-up state are never persisted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sam.storage.database import Database

SCHEDULER_PERSISTENT = "proactive.scheduler.persistent"
VOICE_ACTIVATION = "voice.activation"


class OwnerSettingsStore(Protocol):
    def scheduler_persistent(self) -> bool: ...
    def set_scheduler_persistent(self, value: bool) -> None: ...
    def voice_activation(self) -> bool: ...
    def set_voice_activation(self, value: bool) -> None: ...
    def revoked_bootstrap_grants(self) -> frozenset[str]: ...
    def revoke_bootstrap_grant(self, identity: str) -> None: ...


class InMemoryOwnerSettings:
    """Development / tests: nothing survives a restart (scheduler OFF)."""

    durable = False

    def __init__(self) -> None:
        self._scheduler = False
        self._voice_activation = False
        self._revoked: set[str] = set()

    def scheduler_persistent(self) -> bool:
        return self._scheduler

    def set_scheduler_persistent(self, value: bool) -> None:
        self._scheduler = value

    def voice_activation(self) -> bool:
        return self._voice_activation

    def set_voice_activation(self, value: bool) -> None:
        self._voice_activation = value

    def revoked_bootstrap_grants(self) -> frozenset[str]:
        return frozenset(self._revoked)

    def revoke_bootstrap_grant(self, identity: str) -> None:
        self._revoked.add(identity)


class SQLiteOwnerSettings:
    durable = True

    def __init__(self, db: Database) -> None:
        self._db = db

    def _flag(self, key: str) -> bool:
        rows = self._db.query("SELECT value FROM owner_settings WHERE key = ?", (key,))
        return bool(rows) and rows[0][0] == "true"

    def _set_flag(self, key: str, value: bool) -> None:
        self._db.execute(
            "INSERT INTO owner_settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, "true" if value else "false", _now()),
        )

    def scheduler_persistent(self) -> bool:
        return self._flag(SCHEDULER_PERSISTENT)

    def set_scheduler_persistent(self, value: bool) -> None:
        self._set_flag(SCHEDULER_PERSISTENT, value)

    def voice_activation(self) -> bool:
        return self._flag(VOICE_ACTIVATION)

    def set_voice_activation(self, value: bool) -> None:
        self._set_flag(VOICE_ACTIVATION, value)

    def revoked_bootstrap_grants(self) -> frozenset[str]:
        rows = self._db.query("SELECT identity FROM revoked_bootstrap_grants")
        return frozenset(str(r[0]) for r in rows)

    def revoke_bootstrap_grant(self, identity: str) -> None:
        self._db.execute(
            "INSERT INTO revoked_bootstrap_grants (identity, revoked_at)"
            " VALUES (?, ?) ON CONFLICT (identity) DO NOTHING",
            (identity[:300], _now()),
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "SCHEDULER_PERSISTENT",
    "VOICE_ACTIVATION",
    "InMemoryOwnerSettings",
    "OwnerSettingsStore",
    "SQLiteOwnerSettings",
]
