"""Deterministic opportunity deduplication. No model decides identity.

Two opportunities of the same type are the same one when any of these holds:

1. the same external reference id at the same registrable domain;
2. the same canonical URL;
3. the same normalized organization, normalized title and normalized location;
4. the same normalized organization and near-identical descriptions (shingle
   Jaccard >= ``DESCRIPTION_THRESHOLD``).

When they merge, all sources are kept as provenance and the highest-priority
source (official first) becomes canonical: its fields are the opportunity's
facts. A lower-trust source never overwrites an official one.
"""

from __future__ import annotations

import re

from sam.career.models import CareerOpportunity
from sam.career.opportunities import normalize_name, normalize_title
from sam.career.sources import priority

DESCRIPTION_THRESHOLD = 0.85
_SHINGLE = 5


def _shingles(text: str) -> frozenset[str]:
    words = re.sub(r"[^\w]+", " ", text.casefold()).split()
    if len(words) < _SHINGLE:
        return frozenset({" ".join(words)}) if words else frozenset()
    return frozenset(
        " ".join(words[i : i + _SHINGLE]) for i in range(len(words) - _SHINGLE + 1)
    )


def description_similarity(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def same_opportunity(a: CareerOpportunity, b: CareerOpportunity) -> bool:
    if a.type is not b.type:
        return False
    if (
        a.external_reference_id
        and a.external_reference_id == b.external_reference_id
        and a.canonical_domain == b.canonical_domain
    ):
        return True
    if a.canonical_url == b.canonical_url:
        return True
    same_org = normalize_name(a.organization) == normalize_name(b.organization)
    if not same_org:
        return False
    if normalize_title(a.title) == normalize_title(b.title) and normalize_name(
        a.location or ""
    ) == normalize_name(b.location or ""):
        return True
    return description_similarity(a.description, b.description) >= DESCRIPTION_THRESHOLD


def _rank(o: CareerOpportunity) -> int:
    return priority(o.canonical_source, o.type)


def merge(
    existing: CareerOpportunity, incoming: CareerOpportunity
) -> CareerOpportunity:
    """Merge ``incoming`` into ``existing`` (same identity). The id and first
    sighting are preserved; the best source's facts win; a changed canonical
    checksum bumps the version (so an approved draft sees the change)."""

    sources = {(s.kind, s.url): s for s in existing.sources}
    for source in incoming.sources:
        sources[(source.kind, source.url)] = source
    all_sources = tuple(
        sorted(sources.values(), key=lambda s: (priority(s.kind, existing.type), s.url))
    )
    better = _rank(incoming) < _rank(existing)
    same_canonical = incoming.canonical_url == existing.canonical_url
    base = incoming if (better or same_canonical) else existing
    changed = base.checksum != existing.checksum
    return base.model_copy(
        update={
            "opportunity_id": existing.opportunity_id,
            "first_seen": existing.first_seen,
            "last_seen": max(existing.last_seen, incoming.last_seen),
            "last_verified": (
                base.last_verified
                if (better or same_canonical)
                else existing.last_verified
            ),
            "sources": all_sources,
            "version": existing.version + 1 if changed else existing.version,
            "tracked": existing.tracked,
            "privacy_class": existing.privacy_class,
        }
    )


__all__ = [
    "DESCRIPTION_THRESHOLD",
    "description_similarity",
    "merge",
    "same_opportunity",
]
