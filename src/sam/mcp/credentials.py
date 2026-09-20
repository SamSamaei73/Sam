"""Credential boundary.

A credential is looked up from a trusted ``CredentialReference`` held in the
registry — never from a string the LLM or a request supplies — and only at
the execution boundary, after the PermissionEngine has said ALLOW. The
resolved ``Credential`` is handed to exactly one transport (the one
registered for the tool's server) and is never stored on a request, result,
audit event, error, or log line.

``Credential`` deliberately makes accidental disclosure hard: it has no
``__dict__``, its ``repr``/``str``/``format`` are redacted, it cannot be
pickled or copied, and it compares by identity. The only way to read the
value is the explicit ``reveal()`` call, which only a transport should make.

Phase 8 ships only ``FakeCredentialProvider`` with synthetic values. There
is no OAuth flow, no secret manager, and no ambient-environment lookup —
the provider never reads ``os.environ``.
"""

from __future__ import annotations

from threading import RLock
from typing import NoReturn, Protocol

from sam.mcp.errors import MCPCredentialError
from sam.mcp.models import MAX_CREDENTIAL_LENGTH, CredentialReference

_REDACTED = "Credential(**redacted**)"


class Credential:
    """An opaque secret. See the module docstring for the disclosure
    hardening this class provides."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > MAX_CREDENTIAL_LENGTH
        ):
            raise MCPCredentialError("credential is invalid")
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """The raw secret. Call only at a transport boundary."""

        return str(object.__getattribute__(self, "_value"))

    def appears_in(self, text: str) -> bool:
        """True if the secret occurs verbatim in ``text`` — used to stop a
        misbehaving server from echoing a credential back to the caller."""

        return self.reveal() in text

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


class CredentialProvider(Protocol):
    """Resolve a trusted reference to a credential for one server/tool."""

    def resolve(
        self, reference: CredentialReference, *, server_id: str, tool_name: str
    ) -> Credential:
        """Raise ``MCPCredentialError`` if the reference is unknown, or if it
        is not bound to ``server_id`` — a credential is never released to a
        different server than the one it was issued for."""


class FakeCredentialProvider:
    """In-memory provider with synthetic credentials, for tests.

    Credentials are keyed by ``(server_id, credential_id)`` so one server's
    credential can never be resolved for another, even if a reference to it
    is somehow presented.
    """

    def __init__(self) -> None:
        self._credentials: dict[tuple[str, str], Credential] = {}
        self._lock = RLock()
        self.resolve_count = 0

    def add(self, reference: CredentialReference, secret: str) -> None:
        with self._lock:
            self._credentials[(reference.server_id, reference.credential_id)] = (
                Credential(secret)
            )

    def resolve(
        self, reference: CredentialReference, *, server_id: str, tool_name: str
    ) -> Credential:
        with self._lock:
            self.resolve_count += 1
            if reference.server_id != server_id:
                raise MCPCredentialError("credential is not available") from None
            found = self._credentials.get((server_id, reference.credential_id))
        if found is None:
            raise MCPCredentialError("credential is not available") from None
        return found

    def __repr__(self) -> str:
        return f"FakeCredentialProvider(entries={len(self._credentials)})"


__all__ = ["Credential", "CredentialProvider", "FakeCredentialProvider"]
