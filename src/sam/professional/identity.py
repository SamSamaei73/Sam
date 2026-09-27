"""The owner's professional identity: who "the owner" is in an author list.

Trusted LOCAL configuration only: a canonical name and the aliases the owner
explicitly approved. It is built once at startup from settings and is frozen:
no document, no model output, no Guest and no HTTP route can add an alias or
change it.

Matching is exact and deterministic:

* normalization is Unicode NFKD with accents removed, case folded, punctuation
  turned into spaces; nothing else;
* an author matches only if the WHOLE normalized author equals the canonical
  name or an approved alias: no surname-only match, no guessed initials, no
  fuzzy or embedding similarity, no model;
* every name and alias must have at least two words, so a lone surname can
  never be configured;
* no match, or more than one matching author in one list, is UNKNOWN: Sam
  records no position rather than guessing which one is the owner.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

MAX_ALIASES = 10
MAX_NAME_LENGTH = 100


class OwnerMatch(StrEnum):
    MATCHED = "matched"
    UNKNOWN = "unknown"


def normalize_person_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", " ", stripped.casefold()).replace("_", " ").strip()


def _valid(name: str) -> str:
    if not name or len(name) > MAX_NAME_LENGTH:
        raise ValueError("owner name must be 1-100 characters")
    normalized = normalize_person_name(name)
    words = normalized.split()
    if len(words) < 2 or any(w.isdigit() for w in words):
        raise ValueError("owner name and aliases need at least two words")
    return normalized


@dataclass(frozen=True)
class AuthorPosition:
    status: OwnerMatch
    position: int | None = None


@dataclass(frozen=True)
class OwnerProfessionalIdentity:
    canonical_name: str
    approved_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.approved_aliases) > MAX_ALIASES:
            raise ValueError("too many approved aliases")
        _valid(self.canonical_name)
        for alias in self.approved_aliases:
            _valid(alias)

    @classmethod
    def from_config(
        cls, canonical_name: str | None, aliases: str | Iterable[str] | None = None
    ) -> OwnerProfessionalIdentity | None:
        """Build from trusted settings. ``aliases`` may be a ``;``-separated
        string. No canonical name means no identity (every position UNKNOWN)."""

        if not canonical_name or not canonical_name.strip():
            return None
        if isinstance(aliases, str):
            items = [a.strip() for a in aliases.split(";")]
        else:
            items = [a.strip() for a in (aliases or ())]
        return cls(canonical_name.strip(), tuple(a for a in items if a))

    @property
    def names(self) -> frozenset[str]:
        return frozenset(
            normalize_person_name(n)
            for n in (self.canonical_name, *self.approved_aliases)
        )

    def is_owner(self, author: str) -> bool:
        return normalize_person_name(author) in self.names

    def author_position(self, authors: Sequence[str]) -> AuthorPosition:
        hits = [i for i, a in enumerate(authors, start=1) if self.is_owner(a)]
        if len(hits) == 1:
            return AuthorPosition(OwnerMatch.MATCHED, hits[0])
        return AuthorPosition(OwnerMatch.UNKNOWN)

    def mentioned_in(self, text: str) -> bool:
        """The canonical name or an approved alias appears in ``text`` as whole
        words (never a substring of a longer name)."""

        padded = f" {normalize_person_name(text)} "
        return any(f" {name} " in padded for name in self.names)


__all__ = [
    "MAX_ALIASES",
    "AuthorPosition",
    "OwnerMatch",
    "OwnerProfessionalIdentity",
    "normalize_person_name",
]
