"""Owner voice-template persistence.

The biometric template must survive a restart, so it persists — but ONLY in an
OS-backed secure store. The production implementation is the macOS Keychain.
There is deliberately no file/JSON/SQLite/.env/browser fallback: if the
Keychain cannot be used the store refuses to operate and identity fails
closed. ``InMemoryVoiceProfileStore`` exists for deterministic tests and is
never wired by production composition.
"""

from __future__ import annotations

import importlib
import sys
from threading import RLock
from typing import Any, Protocol

from sam.voice_identity.errors import ProfileStoreError
from sam.voice_identity.models import OwnerTemplate

KEYCHAIN_SERVICE = "app.sam.voice-identity"
KEYCHAIN_ACCOUNT = "owner-template"


class VoiceProfileStore(Protocol):
    def load(self) -> OwnerTemplate | None:
        """The stored template, ``None`` if none exists. A read *failure*
        raises ProfileStoreError — it is never reported as 'not enrolled'."""

    def save(self, template: OwnerTemplate) -> None:
        """Atomically replace any previous template."""

    def delete(self) -> None:
        """Remove the template; raises if it could not be removed."""


class InMemoryVoiceProfileStore:
    """Tests only. Volatile, never persisted."""

    def __init__(self, template: OwnerTemplate | None = None) -> None:
        self._template = template
        self._lock = RLock()
        self.fail_load = False
        self.fail_save = False
        self.fail_delete = False

    def load(self) -> OwnerTemplate | None:
        if self.fail_load:
            raise ProfileStoreError("secure store unavailable")
        with self._lock:
            return self._template

    def save(self, template: OwnerTemplate) -> None:
        if self.fail_save:
            raise ProfileStoreError("secure store unavailable")
        with self._lock:
            self._template = template

    def delete(self) -> None:
        if self.fail_delete:
            raise ProfileStoreError("secure store unavailable")
        with self._lock:
            self._template = None

    def __repr__(self) -> str:
        return "InMemoryVoiceProfileStore(<test only>)"


class MacOSKeychainVoiceProfileStore:
    """Stores the template as one generic-password item in the macOS login
    Keychain (encrypted at rest by the OS, access-controlled to the user).

    Uses the ``keyring`` package's native macOS backend. It refuses to run on
    any other platform or with any other keyring backend (a file-backed or
    plaintext backend would be an insecure fallback).
    """

    def __init__(
        self,
        *,
        service: str = KEYCHAIN_SERVICE,
        account: str = KEYCHAIN_ACCOUNT,
        keyring_module: Any | None = None,
        platform: str | None = None,
    ) -> None:
        if (platform if platform is not None else sys.platform) != "darwin":
            raise ProfileStoreError("the macOS Keychain is required and unavailable")
        module: Any = keyring_module
        if module is None:
            try:  # optional dependency (the ``voice-local`` group)
                module = importlib.import_module("keyring")
            except ImportError:
                raise ProfileStoreError(
                    "the Keychain backend is not installed"
                ) from None
        backend_module = type(module.get_keyring()).__module__
        if backend_module != "keyring.backends.macOS":
            raise ProfileStoreError("the macOS Keychain backend is required")
        self._keyring = module
        self._service = service
        self._account = account
        self._lock = RLock()

    def load(self) -> OwnerTemplate | None:
        with self._lock:
            try:
                raw = self._keyring.get_password(self._service, self._account)
            except Exception:
                raise ProfileStoreError("secure store read failed") from None
        return None if raw is None else OwnerTemplate.from_json(raw)

    def save(self, template: OwnerTemplate) -> None:
        with self._lock:
            try:
                self._keyring.set_password(
                    self._service, self._account, template.to_json()
                )
            except Exception:
                raise ProfileStoreError("secure store write failed") from None

    def delete(self) -> None:
        with self._lock:
            try:
                self._keyring.delete_password(self._service, self._account)
            except Exception as error:
                if type(error).__name__ == "PasswordDeleteError":
                    return  # nothing stored: deletion is idempotent
                raise ProfileStoreError("secure store delete failed") from None

    def __repr__(self) -> str:
        return "MacOSKeychainVoiceProfileStore()"


__all__ = [
    "KEYCHAIN_ACCOUNT",
    "KEYCHAIN_SERVICE",
    "InMemoryVoiceProfileStore",
    "MacOSKeychainVoiceProfileStore",
    "VoiceProfileStore",
]
