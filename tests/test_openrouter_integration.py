"""Explicit OpenRouter opt-in across configuration, routing, and bootstrap.

All credentials are synthetic, HTTP is mocked, and no real .env or Keychain
is read. These tests cover the boundaries beyond the HTTP adapter itself.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from sam.agent.models import Message, MessageRole
from sam.core.config import Settings
from sam.models.adapter import RoutedLLMProvider
from sam.models.audit import InMemoryRoutingAuditSink, RoutingAuditEvent
from sam.models.errors import ProviderFailure, RegistryError
from sam.models.factory import ModelSettingsStore, build_model_router
from sam.models.models import (
    Availability,
    BillingMode,
    FailureCategory,
    PrivacyClass,
    ProviderId,
    ReasonCode,
    TaskProfile,
)
from sam.models.openrouter_config import DEFAULT_OPENROUTER_MODEL
from sam.models.policies import CostPolicy, OwnerPreferences
from sam.models.providers.fake import FakeProvider
from sam.models.registry import ModelSpec, ProviderRegistry, build_registry
from sam.models.router import ModelRouter
from sam.system.backend import (
    MAX_OPENROUTER_ENV_BYTES,
    OPENROUTER_ENV_NAMES,
    _load_openrouter_environment,
)
from sam.system.secrets import (
    CredentialSource,
    MacOSKeychainSecretStore,
    resolve_credentials,
)
from tests.models_support import request
from tests.test_system_security import FakeKeyring

OPENROUTER = ProviderId.OPENROUTER
FAKE_KEY = "test-openrouter-credential-only"


def response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": DEFAULT_OPENROUTER_MODEL,
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Mocked answer"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        },
    )


def test_dotenv_loads_the_openrouter_key_and_model_without_exposing_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"OPENROUTER_API_KEY={FAKE_KEY}\n"
        "OPENROUTER_MODEL=meta-llama/llama-3.3-70b-instruct:free\n"
    )
    settings = Settings()
    assert settings.openrouter_api_key == SecretStr(FAKE_KEY)
    assert settings.openrouter_model == "meta-llama/llama-3.3-70b-instruct:free"
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in settings.model_dump_json()
    monkeypatch.setenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)
    assert Settings().openrouter_model == DEFAULT_OPENROUTER_MODEL


@pytest.mark.parametrize(
    "model",
    [
        "",
        "no-vendor",
        "vendor/model\n",
        "vendor/model extra",
        "vendor/model/x",
        "https://example.org/model",
        "openrouter/auto",
        "openrouter/free",
        "OpenRouter/auto",
        "@owner/router",
        "v/" + "x" * 161,
    ],
)
def test_config_rejects_urls_routers_and_malformed_model_slugs(model: str) -> None:
    with pytest.raises(ValidationError):
        Settings(openrouter_model=model)


def test_blank_key_is_rejected_and_model_validation_stays_provider_specific() -> None:
    with pytest.raises(ValidationError):
        Settings(openrouter_api_key=SecretStr("   "))
    with pytest.raises(RegistryError):
        ModelSpec(DEFAULT_OPENROUTER_MODEL)
    allowed = ModelSpec(DEFAULT_OPENROUTER_MODEL, provider_id=OPENROUTER)
    descriptor = build_registry(gemini_configured=True).get(ProviderId.GEMINI_FREE)
    with pytest.raises(RegistryError):
        replace(descriptor, models=(allowed,))


def test_speech_configuration_uses_the_same_key_and_validated_model_voice() -> None:
    settings = Settings(openrouter_api_key=SecretStr(FAKE_KEY))
    assert settings.openrouter_tts_model == "openai/gpt-4o-mini-tts-2025-12-15"
    assert settings.openrouter_tts_voice == "alloy"
    with pytest.raises(ValidationError):
        Settings(openrouter_tts_model="https://example.org/model")
    with pytest.raises(ValidationError):
        Settings(openrouter_tts_voice="bad voice")


def test_unconfigured_openrouter_is_disabled_without_requests() -> None:
    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("unconfigured provider made a request")

    router, store = build_model_router(
        Settings(claude_subscription_enabled=False),
        openrouter_transport=httpx.MockTransport(forbidden),
    )
    assert store.preferences().preferred_provider is None
    assert not router.registry().get(OPENROUTER).enabled
    assert router.provider_state(OPENROUTER) is Availability.DISABLED
    assert not router.any_usable()
    assert router.execute(request()).status is FailureCategory.COST_BLOCKED


@pytest.mark.parametrize("profile", list(TaskProfile))
def test_configured_key_selects_openrouter_for_all_llm_task_profiles(
    profile: TaskProfile,
) -> None:
    calls: list[httpx.Request] = []

    def respond(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        return response()

    audit = InMemoryRoutingAuditSink()
    router, store = build_model_router(
        Settings(openrouter_api_key=SecretStr(FAKE_KEY)),
        openrouter_transport=httpx.MockTransport(respond),
        audit=audit,
    )
    assert store.preferences().preferred_provider is OPENROUTER
    descriptor = router.registry().get(OPENROUTER)
    assert descriptor.enabled and descriptor.billing_mode is BillingMode.PAID_API
    assert not descriptor.fallback_eligible and descriptor.external_processing
    assert router.any_usable()
    result = router.execute(request(profile=profile))
    assert result.status is FailureCategory.SUCCESS
    assert result.provider_id is OPENROUTER
    assert result.model_id == DEFAULT_OPENROUTER_MODEL
    assert not result.fallback_used
    assert len(calls) == 1
    assert calls[0].headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert len(audit.events()) == 1
    event = audit.events()[0]
    assert event.cost_class is BillingMode.PAID_API
    assert event.model_id == DEFAULT_OPENROUTER_MODEL
    assert FAKE_KEY not in event.model_dump_json()
    with pytest.raises(ValidationError):
        RoutingAuditEvent.model_validate(
            {**event.model_dump(), "provider_id": ProviderId.GEMINI_FREE}
        )
    for provider_id in (ProviderId.OPENAI_API, ProviderId.GROK_API):
        assert router.provider_state(provider_id) is Availability.DISABLED


def test_existing_agent_llm_adapter_uses_the_configured_openrouter() -> None:
    router, _ = build_model_router(
        Settings(openrouter_api_key=SecretStr(FAKE_KEY)),
        openrouter_transport=httpx.MockTransport(lambda _: response()),
    )
    result = RoutedLLMProvider(router).complete(
        [Message(role=MessageRole.USER, content="Explain this function.")]
    )
    assert result.message.content == "Mocked answer"
    assert result.model == DEFAULT_OPENROUTER_MODEL


def test_trusted_paid_opt_in_is_scoped_to_openrouter_without_paid_fallback() -> None:
    registry = build_registry(gemini_configured=False, openrouter_configured=True)
    descriptor = registry.get(OPENROUTER)
    with pytest.raises(RegistryError):
        ProviderRegistry([descriptor])
    with pytest.raises(RegistryError):
        ProviderRegistry(
            [replace(descriptor, fallback_eligible=True)],
            allowed_paid_providers=frozenset({OPENROUTER}),
        )
    with pytest.raises(RegistryError):
        ProviderRegistry([], allowed_paid_providers=frozenset({ProviderId.OPENAI_API}))
    policy = CostPolicy(allowed_paid_providers=frozenset({OPENROUTER}))
    assert policy.allows(BillingMode.PAID_API, OPENROUTER)
    assert not policy.allows(BillingMode.PAID_API, ProviderId.OPENAI_API)
    assert not policy.allows(BillingMode.PAID_API)
    assert not CostPolicy().allows(BillingMode.PAID_API, OPENROUTER)
    assert not policy.allow_paid_api and not policy.allow_paid_fallback
    with pytest.raises(ValueError):
        CostPolicy(allowed_paid_providers=frozenset({ProviderId.GROK_API}))


def test_an_injected_owner_preference_is_preserved_and_never_falls_back_to_paid() -> (
    None
):
    store = ModelSettingsStore(
        preferences=OwnerPreferences(preferred_provider=ProviderId.CLAUDE_SUBSCRIPTION)
    )
    router, returned_store = build_model_router(
        Settings(openrouter_api_key=SecretStr(FAKE_KEY)), store=store
    )
    assert returned_store is store
    assert store.preferences().preferred_provider is ProviderId.CLAUDE_SUBSCRIPTION
    assert (OPENROUTER, ReasonCode.COST_BLOCKED) in router.plan(request()).excluded
    assert not router.any_usable()

    # Even a failure AFTER planning must not lead to paid fallback.
    claude = FakeProvider(
        ProviderId.CLAUDE_SUBSCRIPTION,
        failure=ProviderFailure(FailureCategory.TIMEOUT, "timeout"),
    )
    paid = FakeProvider(OPENROUTER)
    router = ModelRouter(
        registry=router.registry(),
        providers={ProviderId.CLAUDE_SUBSCRIPTION: claude, OPENROUTER: paid},
        preferences=store.preferences,
        content_policy=store.content,
        audit=InMemoryRoutingAuditSink(),
        cost=CostPolicy(allowed_paid_providers=frozenset({OPENROUTER})),
    )
    assert router.execute(request()).status is FailureCategory.TIMEOUT
    assert claude.call_count == 1 and paid.call_count == 0


def test_secret_and_free_variant_privacy_rules_apply_to_openrouter() -> None:
    router, _ = build_model_router(
        Settings(
            openrouter_api_key=SecretStr(FAKE_KEY),
            openrouter_model="vendor/model:free",
        )
    )
    assert router.execute(request("speaker embedding: stored")).status is (
        FailureCategory.POLICY_BLOCKED
    )
    plan = router.plan(request(privacy=PrivacyClass.PRIVATE))
    assert (OPENROUTER, ReasonCode.PRIVACY_RESTRICTED) in plan.excluded


def test_openrouter_credentials_honor_env_in_production_with_optional_keychain() -> (
    None
):
    keyring = FakeKeyring()
    store = MacOSKeychainSecretStore(keyring_module=keyring, platform="darwin")
    store.set("openrouter_api_key", "synthetic-keychain-key")
    store.set("gemini_api_key", "synthetic-gemini-keychain-key")
    configured = Settings(
        app_env="production",
        openrouter_api_key=SecretStr(FAKE_KEY),
        gemini_api_key=SecretStr("ambient-gemini-ignored"),
    )
    resolved, report = resolve_credentials(configured, store)
    assert resolved.openrouter_api_key == SecretStr(FAKE_KEY)
    assert report.sources["openrouter_api_key"] is CredentialSource.ENVIRONMENT
    assert resolved.gemini_api_key == SecretStr("synthetic-gemini-keychain-key")
    assert report.sources["gemini_api_key"] is CredentialSource.KEYCHAIN

    resolved, report = resolve_credentials(Settings(app_env="production"), store)
    assert resolved.openrouter_api_key == SecretStr("synthetic-keychain-key")
    assert report.sources["openrouter_api_key"] is CredentialSource.KEYCHAIN
    keyring.fail = True
    resolved, report = resolve_credentials(Settings(app_env="production"), store)
    assert resolved.openrouter_api_key is None
    assert report.sources["openrouter_api_key"] is CredentialSource.UNAVAILABLE
    resolved, report = resolve_credentials(configured, store)
    assert resolved.openrouter_api_key == SecretStr(FAKE_KEY)
    assert report.sources["openrouter_api_key"] is CredentialSource.ENVIRONMENT


def test_packaged_backend_reads_only_explicit_openrouter_dotenv_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv = tmp_path / "owner.env"
    dotenv.write_text(
        f"OPENROUTER_API_KEY={FAKE_KEY}\n"
        "OPENROUTER_MODEL=vendor/model:free\n"
        "OPENROUTER_TTS_MODEL=openai/gpt-4o-mini-tts-2025-12-15\n"
        "OPENROUTER_TTS_VOICE=nova\n"
        "ANTHROPIC_API_KEY=never-import-this\n"
        "APP_ENV=development\n"
        "SAM_STORAGE=memory\n"
    )
    monkeypatch.setenv("SAM_OPENROUTER_ENV_FILE", str(dotenv))
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SAM_STORAGE", "sqlite")
    monkeypatch.setenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)
    # Register the introduced key with monkeypatch for cleanup after loading.
    monkeypatch.setenv("OPENROUTER_API_KEY", "temporary")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setenv("OPENROUTER_TTS_MODEL", "temporary")
    monkeypatch.delenv("OPENROUTER_TTS_MODEL")
    monkeypatch.setenv("OPENROUTER_TTS_VOICE", "temporary")
    monkeypatch.delenv("OPENROUTER_TTS_VOICE")
    assert _load_openrouter_environment() is None
    assert os.environ["OPENROUTER_API_KEY"] == FAKE_KEY
    assert os.environ["OPENROUTER_MODEL"] == DEFAULT_OPENROUTER_MODEL
    assert os.environ["OPENROUTER_TTS_MODEL"] == "openai/gpt-4o-mini-tts-2025-12-15"
    assert os.environ["OPENROUTER_TTS_VOICE"] == "nova"
    assert os.environ["APP_ENV"] == "production"
    assert os.environ["SAM_STORAGE"] == "sqlite"
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_packaged_dotenv_never_expands_other_environment_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENROUTER_API_KEY=${UNRELATED_SECRET}\n")
    monkeypatch.setenv("SAM_OPENROUTER_ENV_FILE", str(dotenv))
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-expand")
    monkeypatch.setenv("OPENROUTER_API_KEY", "temporary")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert _load_openrouter_environment() is None
    assert os.environ["OPENROUTER_API_KEY"] == "${UNRELATED_SECRET}"


def test_packaged_dotenv_invalid_value_imports_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv = tmp_path / "invalid-value.env"
    dotenv.write_bytes(
        f"OPENROUTER_API_KEY={FAKE_KEY}\n".encode()
        + b"OPENROUTER_TTS_VOICE=alloy\x00invalid\n"
    )
    monkeypatch.setenv("SAM_OPENROUTER_ENV_FILE", str(dotenv))
    for name in OPENROUTER_ENV_NAMES:
        monkeypatch.setenv(name, "temporary")
        monkeypatch.delenv(name)
    assert _load_openrouter_environment() == "openrouter_env_file_invalid"
    assert all(name not in os.environ for name in OPENROUTER_ENV_NAMES)


def test_packaged_dotenv_path_is_explicit_bounded_and_fails_without_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _load_openrouter_environment() is None
    monkeypatch.setenv("SAM_OPENROUTER_ENV_FILE", ".env")
    assert _load_openrouter_environment() == "openrouter_env_file_must_be_absolute"
    monkeypatch.setenv("SAM_OPENROUTER_ENV_FILE", str(tmp_path / "missing"))
    assert _load_openrouter_environment() == "openrouter_env_file_invalid"
    dotenv = tmp_path / "too-large.env"
    dotenv.write_bytes(b"x" * (MAX_OPENROUTER_ENV_BYTES + 1))
    monkeypatch.setenv("SAM_OPENROUTER_ENV_FILE", str(dotenv))
    assert _load_openrouter_environment() == "openrouter_env_file_invalid"
    assert "OPENROUTER_API_KEY" not in os.environ
