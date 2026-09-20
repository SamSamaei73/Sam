"""Credential boundary for the speech-synthesis provider.

The Fish API key must exist only at the provider execution boundary: it is
resolved from a trusted ``TTSCredentialReference`` *inside* the provider's
``synthesize`` call, after the gateway has already validated the text and the
PermissionEngine has said ALLOW. It never appears in a request model, the
gateway, AgentCore, audit, Memory, Knowledge, a ``repr``/``str``, or an
error message.

``TTSCredential`` is opaque: no ``__dict__``, redacted ``repr``/``str``/
``format``, not picklable or copyable, immutable, identity equality. Only the
explicit ``reveal()`` (called by a provider at its transport boundary) yields
the value.

Where the key comes from is a *bootstrap* decision, made outside the
provider: ``credentials_from_settings`` reads the ``SecretStr`` the trusted
``Settings`` object already loaded. No provider code reads ``os.environ``.
"""

from __future__ import annotations

from threading import RLock
from typing import NoReturn, Protocol

from pydantic import BaseModel, ConfigDict, Field

from sam.core.config import Settings
from sam.tts.errors import TTSCredentialError
from sam.tts.models import (
    _CREDENTIAL_ID_RE,
    MAX_TTS_CREDENTIAL_LENGTH,
    MAX_TTS_PROVIDER_ID_LENGTH,
)

_REDACTED = "TTSCredential(**redacted**)"


class TTSCredentialReference(BaseModel):
    """A *reference* to a credential — never the credential itself. Held in
    trusted configuration; no request or LLM output can supply one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(min_length=1, max_length=MAX_TTS_PROVIDER_ID_LENGTH)
    credential_id: str = Field(
        min_length=1, max_length=64, pattern=_CREDENTIAL_ID_RE.pattern
    )


class TTSCredential:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > MAX_TTS_CREDENTIAL_LENGTH
            or any(ord(ch) < 33 or ord(ch) == 127 for ch in value)
        ):
            raise TTSCredentialError("credential is invalid")
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        return str(object.__getattribute__(self, "_value"))

    def __repr__(self) -> str:
        return _REDACTED

    def __str__(self) -> str:
        return _REDACTED

    def __format__(self, format_spec: str) -> str:
        return _REDACTED

    def __reduce__(self) -> NoReturn:
        raise TypeError("credentials cannot be serialized or copied")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("credentials are immutable")

    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)


class TTSCredentialProvider(Protocol):
    def resolve(self, reference: TTSCredentialReference) -> TTSCredential:
        """Raise ``TTSCredentialError`` if unknown. Never return a credential
        issued for a different provider."""


class FakeTTSCredentialProvider:
    """In-memory provider with synthetic values, for tests."""

    def __init__(self) -> None:
        self._credentials: dict[tuple[str, str], TTSCredential] = {}
        self._lock = RLock()
        self.resolve_count = 0

    def add(self, reference: TTSCredentialReference, secret: str) -> None:
        with self._lock:
            self._credentials[(reference.provider_id, reference.credential_id)] = (
                TTSCredential(secret)
            )

    def resolve(self, reference: TTSCredentialReference) -> TTSCredential:
        with self._lock:
            self.resolve_count += 1
            found = self._credentials.get(
                (reference.provider_id, reference.credential_id)
            )
        if found is None:
            raise TTSCredentialError("credential is not available") from None
        return found

    def __repr__(self) -> str:
        return f"FakeTTSCredentialProvider(entries={len(self._credentials)})"


def credentials_from_settings(
    settings: Settings, reference: TTSCredentialReference
) -> FakeTTSCredentialProvider:
    """Bootstrap helper: wrap the key the trusted ``Settings`` already loaded.

    This is the *only* place a Fish key enters the TTS layer, and it happens at
    the application/bootstrap boundary — not inside any provider. Raises
    ``TTSCredentialError`` if no key is configured.
    """

    key = settings.fish_audio_api_key
    if key is None:
        raise TTSCredentialError("no speech-synthesis credential is configured")
    provider = FakeTTSCredentialProvider()
    provider.add(reference, key.get_secret_value())
    return provider


__all__ = [
    "FakeTTSCredentialProvider",
    "TTSCredential",
    "TTSCredentialProvider",
    "TTSCredentialReference",
    "credentials_from_settings",
]
