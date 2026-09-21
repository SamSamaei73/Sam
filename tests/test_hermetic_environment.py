"""Regression: the owner's shell can never change what a test models.

A real ``GEMINI_API_KEY`` exported in the developer's shell once made tests that
model "Gemini is not configured" fail. The fix is test isolation only
(``tests/conftest.py``); production still reads its environment as before.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from sam.core.config import Settings
from tests import conftest
from tests.conftest import PROVIDER_CREDENTIAL_ENV

ROOT = Path(__file__).resolve().parents[1]

# The tests that inherited real credentials before the fix.
FORMERLY_LEAKING = (
    "tests/test_claude_provider.py::test_missing_api_key_is_an_authentication_error",
    "tests/test_desktop_bridge.py::test_status_is_honest_about_capabilities",
    "tests/test_desktop_bridge.py::test_permissions_list_and_revoke_only",
    "tests/test_desktop_bridge.py::test_tts_not_configured",
    "tests/test_desktop_bridge.py::test_bootstrap_grants_are_exactly_the_documented_set_and_audited",
    "tests/test_models_desktop.py::test_without_a_gemini_key_gemini_is_disabled_and_makes_no_request",
    "tests/test_tts_gemini.py::test_disabled_by_default_and_needs_a_local_key",
    "tests/test_tts_gemini.py::test_no_key_means_no_persian_voice_and_persian_stays_text_only",
)


def test_every_provider_credential_variable_is_covered() -> None:
    """The isolation list includes every credential the Sam and Claude Code
    authentication paths can read."""

    for name in (
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "FISH_AUDIO_API_KEY",
    ):
        assert name in PROVIDER_CREDENTIAL_ENV


def test_a_test_starts_with_no_provider_credential_in_its_environment() -> None:
    leaked = sorted(name for name in PROVIDER_CREDENTIAL_ENV if name in os.environ)
    assert leaked == []
    settings = Settings()
    assert settings.gemini_api_key is None
    assert settings.anthropic_api_key is None
    assert settings.fish_audio_api_key is None


def test_the_developers_real_dotenv_is_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stand in for the repo's real ``.env`` with a tmp file holding a key."""

    dotenv = tmp_path / ".env"
    dotenv.write_text("GEMINI_API_KEY=fake-from-the-developers-dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(conftest, "REPO_DOTENV", dotenv.resolve())
    assert Settings().gemini_api_key is None
    # An explicit, different env file is still honoured.
    other = tmp_path / "other.env"
    other.write_text("GEMINI_API_KEY=fake-from-an-explicit-file\n")
    explicit = Settings(_env_file=other)  # type: ignore[call-arg]
    assert explicit.gemini_api_key is not None
    assert explicit.gemini_api_key.get_secret_value() == "fake-from-an-explicit-file"


def test_any_other_dotenv_is_still_read_as_in_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("GEMINI_API_KEY=fake-from-another-dotenv\n")
    monkeypatch.chdir(tmp_path)
    key = Settings().gemini_api_key
    assert key is not None
    assert key.get_secret_value() == "fake-from-another-dotenv"


def test_a_configured_test_injects_its_own_fake_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake-injected-by-the-test")
    key = Settings().gemini_api_key
    assert key is not None
    assert key.get_secret_value() == "fake-injected-by-the-test"


def test_an_exported_key_cannot_alter_unconfigured_environment_tests() -> None:
    """Run the formerly leaking tests in a child pytest whose parent environment
    exports fake credentials, exactly as the owner's shell does."""

    env = {
        **os.environ,
        **{name: f"fake-exported-{name.lower()}" for name in PROVIDER_CREDENTIAL_ENV},
    }
    # Fake values that would fail Settings' length validators (DESKTOP_*) are
    # exported too: the isolation must clear them before Settings validates.
    process = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            *FORMERLY_LEAKING,
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=110,
    )
    assert process.returncode == 0, process.stdout[-1500:]
    assert f"{len(FORMERLY_LEAKING)} passed" in process.stdout
