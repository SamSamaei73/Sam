"""Fit analysis and research alignment, from evidence only.

For every requirement Career asks the Phase 14 requirement matcher and reports
SUPPORTED / PARTIALLY_SUPPORTED / NOT_SUPPORTED / UNKNOWN with the matching
claims, their evidence ids, the supporting projects / employment / publications,
and the unsupported aspects. There is deliberately no numeric "fit score": the
result is a list of evidence-backed facts and gaps, not a judgement.

Research alignment compares a programme's or supervisor's stated topics with the
owner's research, publication and project evidence: SUPPORTED_ALIGNMENT,
PARTIAL_ALIGNMENT, NO_EVIDENCE or UNKNOWN (no topics stated, or the evidence
could not be read). It never claims a research capability without evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sam.career.evidence import EvidenceClaim, EvidencePort
from sam.career.models import AlignmentStatus, CareerOpportunity, FitStatus, Requirement
from sam.permissions.models import Principal

_RESEARCH = frozenset({"research", "publication", "project", "domain_experience"})


@dataclass(frozen=True)
class RequirementFit:
    requirement: str
    preferred: bool
    status: FitStatus
    claims: tuple[EvidenceClaim, ...]
    unsupported: tuple[str, ...]
    notes: tuple[str, ...]

    def of(self, *categories: str) -> tuple[EvidenceClaim, ...]:
        return tuple(c for c in self.claims if c.category in categories)


@dataclass(frozen=True)
class FitReport:
    opportunity_id: str
    requirements: tuple[RequirementFit, ...]

    def count(self, status: FitStatus) -> int:
        return sum(1 for r in self.requirements if r.status is status)

    @property
    def supported_claims(self) -> tuple[EvidenceClaim, ...]:
        seen: dict[str, EvidenceClaim] = {}
        for fit in self.requirements:
            if fit.status in (FitStatus.SUPPORTED, FitStatus.PARTIALLY_SUPPORTED):
                for claim in fit.claims:
                    seen.setdefault(claim.claim_id, claim)
        return tuple(seen.values())


@dataclass(frozen=True)
class TopicAlignment:
    topic: str
    status: AlignmentStatus
    claims: tuple[EvidenceClaim, ...]


@dataclass(frozen=True)
class AlignmentReport:
    status: AlignmentStatus
    topics: tuple[TopicAlignment, ...]


def analyze_fit(
    principal: Principal,
    opportunity: CareerOpportunity,
    evidence: EvidencePort,
    requirements: Iterable[Requirement] | None = None,
) -> FitReport:
    out: list[RequirementFit] = []
    for requirement in (
        requirements if requirements is not None else opportunity.requirements
    ):
        result = evidence.requirement(principal, requirement.text)
        if result is None:
            out.append(
                RequirementFit(
                    requirement.text,
                    requirement.preferred,
                    FitStatus.UNKNOWN,
                    (),
                    (),
                    ("evidence_unavailable",),
                )
            )
            continue
        out.append(
            RequirementFit(
                requirement=requirement.text,
                preferred=requirement.preferred,
                status=result.status,
                claims=result.claims,
                unsupported=result.unsupported,
                notes=result.notes,
            )
        )
    return FitReport(opportunity.opportunity_id, tuple(out))


def research_alignment(
    principal: Principal, topics: Iterable[str], evidence: EvidencePort
) -> AlignmentReport:
    results: list[TopicAlignment] = []
    for topic in topics:
        result = evidence.requirement(principal, topic)
        if result is None:
            results.append(TopicAlignment(topic, AlignmentStatus.UNKNOWN, ()))
            continue
        research = tuple(c for c in result.claims if c.category in _RESEARCH)
        if result.status is FitStatus.SUPPORTED and research:
            status = AlignmentStatus.SUPPORTED_ALIGNMENT
        elif result.status in (FitStatus.SUPPORTED, FitStatus.PARTIALLY_SUPPORTED) and (
            research or result.claims
        ):
            status = AlignmentStatus.PARTIAL_ALIGNMENT
        else:
            # The evidence WAS read and nothing matches: no evidence (UNKNOWN is
            # kept for "could not read the evidence").
            status = AlignmentStatus.NO_EVIDENCE
        results.append(TopicAlignment(topic, status, research or result.claims))
    if not results:
        return AlignmentReport(AlignmentStatus.UNKNOWN, ())
    statuses = {r.status for r in results}
    if statuses == {AlignmentStatus.SUPPORTED_ALIGNMENT}:
        overall = AlignmentStatus.SUPPORTED_ALIGNMENT
    elif statuses & {
        AlignmentStatus.SUPPORTED_ALIGNMENT,
        AlignmentStatus.PARTIAL_ALIGNMENT,
    }:
        overall = AlignmentStatus.PARTIAL_ALIGNMENT
    elif statuses == {AlignmentStatus.UNKNOWN}:
        overall = AlignmentStatus.UNKNOWN
    else:
        overall = AlignmentStatus.NO_EVIDENCE
    return AlignmentReport(overall, tuple(results))


__all__ = [
    "AlignmentReport",
    "FitReport",
    "RequirementFit",
    "TopicAlignment",
    "analyze_fit",
    "research_alignment",
]
