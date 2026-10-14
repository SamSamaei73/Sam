"""Release configuration and live model health: no real model requests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sam.core.config import get_settings
from sam.models.factory import ModelSettingsStore
from sam.models.models import Availability, FailureCategory, ProviderId
from sam.models.providers import claude_subscription as cs
from sam.models.providers.fake import FakeProvider
from tests.desktop_support import Bridge
from tests.models_support import make_rig


def test_packaged_startup_ignores_cwd_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "CLAUDE_SUBSCRIPTION_ENABLED=false\nDEPLOYMENT_MODE=server\nOPENROUTER_API_KEY=untrusted\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_SUBSCRIPTION_ENABLED", raising=False)
    monkeypatch.setenv("SAM_PACKAGED_BACKEND", "1")
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.claude_subscription_enabled
        assert settings.deployment_mode.value == "owner_local"
        assert settings.openrouter_api_key is None
    finally:
        get_settings.cache_clear()


def test_preflight_uses_only_sanitized_production_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envs: list[dict[str, str]] = []

    def runner(argv: list[str], data: bytes, **kwargs: Any) -> tuple[int, bytes, bytes]:
        assert data == b""  # preflight cannot send any prompt
        envs.append(dict(kwargs["env"]))
        if "--version" in argv:
            return 0, b"2.1.286 (Claude Code)", b""
        if "--help" in argv:
            return 0, "\n".join(cs.REQUIRED_HELP_FLAGS).encode(), b""
        assert argv[1:] == ["auth", "status", "--json"]
        return (
            0,
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "subscriptionType": "max",
                }
            ).encode(),
            b"",
        )

    monkeypatch.setattr(
        cs, "resolve_executable", lambda _configured: Path("/trusted/claude")
    )
    provider = cs.ClaudeSubscriptionProvider(
        environ={
            "APP_ENV": "production",
            "HOME": "/Users/owner",
            "USER": "owner",
            "LOGNAME": "owner",
            "PATH": "/untrusted",
            "ANTHROPIC_API_KEY": "must-never-leak",
            "CLAUDE_CODE_OAUTH_TOKEN": "must-never-leak",
        },
        managed_policy_paths=(),
        runner=runner,
    )
    assert provider.availability() is Availability.AVAILABLE
    assert provider.detail() is None
    assert len(envs) == 3
    for env in envs:
        assert env["HOME"] == "/Users/owner"
        assert env["USER"] == env["LOGNAME"] == "owner"
        assert env["PATH"] == cs.SAFE_PATH
        assert "must-never-leak" not in json.dumps(env)
        assert set(env) <= {
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "PATH",
            "TMPDIR",
            "TERM",
            "NO_COLOR",
        }


@pytest.mark.parametrize(
    "environment,reason",
    [
        ({}, "no_home"),
        ({"HOME": "relative"}, "no_home"),
        ({"HOME": "/Users/owner", "USER": "../other"}, "invalid_account"),
    ],
)
def test_bad_production_identity_has_precise_safe_reason(
    environment: dict[str, str], reason: str
) -> None:
    provider = cs.ClaudeSubscriptionProvider(
        environ=environment, managed_policy_paths=()
    )
    assert provider.availability() is Availability.UNAVAILABLE
    assert provider.detail() == reason


class RecoveringClaude(FakeProvider):
    def __init__(self) -> None:
        super().__init__(
            ProviderId.CLAUDE_SUBSCRIPTION, availability=Availability.UNAUTHORIZED
        )

    def detail(self) -> str | None:
        return (
            "not_logged_in"
            if self._availability is not Availability.AVAILABLE
            else None
        )

    def refresh(self) -> Availability:
        self._availability = Availability.AVAILABLE
        return self._availability


def test_retry_updates_health_and_removes_model_blocker_without_restart() -> None:
    claude = RecoveringClaude()
    rig = make_rig(claude=claude, gemini_enabled=False)
    bridge = Bridge(model_router=rig.router, model_settings=ModelSettingsStore())
    before = bridge.get("/status").json()
    assert before["agent"] == "not_configured"
    assert before["model_issue"]["reason_code"] == "not_logged_in"
    assert before["model_issue"]["message"] == "Claude needs authentication"
    assert before["health"]["status"] == "degraded"
    rig.health.record_failure(
        ProviderId.CLAUDE_SUBSCRIPTION, FailureCategory.AUTH_FAILURE
    )
    providers = bridge.get("/models").json()
    assert providers["providers"][0]["state"] == "available"
    after = bridge.get("/status").json()
    assert after["agent"] == "configured" and after["model_issue"] is None
    assert after["health"]["status"] == "ready"
    assert providers["paid_fallback"] == "off"
    assert all(
        not p["enabled"]
        for p in providers["providers"]
        if p["cost_class"] == "paid_api"
    )
    assert claude.call_count == rig.paid_calls() == 0


def test_retry_cannot_clear_usage_limit() -> None:
    claude = RecoveringClaude()
    rig = make_rig(claude=claude, gemini_enabled=False)
    rig.health.record_failure(
        ProviderId.CLAUDE_SUBSCRIPTION, FailureCategory.RATE_LIMIT, usage_limit=True
    )
    rig.router.refresh_availability()
    assert (
        rig.router.provider_state(ProviderId.CLAUDE_SUBSCRIPTION)
        is Availability.USAGE_LIMIT
    )
    assert claude.call_count == 0


def test_unknown_provider_detail_never_reaches_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude = RecoveringClaude()
    monkeypatch.setattr(
        claude, "detail", lambda: "secret-response-body-should-never-appear"
    )
    monkeypatch.setattr(claude, "refresh", lambda: Availability.UNAUTHORIZED)
    rig = make_rig(claude=claude, gemini_enabled=False)
    bridge = Bridge(model_router=rig.router, model_settings=ModelSettingsStore())
    assert "secret-response-body" not in bridge.get("/status").text
    assert "secret-response-body" not in bridge.get("/models").text
