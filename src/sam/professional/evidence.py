"""Evidence strength, conflicts, and the skill evidence graph.

Everything here is a PURE function of a claim's ``EvidenceRef`` records, the
current source families and the owner's review: strength and acceptance are
never stored authority, never assigned by a model, and are recomputed on every
read. Conflicts are derived from evidence and are never merged away.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from sam.professional.lineage import family_count
from sam.professional.models import (
    DATE_ATTRIBUTES,
    DOCUMENTARY_NATURES,
    ClaimCategory,
    ClaimReviewState,
    Conflict,
    ConflictKind,
    ConflictOption,
    EvidenceNature,
    EvidenceRef,
    EvidenceStrength,
    ProfessionalClaim,
    Resolution,
    SourceRecord,
    SourceType,
    stable_id,
)
from sam.professional.timeline import normalize_date, reconcile_dates

# Evidence that states attribute values (dates, classification ...). A model
# candidate and an owner attestation assert none.
_OBSERVING = frozenset(
    {EvidenceNature.EXPLICIT_SOURCE, EvidenceNature.INFERRED_RELATIONSHIP}
)


def compute_strength(
    evidence: Sequence[EvidenceRef], families: Mapping[str, str]
) -> EvidenceStrength:
    """DOCUMENTARY support for one claim.

    Only ``EXPLICIT_SOURCE`` evidence counts, and it is counted in independent
    source FAMILIES (``sam.professional.lineage``), never in files or source
    types: two versions of one CV are one family, so they give SINGLE_SOURCE.
    An owner attestation, an inferred relationship and a model candidate never
    add to it, so the owner's review can neither create nor erase it."""

    documentary = [e.source_id for e in evidence if e.nature in DOCUMENTARY_NATURES]
    count = family_count(documentary, families)
    if count >= 2:
        return EvidenceStrength.CORROBORATED
    if count == 1:
        return EvidenceStrength.SINGLE_SOURCE
    return EvidenceStrength.NONE


def is_accepted(
    evidence: Sequence[EvidenceRef],
    strength: EvidenceStrength,
    review: ClaimReviewState,
) -> bool:
    """Whether a claim may be presented as a fact about the owner.

    Accepted iff the owner has not rejected it AND it has trusted evidence:
    documentary support, or the owner's confirmation backed by an
    ``OWNER_ATTESTATION`` record (which names the evidence the owner reviewed).
    A review with no attestation evidence accepts nothing: no accepted claim
    ever rests on zero trusted evidence."""

    if review is ClaimReviewState.OWNER_REJECTED:
        return False
    if strength is not EvidenceStrength.NONE:
        return True
    return review is ClaimReviewState.OWNER_CONFIRMED and any(
        e.nature is EvidenceNature.OWNER_ATTESTATION and e.basis_evidence_id
        for e in evidence
    )


def has_nature(evidence: Iterable[EvidenceRef], nature: EvidenceNature) -> bool:
    return any(e.nature is nature for e in evidence)


def sources_of(evidence: Iterable[EvidenceRef]) -> tuple[str, ...]:
    return tuple(sorted({e.source_id for e in evidence}))


# ------------------------------------------------------------- normalizing

_CLASSIFICATIONS = {
    "first": "first",
    "first class": "first",
    "first class honours": "first",
    "first class honors": "first",
    "1st": "first",
    "distinction": "distinction",
    "merit": "merit",
    "pass": "pass",
    "2:1": "2:1",
    "upper second": "2:1",
    "upper second class": "2:1",
    "2:2": "2:2",
    "lower second": "2:2",
    "lower second class": "2:2",
}


def norm_text(value: str) -> str:
    return re.sub(r"[^a-z0-9:+#/.]+", " ", value.lower()).strip()


# Only these attributes can CONFLICT: they are structured facts where two
# sources disagreeing is meaningful (dates, classification, institution, DOI,
# author position). Descriptive text (authors, venue, abstract, modules ...) is
# presentation, not a fact two sources can contradict, so it never raises a
# conflict; the value shown is chosen by a fixed, documented source authority.
CONFLICT_ATTRIBUTES = frozenset(
    {
        "start",
        "end",
        "date",
        "year",
        "classification",
        "institution",
        "doi",
        "owner_author_position",
    }
)

SOURCE_AUTHORITY: dict[SourceType, int] = {
    SourceType.PUBLICATION: 0,
    SourceType.TRANSCRIPT: 1,
    SourceType.MASTER_CV: 2,
    SourceType.LINKEDIN_EXPORT: 3,
    SourceType.PROJECT_DOCUMENTATION: 4,
    SourceType.OWNER_DOCUMENT: 5,
    SourceType.GITHUB: 6,
}


