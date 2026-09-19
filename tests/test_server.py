"""Tests for development server configuration."""

from pathlib import Path

import uvicorn
from pytest import MonkeyPatch

from sam.core.config import get_settings
from sam.server import run_server


def test_server_uses_dotenv_host_and_port(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """The documented .env values control the development server binding."""

    captured: dict[str, object] = {}

    def capture_run(app: str, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("API_HOST", raising=False)
    monkeypatch.delenv("API_PORT", raising=False)
    (tmp_path / ".env").write_text(
        "API_HOST=127.0.0.2\nAPI_PORT=8123\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(uvicorn, "run", capture_run)
    get_settings.cache_clear()

    try:
        run_server()
    finally:
        get_settings.cache_clear()

    assert captured == {
        "app": "sam.main:app",
        "host": "127.0.0.2",
        "port": 8123,
    }
