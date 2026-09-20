"""Tests for validated application configuration."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from pytest import MonkeyPatch

from sam.core.config import Settings

_SETTING_NAMES = (
    "APP_NAME",
    "APP_ENV",
    "LOG_LEVEL",
    "API_HOST",
    "API_PORT",
    "ANTHROPIC_API_KEY",
    "CLAUDE_MODEL",
    "CLAUDE_BASE_URL",
    "CLAUDE_TIMEOUT",
    "CLAUDE_MAX_RETRIES",
)


def test_default_configuration_is_safe(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """Defaults identify Sam and bind development to loopback only."""

    monkeypatch.chdir(tmp_path)
    for name in _SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.app_name == "Sam"
    assert settings.app_env == "development"
    assert settings.log_level == "INFO"
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000
    assert settings.anthropic_api_key is None
    assert settings.claude_model == "claude-sonnet-4-5"
    assert str(settings.claude_base_url) == "https://api.anthropic.com/"
    assert settings.claude_timeout == 30.0
    assert settings.claude_max_retries == 2


def test_configuration_loads_environment_variables(monkeypatch: MonkeyPatch) -> None:
    """Settings read the documented uppercase environment variables."""

    monkeypatch.setenv("APP_NAME", "Configured Service")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("API_HOST", "127.0.0.2")
    monkeypatch.setenv("API_PORT", "9000")

    settings = Settings()

    assert settings.app_name == "Configured Service"
    assert settings.app_env == "test"
    assert settings.log_level == "DEBUG"
    assert settings.api_host == "127.0.0.2"
    assert settings.api_port == 9000


def test_configuration_loads_claude_settings_without_exposing_the_key(
    monkeypatch: MonkeyPatch,
) -> None:
    """Claude configuration is typed and secrets remain masked by Pydantic."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("CLAUDE_MODEL", "test-model")
    monkeypatch.setenv("CLAUDE_BASE_URL", "https://proxy.example")
    monkeypatch.setenv("CLAUDE_TIMEOUT", "12.5")
    monkeypatch.setenv("CLAUDE_MAX_RETRIES", "1")

    settings = Settings()

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "x"
    assert '"x"' not in repr(settings)
    assert settings.claude_model == "test-model"
    assert str(settings.claude_base_url) == "https://proxy.example/"
    assert settings.claude_timeout == 12.5
    assert settings.claude_max_retries == 1


def test_configuration_rejects_invalid_values() -> None:
    """Invalid required text and port values fail validation."""

    with pytest.raises(ValidationError):
        Settings(app_name="")

    with pytest.raises(ValidationError):
        Settings(api_port=0)

    with pytest.raises(ValidationError):
        Settings(api_port=65536)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), 301])
def test_configuration_rejects_unsafe_timeouts(value: float) -> None:
    """Timeouts are finite and bounded for synchronous provider calls."""

    with pytest.raises(ValidationError):
        Settings(claude_timeout=value)


@pytest.mark.parametrize("value", [-1, 4])
def test_configuration_rejects_unsafe_retry_counts(value: int) -> None:
    """Retries remain explicit and bounded."""

    with pytest.raises(ValidationError):
        Settings(claude_max_retries=value)


def test_configuration_rejects_blank_provider_values() -> None:
    """Blank credentials and model names fail closed."""

    with pytest.raises(ValidationError):
        Settings(anthropic_api_key=SecretStr("   "))

    with pytest.raises(ValidationError):
        Settings(claude_model="   ")

    with pytest.raises(ValidationError):
        Settings.model_validate({"claude_base_url": "http://proxy.example"})


def test_desktop_bridge_token_is_optional_secret_and_min_length() -> None:
    """The bridge token is never printed and must resist guessing."""

    assert Settings().desktop_bridge_token is None
    with pytest.raises(ValidationError):
        Settings(desktop_bridge_token=SecretStr("short"))
    token = "z" * 40
    settings = Settings(desktop_bridge_token=SecretStr(token))
    assert token not in repr(settings)
    assert token not in str(settings.model_dump())


def test_desktop_voice_settings_defaults() -> None:
    settings = Settings()
    assert settings.fish_audio_voice_reference is None
    assert settings.fish_audio_model == "s2.1-pro-free"


def test_desktop_step_up_secret_is_optional_secret_and_min_length() -> None:
    assert Settings().desktop_step_up_secret is None
    with pytest.raises(ValidationError):
        Settings(desktop_step_up_secret=SecretStr("too-short"))
    secret = "s" * 24
    settings = Settings(desktop_step_up_secret=SecretStr(secret))
    assert secret not in repr(settings)


def test_step_up_secret_must_differ_from_bridge_token() -> None:
    shared = "x" * 40
    with pytest.raises(ValidationError):
        Settings(
            desktop_bridge_token=SecretStr(shared),
            desktop_step_up_secret=SecretStr(shared),
        )
    Settings(
        desktop_bridge_token=SecretStr(shared),
        desktop_step_up_secret=SecretStr("y" * 24),
    )