def normalize_value(attribute: str, value: str) -> str:
    """The comparison form of an attribute value (never shown to the owner)."""

    if attribute in DATE_ATTRIBUTES:
        return normalize_date(value) or norm_text(value)
    if attribute == "institution":
        return re.sub(r"^the ", "", norm_text(value))
    if attribute == "classification":
        text = norm_text(value)
        text = re.sub(r"\b(honou?rs|degree|grade)\b", "", text).strip()
        return _CLASSIFICATIONS.get(re.sub(r"\s+", " ", text), text)
    return norm_text(value)


# ---------------------------------------------------------------- conflicts


@dataclass(frozen=True)
class AttributeValue:
    value: str | None
    conflicted: bool
    resolved: bool = False


def _trusted_observations(
    evidence: Sequence[EvidenceRef], attribute: str
) -> dict[str, list[EvidenceRef]]:
    """normalized value -> the evidence asserting it (candidates excluded)."""

    grouped: dict[str, list[EvidenceRef]] = defaultdict(list)
    for ref in evidence:
        if ref.nature not in _OBSERVING:
            continue
        raw = ref.observed.get(attribute)
        if raw:
            grouped[normalize_value(attribute, raw)].append(ref)
    return grouped


def _authoritative(grouped: dict[str, list[EvidenceRef]]) -> str | None:
    ranked = sorted(
        (
            (SOURCE_AUTHORITY.get(ref.source_type, 99), ref.evidence_id, value)
            for value, refs in grouped.items()
            for ref in refs
        )
    )
    return ranked[0][2] if ranked else None


def attribute_conflict(
    claim: ProfessionalClaim,
    evidence: Sequence[EvidenceRef],
    attribute: str,
    resolutions: Sequence[Resolution],
) -> tuple[AttributeValue, Conflict | None]:
    """The effective value of one attribute, plus the conflict (if any). If the
    evidence disagrees and the owner has not resolved it, ``value`` is ``None``:
    no value is chosen for the owner."""

    grouped = _trusted_observations(evidence, attribute)
    if not grouped:
        return AttributeValue(None, False), None
    if attribute in DATE_ATTRIBUTES:
        value, conflicted = reconcile_dates(grouped)
    elif attribute in CONFLICT_ATTRIBUTES:
        conflicted = len(grouped) > 1
        value = None if conflicted else next(iter(grouped))
    else:
        # Descriptive text: never a conflict. Show the most authoritative
        # source's value (fixed source order, then evidence id).
        value, conflicted = _authoritative(grouped), False
    if not conflicted:
        return AttributeValue(value, False), None

    options = tuple(
        ConflictOption(
            option_id=stable_id("o", claim.claim_id, attribute, option_value),
            value=option_value,
            evidence_ids=tuple(sorted(e.evidence_id for e in refs)),
            source_ids=sources_of(refs),
        )
        for option_value, refs in sorted(grouped.items())
    )
    chosen = next(
        (
            r.value
            for r in resolutions
            if r.claim_id == claim.claim_id
            and r.attribute == attribute
            and r.value in grouped
        ),
        None,
    )
    conflict = Conflict(
        conflict_id=stable_id(
            "x", ConflictKind.ATTRIBUTE.value, claim.claim_id, attribute
        ),
        kind=ConflictKind.ATTRIBUTE,
        attribute=attribute,
        claim_ids=(claim.claim_id,),
        options=options,
        resolved=chosen is not None,
        resolved_value=chosen,
    )
    if chosen is not None:
        return AttributeValue(chosen, False, resolved=True), conflict
    return AttributeValue(None, True), conflict


def claim_attributes(evidence: Sequence[EvidenceRef]) -> set[str]:
    return {
        name for ref in evidence if ref.nature in _OBSERVING for name in ref.observed
    }


def detect_conflicts(
    claims: Sequence[ProfessionalClaim],
    evidence_by_claim: dict[str, list[EvidenceRef]],
    resolutions: Sequence[Resolution],
    intervals: dict[str, tuple[str, str]],
) -> tuple[Conflict, ...]:
    """Every attribute conflict, plus employment role overlaps.

    ``intervals`` maps an employment claim id to its RESOLVED ``(start, end)``."""

    found: list[Conflict] = []
    for claim in claims:
        refs = evidence_by_claim.get(claim.claim_id, [])
        for attribute in sorted(claim_attributes(refs)):
            _, conflict = attribute_conflict(claim, refs, attribute, resolutions)
            if conflict is not None:
                found.append(conflict)
    found.extend(_role_overlaps(claims, evidence_by_claim, intervals))
    return tuple(sorted(found, key=lambda c: c.conflict_id))


