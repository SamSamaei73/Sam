"""Tests for application startup and health."""

from typing import Any, cast

from fastapi.testclient import TestClient

from sam.core.config import Settings
from sam.main import create_app


def test_application_startup() -> None:
    """The application enters and exits its lifespan successfully."""

    with TestClient(create_app()) as client:
        assert cast(Any, client.app).state.settings.app_name == "Sam"


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
