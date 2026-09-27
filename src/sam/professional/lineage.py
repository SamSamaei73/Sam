"""Source families: which sources are genuinely independent of each other.

Corroboration needs two INDEPENDENT sources. Two files are not independent just
because they are two files or have two source types: a second version of the CV,
a renamed copy of it, or the same text saved as another file name is the SAME
source family and can never corroborate the first.

Families are decided by deterministic code only (never a model, never an
embedding), from three facts recorded on each ``SourceRecord``:

* ``independence_key``: the owner's master CV and LinkedIn export are each ONE
  logical document however many versions are uploaded, so every source of those
  types shares a key; any other source's key is its own logical source id;
* ``content_fingerprint``: sha256 of the normalized text, so identical text
  under another name, type or encoding is the same family;
* ``line_fingerprints``: short hashes of the normalized content lines, so a
  lightly edited copy (most lines shared) is the same family.

Families are recomputed from the CURRENT sources on every read (a union of the
three relations), so the result never depends on ingestion order and removing a
source recomputes every family it touched.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence

from sam.professional.models import (
    MAX_LINE_FINGERPRINTS,
    SourceRecord,
    SourceType,
    stable_id,
)

# One logical document per owner, whatever the file name or version.
SINGLETON_TYPES = frozenset({SourceType.MASTER_CV, SourceType.LINKEDIN_EXPORT})

# A content line shorter than this (a heading, "Python") says nothing about
# whether two documents are copies of each other.
MIN_LINE_CHARS = 16
# Two sources sharing at least this fraction of the smaller one's content lines
# (and at least MIN_SHARED_LINES of them) are copies: one family.
OVERLAP_THRESHOLD = 0.6
MIN_SHARED_LINES = 3


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w]+", " ", folded).strip()


def independence_key(source_type: SourceType, source_id: str) -> str:
    if source_type in SINGLETON_TYPES:
        return f"type:{source_type.value}"
    return f"source:{source_id}"


def fingerprints(texts: Sequence[str]) -> tuple[str, frozenset[str]]:
    """``(content_fingerprint, line_fingerprints)`` of a document's text."""

    lines: list[str] = []
    for text in texts:
        for raw in text.splitlines():
            line = _normalize(raw)
            if line:
                lines.append(line)
    content = hashlib.sha256(" ".join(lines).encode("utf-8")).hexdigest()
    shared = sorted(
        {
            hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]
            for line in lines
            if len(line) >= MIN_LINE_CHARS
        }
    )
    return content, frozenset(shared[:MAX_LINE_FINGERPRINTS])


def _copies(a: SourceRecord, b: SourceRecord) -> bool:
    if a.independence_key == b.independence_key:
        return True
    if a.content_fingerprint == b.content_fingerprint:
        return True
    smaller = min(len(a.line_fingerprints), len(b.line_fingerprints))
    if smaller < MIN_SHARED_LINES:
        return False
    shared = len(a.line_fingerprints & b.line_fingerprints)
    return shared >= MIN_SHARED_LINES and shared / smaller >= OVERLAP_THRESHOLD


def source_families(sources: Iterable[SourceRecord]) -> dict[str, str]:
    """source_id -> source_family_id for the given sources. Deterministic and
    independent of order: the family id is derived from the smallest
    independence key in the family."""

    ordered = sorted(sources, key=lambda s: s.source_id)
    parent = {s.source_id: s.source_id for s in ordered}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if _copies(a, b):
                root_a, root_b = find(a.source_id), find(b.source_id)
                if root_a != root_b:
                    parent[max(root_a, root_b)] = min(root_a, root_b)

    members: dict[str, list[SourceRecord]] = {}
    for source in ordered:
        members.setdefault(find(source.source_id), []).append(source)
    family: dict[str, str] = {}
    for group in members.values():
        family_id = stable_id("f", min(s.independence_key for s in group))
        for source in group:
            family[source.source_id] = family_id
    return family


def family_count(source_ids: Iterable[str], families: Mapping[str, str]) -> int:
    """How many independent families the given sources span. A source with no
    known family (it no longer exists) counts for nothing."""

    return len({families[s] for s in source_ids if s in families})


__all__ = [
    "MIN_LINE_CHARS",
    "MIN_SHARED_LINES",
    "OVERLAP_THRESHOLD",
    "SINGLETON_TYPES",
    "family_count",
    "fingerprints",
    "independence_key",
    "source_families",
]
