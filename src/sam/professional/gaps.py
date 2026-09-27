"""Factual profile-gap analysis.

Reports EVIDENCE gaps only: where a claim rests on a single kind of source, where
sources disagree, where something is missing from the CV, where a source is old.
It does not rate, score or label the owner: there is no "weak" or "strong"
here, and no career score. Every gap names a fact about the evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sam.models.models import PrivacyClass
from sam.professional.models import (
    ClaimCategory,
    EvidenceNature,
    EvidenceStrength,
    Freshness,
    SourceType,
)
from sam.professional.profile import Profile
from sam.professional.timeline import IntervalStatus


class GapKind(StrEnum):
    SKILL_ONLY_IN_CV = "skill_only_in_cv"
    SKILL_INFERRED_ONLY = "skill_inferred_only"
    SKILL_NOT_IN_CV = "skill_not_in_cv"
    PROJECT_MISSING_FROM_CV = "project_missing_from_cv"
    PUBLICATION_MISSING_FROM_CV = "publication_missing_from_cv"
    PROJECT_NO_QUANTIFIED_OUTCOME = "project_no_quantified_outcome"
    CONFLICT_OPEN = "conflict_open"
    DATES_CONFLICTED = "dates_conflicted"
    DATES_INCOMPLETE = "dates_incomplete"
    TIMELINE_GAP = "timeline_gap"
    SOURCE_STALE = "source_stale"
    CANDIDATES_UNREVIEWED = "candidates_unreviewed"
    NO_MASTER_CV = "no_master_cv"


@dataclass(frozen=True)
class ProfileGap:
    kind: GapKind
    detail: str
    claim_id: str | None = None
    source_id: str | None = None


_DECLARED = (SourceType.MASTER_CV, SourceType.LINKEDIN_EXPORT)


def analyze_gaps(
    profile: Profile, max_sensitivity: PrivacyClass | None = None
) -> tuple[ProfileGap, ...]:
    """Evidence gaps. With ``max_sensitivity`` (used for anything that may reach
    a model) gaps about more-sensitive claims or sources are left out."""

    gaps: list[ProfileGap] = []
    has_cv = any(
        s.source_type is SourceType.MASTER_CV for s in profile.sources.values()
    )

    for view in profile.by_category(ClaimCategory.SKILL, ClaimCategory.TECHNOLOGY):
        types = {
            e.source_type
            for e in view.evidence
            if e.nature is EvidenceNature.EXPLICIT_SOURCE
        }
        if view.strength is EvidenceStrength.SINGLE_SOURCE and types == {
            SourceType.MASTER_CV
        }:
            gaps.append(
                ProfileGap(
                    GapKind.SKILL_ONLY_IN_CV,
                    f"{view.statement}: stated in the CV only, no other source",
                    view.claim_id,
                )
            )
        elif not view.accepted and view.inferred:
            gaps.append(
                ProfileGap(
                    GapKind.SKILL_INFERRED_ONLY,
                    f"{view.statement}: appears only as a rule-derived relationship",
                    view.claim_id,
                )
            )
        elif has_cv and view.accepted and not any(t in _DECLARED for t in types):
            gaps.append(
                ProfileGap(
                    GapKind.SKILL_NOT_IN_CV,
                    f"{view.statement}: evidenced in project or other sources but "
                    "not listed in the CV",
                    view.claim_id,
                )
            )

    if has_cv:
        for view in profile.by_category(ClaimCategory.PROJECT):
            if view.accepted and not any(
                e.source_type is SourceType.MASTER_CV for e in view.evidence
            ):
                gaps.append(
                    ProfileGap(
                        GapKind.PROJECT_MISSING_FROM_CV,
                        f"{view.statement}: not in the CV",
                        view.claim_id,
                    )
                )
        for view in profile.by_category(ClaimCategory.PUBLICATION):
            if view.accepted and not any(
                e.source_type is SourceType.MASTER_CV for e in view.evidence
            ):
                gaps.append(
                    ProfileGap(
                        GapKind.PUBLICATION_MISSING_FROM_CV,
                        f"{view.statement}: not in the CV",
                        view.claim_id,
                    )
                )
    elif profile.claims:
        gaps.append(ProfileGap(GapKind.NO_MASTER_CV, "no Master CV source is ingested"))

    achievements_by_context: dict[str, int] = {}
    for view in profile.by_category(ClaimCategory.ACHIEVEMENT):
        for evidence in view.evidence:
            if evidence.context_claim_id:
                achievements_by_context[evidence.context_claim_id] = (
                    achievements_by_context.get(evidence.context_claim_id, 0) + 1
                )
    for view in profile.by_category(ClaimCategory.PROJECT):
        if view.accepted and view.claim_id not in achievements_by_context:
            gaps.append(
                ProfileGap(
                    GapKind.PROJECT_NO_QUANTIFIED_OUTCOME,
                    f"{view.statement}: no quantified outcome in the evidence",
                    view.claim_id,
                )
            )

    for conflict in profile.open_conflicts():
        gaps.append(
            ProfileGap(
                GapKind.CONFLICT_OPEN,
                f"sources disagree on {conflict.attribute} "
                f"({len(conflict.options)} values)",
                conflict.claim_ids[0],
            )
        )

    summary = profile.timeline(max_sensitivity)
    for entry in summary.entries:
        if entry.status is IntervalStatus.CONFLICTED:
            gaps.append(
                ProfileGap(
                    GapKind.DATES_CONFLICTED,
                    "employment dates conflict and are excluded from experience totals",
                    entry.claim_id,
                )
            )
        elif entry.status is IntervalStatus.INCOMPLETE:
            gaps.append(
                ProfileGap(
                    GapKind.DATES_INCOMPLETE,
                    "employment dates are incomplete and are excluded from "
                    "experience totals",
                    entry.claim_id,
                )
            )
    for start, end, months in summary.gaps:
        gaps.append(
            ProfileGap(
                GapKind.TIMELINE_GAP,
                f"no employment evidence from {start} to {end} ({months} months)",
            )
        )

    now = _now(profile)
    for source in profile.sources.values():
        if source.freshness(now) is Freshness.STALE:
            gaps.append(
                ProfileGap(
                    GapKind.SOURCE_STALE,
                    "source last refreshed more than a year ago",
                    source_id=source.source_id,
                )
            )

    pending = [v for v in profile.views.values() if not v.accepted and v.candidate]
    if pending:
        gaps.append(
            ProfileGap(
                GapKind.CANDIDATES_UNREVIEWED,
                f"{len(pending)} model-suggested candidates await owner review",
            )
        )
    if max_sensitivity is not None:
        gaps = [
            g
            for g in gaps
            if (
                g.claim_id is None
                or profile.sensitivity_of(g.claim_id) <= max_sensitivity
            )
            and (
                g.source_id is None
                or profile.sources[g.source_id].privacy_class <= max_sensitivity
            )
        ]
    return tuple(sorted(gaps, key=lambda g: (g.kind.value, g.claim_id or "", g.detail)))


def _now(profile: Profile):  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    return datetime(
        profile.today.year, profile.today.month, profile.today.day, tzinfo=UTC
    )


__all__ = ["GapKind", "ProfileGap", "analyze_gaps"]
