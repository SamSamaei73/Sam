"""Phase 17 production security invariants: credential boundary, log
redaction and bounds, the outbound-network inventory, and that production
never selects test fakes. No Keychain, network or provider is used."""

from __future__ import annotations

import ast
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

import sam as sam_package
from sam.core import logging as sam_logging
from sam.core.config import Settings
from sam.system import secrets
from sam.system.secrets import (
    CredentialSource,
    MacOSKeychainSecretStore,
    SecretUnavailable,
    credential_store_for,
    resolve_credentials,
)
from tests.storage_support import Sam

SRC = Path(sam_package.__file__).parent
FAKE_KEY = "AIza" + "S" * 35  # synthetic


class FakeKeyring:
    """A stand-in for the ``keyring`` module (never the real Keychain)."""

    def __init__(self, backend_module: str = "keyring.backends.macOS") -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.fail = False
        backend = type("Backend", (), {"__module__": backend_module})
        self._backend = backend()

    def get_keyring(self) -> Any:
        return self._backend

    def get_password(self, service: str, name: str) -> str | None:
        if self.fail:
            raise RuntimeError("keychain locked")
        return self.items.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.items[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        self.items.pop((service, name), None)


# ------------------------------------------------------------ credentials


def test_only_the_native_macos_keychain_backend_is_accepted() -> None:
    with pytest.raises(SecretUnavailable) as error:
        MacOSKeychainSecretStore(keyring_module=FakeKeyring(), platform="linux")
    assert error.value.code == "keychain_unsupported_platform"
    for backend in ("keyrings.alt.file", "keyring.backends.fail", "plaintext"):
        with pytest.raises(SecretUnavailable):
            MacOSKeychainSecretStore(
                keyring_module=FakeKeyring(backend), platform="darwin"
            )


def test_keychain_credentials_are_used_and_the_environment_wins() -> None:
    keyring = FakeKeyring()
    store = MacOSKeychainSecretStore(keyring_module=keyring, platform="darwin")
    store.set("gemini_api_key", FAKE_KEY)
    settings, report = resolve_credentials(Settings(), store, "ok")
    assert settings.gemini_api_key is not None
    assert settings.gemini_api_key.get_secret_value() == FAKE_KEY
    assert report.sources["gemini_api_key"] is CredentialSource.KEYCHAIN
    assert report.sources["fish_audio_api_key"] is CredentialSource.NOT_CONFIGURED
    env = Settings(gemini_api_key=SecretStr("A" * 39))
    settings, report = resolve_credentials(env, store, "ok")
    assert settings.gemini_api_key == env.gemini_api_key
    assert report.sources["gemini_api_key"] is CredentialSource.ENVIRONMENT
    assert FAKE_KEY not in repr(report) and FAKE_KEY[:8] not in repr(report)


def test_a_keychain_failure_never_falls_back_to_plaintext() -> None:
    keyring = FakeKeyring()
    store = MacOSKeychainSecretStore(keyring_module=keyring, platform="darwin")
    store.set("gemini_api_key", FAKE_KEY)
    keyring.fail = True
    settings, report = resolve_credentials(Settings(), store, "ok")
    assert settings.gemini_api_key is None  # unavailable: the provider is off
    assert report.sources["gemini_api_key"] is CredentialSource.UNAVAILABLE
    assert report.degraded
    with pytest.raises(SecretUnavailable):
        store.get("not_a_known_credential")


def test_a_required_keychain_that_cannot_open_makes_credentials_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def broken(**kwargs: Any) -> Any:
        raise SecretUnavailable("keychain_backend_missing")

    monkeypatch.setattr(secrets, "MacOSKeychainSecretStore", broken)
    store, status = credential_store_for(Settings(sam_keychain="on"))
    assert status == "keychain_backend_missing"
    settings, report = resolve_credentials(Settings(), store, status)
    assert all(s is CredentialSource.UNAVAILABLE for s in report.sources.values())
    assert settings.gemini_api_key is None
    assert credential_store_for(Settings(sam_keychain="off")) == (None, "disabled")
    sam = Sam(tmp_path, sam_keychain="on")
    keychain = {s["name"]: s for s in sam.status()["health"]["subsystems"]}["keychain"]
    assert keychain["status"] == "unavailable"
    assert sam.status()["health"]["status"] == "degraded"
    sam.stop()


def test_credentials_never_reach_the_frontend_status(tmp_path: Path) -> None:
    sam = Sam(tmp_path, gemini_api_key=SecretStr(FAKE_KEY))
    text = sam.client.get(
        "/desktop/v1/status", headers={"x-sam-desktop-token": "t" * 40}
    ).text
    assert FAKE_KEY not in text and FAKE_KEY[:10] not in text
    sam.stop()


# ----------------------------------------------------------------- logging


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-" + "x" * 40,
        "AIza" + "y" * 35,
        "ghp_" + "z" * 36,
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "x-api-key: supersecretvalue123",
        "token=hunter2hunter2",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_logs_are_redacted_including_tracebacks(secret: str) -> None:
    formatter = sam_logging.RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        "t", logging.ERROR, __file__, 1, "value %s", (secret,), None
    )
    try:
        raise RuntimeError(f"provider said {secret}")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()
    text = formatter.format(record)
    core = secret.split()[-1].split("=")[-1].split(": ")[-1]
    assert core not in text
    assert sam_logging.REDACTED in text


