"""A read-only snapshot of the owner's professional evidence.

``Profile`` is built from the repository's claims, evidence, sources, owner
reviews and resolutions. Every derived value (source families, a claim's
documentary strength and acceptance, its effective sensitivity, its conflicts,
the resolved timeline, experience durations) is COMPUTED HERE, deterministically,
on every snapshot. Nothing is taken from a cache or from a model, so a stale or
tampered value cannot change what Sam reports, and a removed source or refreshed
version can leave nothing behind in a later view.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from functools import cached_property

from sam.models.models import PrivacyClass
from sam.professional.evidence import (
    SkillEvidenceGraph,
    attribute_conflict,
    claim_attributes,
    compute_strength,
    detect_conflicts,
    has_nature,
    is_accepted,
    normalize_value,
)
from sam.professional.lineage import source_families
from sam.professional.models import (
    DOCUMENTARY_NATURES,
    ClaimCategory,
    ClaimReviewState,
    Conflict,
    EvidenceNature,
    EvidenceRef,
    EvidenceStrength,
    ProfessionalClaim,
    Resolution,
    SourceLocation,
    SourceRecord,
    SourceType,
)
from sam.professional.timeline import (
    Duration,
    Interval,
    IntervalStatus,
    find_gaps,
    union_months,
)


@dataclass(frozen=True)
class EvidenceView:
    evidence_id: str
    source_id: str
    source_label: str
    source_type: SourceType
    source_family_id: str
    nature: EvidenceNature
    location: SourceLocation
    reference: str
    context_claim_id: str | None
    context_statement: str | None
    basis_evidence_id: str | None


@dataclass(frozen=True)
class ClaimView:
    """One claim as Sam may present it. ``strength`` (documentary support),
    ``review`` (the owner's decision) and ``accepted`` are independent facts:
    an owner-confirmed claim with no document behind it is ACCEPTED with
    strength NONE, never SINGLE_SOURCE or CORROBORATED."""

    claim_id: str
    category: ClaimCategory
    statement: str
    strength: EvidenceStrength
    review: ClaimReviewState
    accepted: bool
    inferred: bool
    candidate: bool
    sensitivity: PrivacyClass
    attributes: Mapping[str, str]
    conflicted_attributes: tuple[str, ...]
    evidence: tuple[EvidenceView, ...]
    source_count: int
    source_family_count: int
    canonical: bool

    @property
    def owner_attested_only(self) -> bool:
        return self.accepted and self.strength is EvidenceStrength.NONE


@dataclass(frozen=True)
class TimelineEntry:
    claim_id: str
    employer: str
    title: str
    start: str | None
    end: str | None
    status: IntervalStatus
    months: int
    approximate: bool


@dataclass(frozen=True)
class ExperienceSummary:
    entries: tuple[TimelineEntry, ...]
    total: Duration
    conservative_total: Duration
    excluded_conflicted: tuple[str, ...]
    excluded_incomplete: tuple[str, ...]
    gaps: tuple[tuple[str, str, int], ...]


class Profile:
    def __init__(
        self,
        *,
        claims: Iterable[ProfessionalClaim],
        evidence: Iterable[EvidenceRef],
        sources: Iterable[SourceRecord],
        resolutions: Sequence[Resolution],
        today: date,
        reviews: Mapping[str, ClaimReviewState] | None = None,
    ) -> None:
        self.claims: dict[str, ProfessionalClaim] = {c.claim_id: c for c in claims}
        self.sources: dict[str, SourceRecord] = {s.source_id: s for s in sources}
        self.families: dict[str, str] = source_families(self.sources.values())
        self.reviews: dict[str, ClaimReviewState] = dict(reviews or {})
        self.resolutions = tuple(resolutions)
        self.today = today
        by_claim: dict[str, list[EvidenceRef]] = {}
        for ref in sorted(evidence, key=lambda e: e.evidence_id):
            # Evidence must point at a claim and a source that still exist.
            if ref.claim_id in self.claims and ref.source_id in self.sources:
                by_claim.setdefault(ref.claim_id, []).append(ref)
        self.evidence: dict[str, list[EvidenceRef]] = by_claim
        self.graph = SkillEvidenceGraph.build(
            self.claims.values(),
            (e for refs in by_claim.values() for e in refs),
            self.sources.values(),
        )

    # ----------------------------------------------------------- claim views

    def strength_of(self, claim_id: str) -> EvidenceStrength:
        return compute_strength(self.evidence.get(claim_id, []), self.families)

    def review_of(self, claim_id: str) -> ClaimReviewState:
        return self.reviews.get(claim_id, ClaimReviewState.UNREVIEWED)

    def sensitivity_of(self, claim_id: str) -> PrivacyClass:
        claim = self.claims[claim_id]
        refs = self.evidence.get(claim_id, [])
        classes = [self.sources[e.source_id].privacy_class for e in refs]
        return max([claim.sensitivity, *classes, *(e.sensitivity for e in refs)])

    def effective_attributes(
        self, claim_id: str
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        """``(attributes, conflicted)``: the identity attributes plus each
        attribute the evidence agrees on (or the owner resolved). A conflicted
        attribute is reported, never given a value."""

        claim = self.claims[claim_id]
        refs = self.evidence.get(claim_id, [])
        values = dict(claim.attributes)
        conflicted: list[str] = []
        for name in sorted(claim_attributes(refs)):
            effective, _ = attribute_conflict(claim, refs, name, self.resolutions)
            if effective.conflicted:
                conflicted.append(name)
            elif effective.value is not None:
                # The comparison form is lower-cased; show the source's own text.
                shown = self._display_value(refs, name, effective.value)
                values.setdefault(name, shown)
        return values, tuple(conflicted)

    @staticmethod
    def _display_value(refs: Sequence[EvidenceRef], name: str, normalized: str) -> str:
        for ref in refs:
            raw = ref.observed.get(name)
            if raw and normalize_value(name, raw) == normalized:
                return normalize_value(name, raw) if name in ("start", "end") else raw
        return normalized

    @cached_property
    def views(self) -> dict[str, ClaimView]:
        views: dict[str, ClaimView] = {}
        for claim_id in sorted(self.claims):
            claim = self.claims[claim_id]
            refs = self.evidence.get(claim_id, [])
            attributes, conflicted = self.effective_attributes(claim_id)
            strength = self.strength_of(claim_id)
            review = self.review_of(claim_id)
            accepted = is_accepted(refs, strength, review)
            views[claim_id] = ClaimView(
                claim_id=claim_id,
                category=claim.category,
                statement=claim.canonical_statement,
                strength=strength,
                review=review,
                accepted=accepted,
                inferred=has_nature(refs, EvidenceNature.INFERRED_RELATIONSHIP),
                candidate=has_nature(refs, EvidenceNature.MODEL_CANDIDATE),
                sensitivity=self.sensitivity_of(claim_id),
                attributes=attributes,
                conflicted_attributes=conflicted,
                evidence=tuple(self._evidence_view(e) for e in refs),
                source_count=len({e.source_id for e in refs}),
                source_family_count=len(
                    {
                        self.families[e.source_id]
                        for e in refs
                        if e.nature in DOCUMENTARY_NATURES
                    }
                ),
                canonical=not claim.key.startswith("unmapped|"),
            )
        return views

    def _evidence_view(self, ref: EvidenceRef) -> EvidenceView:
        source = self.sources[ref.source_id]
        context = self.claims.get(ref.context_claim_id or "")
        return EvidenceView(
            evidence_id=ref.evidence_id,
            source_id=ref.source_id,
            source_label=source.label,
            source_type=ref.source_type,
            source_family_id=self.families[ref.source_id],
            nature=ref.nature,
            location=ref.source_location,
            reference=ref.evidence_reference,
            context_claim_id=ref.context_claim_id if context else None,
            context_statement=context.canonical_statement if context else None,
            basis_evidence_id=ref.basis_evidence_id,
        )

    def view(self, claim_id: str) -> ClaimView | None:
        return self.views.get(claim_id)

    def by_category(
        self, *categories: ClaimCategory, accepted_only: bool = False
    ) -> list[ClaimView]:
        wanted = set(categories)
        return [
            v
            for v in self.views.values()
            if (not wanted or v.category in wanted)
            and (not accepted_only or v.accepted)
        ]

    # ----------------------------------------------------------- conflicts

    @cached_property
    def intervals(self) -> dict[str, tuple[str, str]]:
        """Resolved ``(start, end)`` of each ACCEPTED employment claim."""

        found: dict[str, tuple[str, str]] = {}
        for entry in self._timeline_entries:
            if entry.status is IntervalStatus.RESOLVED and entry.start and entry.end:
                found[entry.claim_id] = (entry.start, entry.end)
        return found

    @cached_property
    def conflicts(self) -> tuple[Conflict, ...]:
        return detect_conflicts(
            list(self.claims.values()),
            self.evidence,
            self.resolutions,
            self.intervals,
        )

    def open_conflicts(self) -> tuple[Conflict, ...]:
        return tuple(c for c in self.conflicts if not c.resolved)

    # ------------------------------------------------------------ timeline

    @cached_property
    def _timeline_entries(self) -> tuple[TimelineEntry, ...]:
        entries: list[TimelineEntry] = []
        for view in self.by_category(ClaimCategory.EMPLOYMENT, accepted_only=True):
            start = view.attributes.get("start")
            end = view.attributes.get("end")
            conflicted = {"start", "end"} & set(view.conflicted_attributes)
            employer = view.attributes.get("employer", "")
            title = view.attributes.get("title", "")
            if conflicted:
                status = IntervalStatus.CONFLICTED
            elif not start or not end:
                status = IntervalStatus.INCOMPLETE
            else:
                interval = Interval(start, end)
                status = (
                    IntervalStatus.RESOLVED
                    if interval.is_valid(self.today)
                    else IntervalStatus.INCOMPLETE
                )
            months = 0
            approximate = False
            if status is IntervalStatus.RESOLVED and start and end:
                interval = Interval(start, end)
                months = interval.months(self.today)
                approximate = interval.approximate
            entries.append(
                TimelineEntry(
                    claim_id=view.claim_id,
                    employer=employer,
                    title=title,
                    start=start,
                    end=end,
                    status=status,
                    months=months,
                    approximate=approximate,
                )
            )
        return tuple(sorted(entries, key=lambda e: (e.start or "", e.claim_id)))

    def timeline(
        self, max_sensitivity: PrivacyClass | None = None
    ) -> ExperienceSummary:
        """The resolved timeline. With ``max_sensitivity`` (used for anything
        that may reach a model) entries whose evidence is more sensitive are left
        out of the entries AND the totals."""

        entries = tuple(
            e
            for e in self._timeline_entries
            if max_sensitivity is None
            or self.sensitivity_of(e.claim_id) <= max_sensitivity
        )
        resolved = [
            Interval(e.start, e.end)
            for e in entries
            if e.status is IntervalStatus.RESOLVED and e.start and e.end
        ]
        return ExperienceSummary(
            entries=entries,
            total=Duration(union_months(resolved, self.today)),
            conservative_total=Duration(
                union_months(resolved, self.today, conservative=True)
            ),
            excluded_conflicted=tuple(
                e.claim_id for e in entries if e.status is IntervalStatus.CONFLICTED
            ),
            excluded_incomplete=tuple(
                e.claim_id for e in entries if e.status is IntervalStatus.INCOMPLETE
            ),
            gaps=tuple(find_gaps(resolved, self.today)),
        )

    def experience_months(
        self,
        skill_claim_id: str | None = None,
        *,
        conservative: bool = True,
        max_sensitivity: PrivacyClass | None = None,
    ) -> int:
        """Whole months covered by RESOLVED employment/project intervals,
        overall or for one skill (through the contexts its evidence sits in).
        Overlaps are merged; a conflicted or incomplete interval never counts."""

        wanted: set[str] | None = None
        if skill_claim_id is not None:
            wanted = {c.claim_id for c in self.graph.contexts_for(skill_claim_id)}
        intervals: list[Interval] = []
        for claim_id, (start, end) in self.intervals.items():
            if max_sensitivity is not None and (
                self.sensitivity_of(claim_id) > max_sensitivity
            ):
                continue
            if wanted is None or claim_id in wanted:
                intervals.append(Interval(start, end))
        if skill_claim_id is not None:
            for view in self.by_category(ClaimCategory.PROJECT, accepted_only=True):
                if view.claim_id in (wanted or set()) and (
                    max_sensitivity is None or view.sensitivity <= max_sensitivity
                ):
                    p_start = view.attributes.get("start")
                    p_end = view.attributes.get("end")
                    if (
                        p_start
                        and p_end
                        and not ({"start", "end"} & set(view.conflicted_attributes))
                    ):
                        intervals.append(Interval(p_start, p_end))
        return union_months(intervals, self.today, conservative=conservative)


__all__ = [
    "ClaimView",
    "EvidenceView",
    "ExperienceSummary",
    "Profile",
    "TimelineEntry",
]
