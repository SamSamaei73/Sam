"""Suite-wide safety: tests are hermetic.

* No test may discover or run the machine's real ``claude``. The Claude
  subscription adapter probes ``claude --version``/``--help``/``auth status``
  before use. Tests that need a CLI pass an explicit fake executable; everything
  else must never find the owner's real installation (which would be slow,
  environment-dependent and would touch a real login).
* No test may inherit a provider credential from the developer's shell or from
  the developer's real ``.env`` file. A test that models "not configured" must
  really be unconfigured even when the owner has exported ``GEMINI_API_KEY`` (or
  any other credential), and a test that models "configured" must inject its own
  fake value.

This is TEST isolation only: production reads its environment exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from sam.core.config import Settings
from sam.models.providers import claude_subscription

# The developer's real ``.env`` (the README tells the owner to keep GEMINI_API_KEY
# there). Tests never inherit it; any OTHER ``.env`` (a test's tmp directory) and
# any explicit ``_env_file=`` are read as in production.
REPO_DOTENV = Path(__file__).resolve().parents[1] / ".env"

# Every environment variable that can carry a provider credential, endpoint
# override or authentication-source selector. Settings' own credential fields,
# the Claude Code sources that outrank a subscription login, cloud-provider
# switches, and the other providers Sam deliberately does not use.
PROVIDER_CREDENTIAL_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "FISH_AUDIO_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_GENAI_API_KEY",
        "OPENAI_API_KEY",
        "XAI_API_KEY",
        "DESKTOP_BRIDGE_TOKEN",
        "DESKTOP_STEP_UP_SECRET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
    | claude_subscription.STRIPPED_ENV
)


@pytest.fixture(autouse=True)
def _never_find_the_real_claude_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_subscription, "trusted_candidates", lambda: ())


def _is_the_repo_dotenv(env_file: object) -> bool:
    files = env_file if isinstance(env_file, (list, tuple)) else [env_file]
    for item in files:
        if isinstance(item, (str, Path)) and Path(item).resolve() == REPO_DOTENV:
            return True
    return False


def _sources_without_the_developers_dotenv(
    cls: type[BaseSettings],
    settings_cls: type[BaseSettings],
    init_settings: PydanticBaseSettingsSource,
    env_settings: PydanticBaseSettingsSource,
    dotenv_settings: PydanticBaseSettingsSource,
    file_secret_settings: PydanticBaseSettingsSource,
) -> tuple[PydanticBaseSettingsSource, ...]:
    """The stock source order, minus the dotenv source when (and only when) it
    would read the developer's real ``.env``. Decided at construction time, so a
    test that changes directory to a tmp dir still exercises real dotenv reading."""

    sources: tuple[PydanticBaseSettingsSource, ...] = (
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    )
    env_file: Any = getattr(dotenv_settings, "env_file", None)
    if _is_the_repo_dotenv(env_file):
        return (init_settings, env_settings, file_secret_settings)
    return sources


@pytest.fixture(autouse=True)
def _hermetic_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every provider credential from the process environment, and stop
    ``Settings`` from reading the developer's real ``.env``, before each test."""

    for name in PROVIDER_CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        Settings,
        "settings_customise_sources",
        classmethod(_sources_without_the_developers_dotenv),
    )
