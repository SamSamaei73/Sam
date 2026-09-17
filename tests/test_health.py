"""Tests for application startup, health, and configuration."""

from typing import Any, cast

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from jarvis.core.config import Settings
from jarvis.main import create_app


def test_application_startup() -> None:
    """The application enters and exits its lifespan successfully."""

    with TestClient(create_app()) as client:
        assert cast(Any, client.app).state.settings.app_name == "ALI-JARVIS"


def test_health() -> None:
    """The health endpoint returns the public service status."""

    settings = Settings(app_name="Test Service", app_env="test")

    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "Test Service",
        "environment": "test",
    }


def test_configuration_loads_environment_variables(monkeypatch: MonkeyPatch) -> None:
    """Settings read the documented uppercase environment variables."""

    monkeypatch.setenv("APP_NAME", "Configured Service")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("API_HOST", "0.0.0.0")
    monkeypatch.setenv("API_PORT", "9000")

    settings = Settings()

    assert settings.app_name == "Configured Service"
    assert settings.app_env == "test"
    assert settings.log_level == "DEBUG"
    assert settings.api_host == "0.0.0.0"
    assert settings.api_port == 9000
