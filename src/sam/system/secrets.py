"""The trusted credential boundary.

Provider credentials are resolved in ONE place, at startup, by trusted code.

PRODUCTION (``APP_ENV=production``): the macOS login Keychain item
``app.sam.desktop.credentials`` / ``<name>`` is the ONLY source. An ambient
environment value (shell, launch environment, ``.env``) is IGNORED: it never
overrides or supplements the Keychain, and nothing a prompt, model, task,
web page or the frontend says can select a credential source. There is no
production override switch; an owner who wants a key in production stores it
in the Keychain with ``python -m sam.system.cli secret-set <name>``.

DEVELOPMENT / TEST: an explicitly configured environment value wins (the
existing development mechanism); otherwise the Keychain if enabled; otherwise
the credential is not configured and the integration is off.

The Claude subscription provider has no credential here: it keeps its own
sanitized-authentication boundary (Phase 13), unchanged.

If the Keychain cannot be read, the credential is UNAVAILABLE: there is no
plaintext file, database or ``.env`` fallback and no model is ever asked to
help. Credentials are never stored in SQLite or Obsidian, never sent to the
frontend and never logged (not even a prefix). Existing environment secrets
are never copied into the Keychain automatically; the owner stores one
explicitly with ``python -m sam.system.cli secret-set <name>``.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pydantic import SecretStr

from sam.core.config import Settings

KEYCHAIN_SERVICE = "app.sam.desktop.credentials"
# The ONLY credential names Sam resolves (setting field names).
# ``desktop_step_up_secret`` is the owner's step-up secret: required to set up
# (or replace) the owner voice profile and to approve CRITICAL actions. In
# production it can only come from the Keychain, like every credential.
KNOWN_SECRETS = (
    "anthropic_api_key",
    "desktop_step_up_secret",
    "fish_audio_api_key",
    "gemini_api_key",
)


MIN_STEP_UP_CHARS = 16


class SecretUnavailable(RuntimeError):
    def __init__(self, code: str = "keychain_unavailable") -> None:
        super().__init__(code)
        self.code = code


class CredentialSource(StrEnum):
    ENVIRONMENT = "environment"
    KEYCHAIN = "keychain"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"  # the Keychain failed: fail closed


class SecretStore(Protocol):
    def get(self, name: str) -> str | None:
        """``None`` when absent; raises ``SecretUnavailable`` on failure."""

    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


def _check_name(name: str) -> None:
    if name not in KNOWN_SECRETS:
        raise SecretUnavailable("unknown_credential")


class MacOSKeychainSecretStore:
    """Generic-password items in the macOS login Keychain through the
    ``keyring`` package's native macOS backend. Any other platform or backend
    (a file or plaintext keyring) is refused."""

    def __init__(
        self,
        *,
        service: str = KEYCHAIN_SERVICE,
        keyring_module: Any | None = None,
        platform: str | None = None,
    ) -> None:
        if (platform if platform is not None else sys.platform) != "darwin":
            raise SecretUnavailable("keychain_unsupported_platform")
        module: Any = keyring_module
        if module is None:
            try:
                module = importlib.import_module("keyring")
            except ImportError:
                raise SecretUnavailable("keychain_backend_missing") from None
        if type(module.get_keyring()).__module__ != "keyring.backends.macOS":
            raise SecretUnavailable("keychain_backend_not_macos")
        self._keyring = module
        self._service = service

    def get(self, name: str) -> str | None:
        _check_name(name)
        try:
            value = self._keyring.get_password(self._service, name)
        except Exception:
            raise SecretUnavailable("keychain_read_failed") from None
        return None if value is None else str(value)

    def set(self, name: str, value: str) -> None:
        _check_name(name)
        if not value.strip():
            raise SecretUnavailable("credential_blank")
        try:
            self._keyring.set_password(self._service, name, value)
        except Exception:
            raise SecretUnavailable("keychain_write_failed") from None

    def delete(self, name: str) -> None:
        _check_name(name)
        try:
            self._keyring.delete_password(self._service, name)
        except Exception as error:
            if type(error).__name__ == "PasswordDeleteError":
                return
            raise SecretUnavailable("keychain_delete_failed") from None

    def __repr__(self) -> str:
        return "MacOSKeychainSecretStore()"


@dataclass(frozen=True)
class CredentialReport:
    """Where each credential came from. Names and sources only: never a
    value, a length or a prefix."""

    sources: Mapping[str, CredentialSource]
    keychain: str  # ok | disabled | unavailable reason code

    @property
    def degraded(self) -> bool:
        return any(s is CredentialSource.UNAVAILABLE for s in self.sources.values())


class _UnavailableStore:
    """The Keychain is required but cannot be used: every lookup fails, so
    every credential not set in the environment is UNAVAILABLE."""

    def __init__(self, code: str) -> None:
        self.code = code

    def get(self, name: str) -> str | None:
        raise SecretUnavailable(self.code)

    def set(self, name: str, value: str) -> None:
        raise SecretUnavailable(self.code)

    def delete(self, name: str) -> None:
        raise SecretUnavailable(self.code)


def credential_store_for(settings: Settings) -> tuple[SecretStore | None, str]:
    """The store to use and its status: ``disabled`` | ``ok`` | a reason."""

    if not settings.keychain_enabled:
        return None, "disabled"
    try:
        return MacOSKeychainSecretStore(), "ok"
    except SecretUnavailable as error:
        return _UnavailableStore(error.code), error.code


def resolve_credentials(
    settings: Settings, store: SecretStore | None, status: str | None = None
) -> tuple[Settings, CredentialReport]:
    """Return settings with Keychain credentials filled in where no
    environment value exists. ``store`` is ``None`` when the Keychain is not
    used (development) or could not be opened."""

    production = settings.is_production
    sources: dict[str, CredentialSource] = {}
    updates: dict[str, SecretStr | None] = {}
    for name in KNOWN_SECRETS:
        current = getattr(settings, name)
        if current is not None and not production:
            sources[name] = CredentialSource.ENVIRONMENT
            continue
        if production:
            updates[name] = None  # an ambient environment value is never used
        if store is None:
            sources[name] = CredentialSource.NOT_CONFIGURED
            continue
        try:
            value = store.get(name)
        except SecretUnavailable:
            sources[name] = CredentialSource.UNAVAILABLE  # no fallback
            continue
        if value is None or not value.strip():
            sources[name] = CredentialSource.NOT_CONFIGURED
        elif name == "desktop_step_up_secret" and len(value) < MIN_STEP_UP_CHARS:
            # Keychain values bypass the settings validators: enforce the same
            # minimum here, failing closed (a weak secret is no secret).
            sources[name] = CredentialSource.NOT_CONFIGURED
        else:
            updates[name] = SecretStr(value)
            sources[name] = CredentialSource.KEYCHAIN
    resolved = settings.model_copy(update=updates) if updates else settings
    keychain = status or ("ok" if store is not None else "disabled")
    return resolved, CredentialReport(sources, keychain)


__all__ = [
    "KEYCHAIN_SERVICE",
    "KNOWN_SECRETS",
    "CredentialReport",
    "CredentialSource",
    "MacOSKeychainSecretStore",
    "SecretStore",
    "SecretUnavailable",
    "credential_store_for",
    "resolve_credentials",
]
