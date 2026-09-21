"""Provider-bound credentials.

A credential is opaque: redacted ``repr``/``str``/``format``, not picklable or
copyable, immutable, identity equality. It exists only inside the adapter that
owns it and is revealed only at that adapter's transport boundary. There is no
shared credential bag: an adapter never sees another provider's credential.
"""

from __future__ import annotations

from typing import NoReturn

_REDACTED = "ProviderCredential(**redacted**)"
MAX_CREDENTIAL_LENGTH = 512


class ProviderCredential:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > MAX_CREDENTIAL_LENGTH
            or any(ord(c) < 33 or ord(c) == 127 for c in value)
        ):
            raise ValueError("credential is invalid")
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


__all__ = ["ProviderCredential"]