def _role_overlaps(
    claims: Sequence[ProfessionalClaim],
    evidence_by_claim: dict[str, list[EvidenceRef]],
    intervals: dict[str, tuple[str, str]],
) -> list[Conflict]:
    jobs = [
        c
        for c in claims
        if c.category is ClaimCategory.EMPLOYMENT and c.claim_id in intervals
    ]
    out: list[Conflict] = []
    for i, a in enumerate(jobs):
        for b in jobs[i + 1 :]:
            if norm_text(a.attributes.get("employer", "")) != norm_text(
                b.attributes.get("employer", "")
            ):
                continue
            if norm_text(a.attributes.get("title", "")) == norm_text(
                b.attributes.get("title", "")
            ):
                continue
            src_a = set(sources_of(evidence_by_claim.get(a.claim_id, [])))
            src_b = set(sources_of(evidence_by_claim.get(b.claim_id, [])))
            if src_a & src_b or not _overlap(
                intervals[a.claim_id], intervals[b.claim_id]
            ):
                continue
            first, second = sorted((a, b), key=lambda c: c.claim_id)
            out.append(
                Conflict(
                    conflict_id=stable_id(
                        "x",
                        ConflictKind.ROLE_OVERLAP.value,
                        first.claim_id,
                        second.claim_id,
                    ),
                    kind=ConflictKind.ROLE_OVERLAP,
                    attribute="title",
                    claim_ids=(first.claim_id, second.claim_id),
                    options=tuple(
                        ConflictOption(
                            option_id=stable_id("o", c.claim_id, "title"),
                            value=c.attributes.get("title", ""),
                            claim_id=c.claim_id,
                            evidence_ids=tuple(
                                sorted(
                                    e.evidence_id
                                    for e in evidence_by_claim.get(c.claim_id, [])
                                )
                            ),
                            source_ids=sources_of(
                                evidence_by_claim.get(c.claim_id, [])
                            ),
                        )
                        for c in (first, second)
                    ),
                )
            )
    return out


def _overlap(a: tuple[str, str], b: tuple[str, str]) -> bool:
    def span(pair: tuple[str, str]) -> tuple[str, str]:
        start = pair[0] if len(pair[0]) == 7 else f"{pair[0]}-01"
        end = "9999-12" if pair[1] == "present" else pair[1]
        end = end if len(end) == 7 else f"{end}-12"
        return start, end

    (a0, a1), (b0, b1) = span(a), span(b)
    return a0 <= b1 and b0 <= a1


# --------------------------------------------------------------- the graph


@dataclass(frozen=True)
class GraphEdge:
    """Skill -> EvidenceRef -> (project / employment / publication / education)
    -> Source. ``context`` is ``None`` when the evidence is not inside one."""

    evidence: EvidenceRef
    context: ProfessionalClaim | None
    source: SourceRecord | None


@dataclass
class SkillEvidenceGraph:
    """An in-memory, deterministic relationship model. No graph database: a
    future Graph RAG can index these same nodes and edges."""

    claims: dict[str, ProfessionalClaim] = field(default_factory=dict)
    sources: dict[str, SourceRecord] = field(default_factory=dict)
    evidence: dict[str, list[EvidenceRef]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        claims: Iterable[ProfessionalClaim],
        evidence: Iterable[EvidenceRef],
        sources: Iterable[SourceRecord],
    ) -> SkillEvidenceGraph:
        graph = cls()
        graph.claims = {c.claim_id: c for c in claims}
        graph.sources = {s.source_id: s for s in sources}
        for ref in sorted(evidence, key=lambda e: e.evidence_id):
            graph.evidence.setdefault(ref.claim_id, []).append(ref)
        return graph

    def edges_for(self, claim_id: str) -> tuple[GraphEdge, ...]:
        return tuple(
            GraphEdge(
                evidence=ref,
                context=self.claims.get(ref.context_claim_id or ""),
                source=self.sources.get(ref.source_id),
            )
            for ref in self.evidence.get(claim_id, [])
        )

    def skills_in_context(self, context_claim_id: str) -> tuple[str, ...]:
        """Skill/technology claim ids that have evidence inside a context."""

        return tuple(
            sorted(
                {
                    claim_id
                    for claim_id, refs in self.evidence.items()
                    for ref in refs
                    if ref.context_claim_id == context_claim_id
                    and self.claims.get(claim_id) is not None
                    and self.claims[claim_id].category
                    in (ClaimCategory.SKILL, ClaimCategory.TECHNOLOGY)
                }
            )
        )

    def contexts_for(self, claim_id: str) -> tuple[ProfessionalClaim, ...]:
        return tuple(
            sorted(
                {
                    edge.context.claim_id: edge.context
                    for edge in self.edges_for(claim_id)
                    if edge.context is not None
                }.values(),
                key=lambda c: c.claim_id,
            )
        )


__all__ = [
    "AttributeValue",
    "GraphEdge",
    "SkillEvidenceGraph",
    "attribute_conflict",
    "claim_attributes",
    "compute_strength",
    "detect_conflicts",
    "has_nature",
    "is_accepted",
    "norm_text",
    "normalize_value",
    "sources_of",
]
