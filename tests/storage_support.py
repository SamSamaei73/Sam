"""Shared helpers for the Phase 17 durability tests.

Everything is SYNTHETIC and local: temporary data directories only (the
suite-wide fixture also points ``SAM_DATA_DIR`` at a temporary directory), no
Keychain, no provider, no network, no real external action.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from sam.agent.core import AgentCore
from sam.core.config import Settings
from sam.desktop.runtime import DesktopRuntime
from sam.main import create_app
from sam.storage.database import Database
from sam.storage.migrations import migrate
from sam.storage.paths import DATABASE_NAME
from tests.desktop_support import HEADERS, TOKEN, StubAgent


def durable_settings(data_dir: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "production",
        "desktop_bridge_token": SecretStr(TOKEN),
        "sam_data_dir": str(data_dir),
        "sam_keychain": "off",
        "log_level": "INFO",
        "claude_subscription_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


class Sam:
    """One Sam process (app + runtime) over a data directory."""

    def __init__(self, data_dir: Path, **overrides: Any) -> None:
        self.data_dir = data_dir
        self.app: FastAPI = create_app(
            durable_settings(data_dir, **overrides),
            agent_core=cast(AgentCore, StubAgent()),
        )
        self.client = client_for(self.app)

    @property
    def runtime(self) -> DesktopRuntime:
        return cast(DesktopRuntime, self.app.state.desktop_runtime)

    @property
    def report(self) -> Any:
        return self.app.state.startup

    def status(self) -> dict[str, Any]:
        response = self.client.get("/desktop/v1/status", headers=HEADERS)
        assert response.status_code == 200, response.text
        data: dict[str, Any] = response.json()
        return data

    def get(self, path: str) -> Any:
        return self.client.get(f"/desktop/v1{path}", headers=HEADERS)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self.client.post(f"/desktop/v1{path}", headers=HEADERS, json=body)

    def stop(self) -> None:
        """Simulated process exit: the database closes and the instance lock
        is released (as the kernel would on a real exit)."""

        durable = self.app.state.durable
        if durable is not None:
            durable.close()


def client_for(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000))


def fresh_db(path: Path) -> Database:
    db = Database(path)
    migrate(db)
    return db


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def db_path(data_dir: Path) -> Path:
    return data_dir / DATABASE_NAME


__all__ = [
    "HEADERS",
    "Sam",
    "client_for",
    "db_path",
    "durable_settings",
    "fresh_db",
    "mode",
]