def test_the_log_file_is_owner_only_and_bounded(tmp_path: Path) -> None:
    sam_logging.configure_logging("INFO", log_dir=tmp_path / "logs")
    log = logging.getLogger("sam.test.bounded")
    for i in range(12_000):
        log.info("line %05d %s", i, "x" * 400)
    for handler in logging.getLogger().handlers:
        handler.flush()
    files = sorted((tmp_path / "logs").iterdir())
    assert 1 < len(files) <= sam_logging.LOG_BACKUPS + 1
    for file in files:
        assert stat.S_IMODE(os.stat(file).st_mode) == 0o600
        assert file.stat().st_size <= sam_logging.LOG_MAX_BYTES + 1_000
    sam_logging.configure_logging("WARNING")


# ------------------------------------------------------- network inventory

NETWORK_MODULES = {
    "agent/claude.py",  # paid Anthropic API client: NOT wired (PAID_FALLBACK off)
    "models/factory.py",  # httpx transport typing for the Gemini provider
    "models/providers/gemini.py",  # generativelanguage.googleapis.com
    "tts/fish_audio.py",  # api.fish.audio
    "tts/gemini_tts.py",  # generativelanguage.googleapis.com
}
NETWORK_LIBS = {
    "httpx",
    "urllib.request",
    "http.client",
    "socket",
    "requests",
    "aiohttp",
}


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_outbound_network_is_confined_to_the_reviewed_provider_modules() -> None:
    users = {
        str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if _imports(p) & NETWORK_LIBS
    }
    assert users == NETWORK_MODULES
    for package in ("storage", "system", "career", "proactive", "professional"):
        for path in (SRC / package).rglob("*.py"):
            assert not _imports(path) & NETWORK_LIBS, path


def test_production_never_selects_test_code_or_fake_adapters(tmp_path: Path) -> None:
    for path in SRC.rglob("*.py"):
        assert not any(m.startswith("tests") for m in _imports(path)), path
    composition = (SRC / "desktop" / "runtime.py").read_text() + (
        SRC / "main.py"
    ).read_text()
    for banned in (
        "FakeTranscription",
        "FakeSpeech",
        "InMemoryVoiceProfileStore",
        "Fake",
    ):
        assert banned not in composition, banned
    sam = Sam(tmp_path)
    career = sam.runtime.career
    assert career._submitter is None and career._email is None
    assert career.attempts.durable
    sam.stop()
