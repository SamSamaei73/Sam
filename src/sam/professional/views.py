"""Result and view types returned by ``ProfessionalService``.

Plain, immutable, content-bounded data. Nothing here carries a credential, a
raw document, or a permission decision other than the engine's own summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sam.models.models import PrivacyClass
from sam.professional.gaps import ProfileGap
from sam.professional.models import (
    ClaimReviewState,
    Conflict,
    EvidenceStrength,
    Freshness,
    SourceType,
)
from sam.professional.profile import ClaimView, EvidenceView, ExperienceSummary


@dataclass(frozen=True)
class OpResult[T]:
    """One operation's outcome. ``permission`` is the PermissionEngine's own
    decision ("allow" / "deny" / "confirm_required"); ``reason`` is a short,
    content-free code."""

    permission: str
    ok: bool
    reason: str | None = None
    confirmation_id: str | None = None
    data: T | None = None
    operation_id: str = ""


@dataclass(frozen=True)
class PublicationView:
    claim_id: str
    strength: EvidenceStrength
    review: ClaimReviewState
    accepted: bool
    sensitivity: PrivacyClass
    title: str
    authors: tuple[str, ...]
    owner_author_position: int | None
    venue: str | None
    year: str | None
    doi: str | None
    abstract: str | None
    methods: str | None
    models: tuple[str, ...]
    datasets: str | None
    findings: str | None
    limitations: str | None
    topics: tuple[str, ...]
    owner_contribution: str | None
    evidence: tuple[EvidenceView, ...]
    conflicted_attributes: tuple[str, ...]


@dataclass(frozen=True)
class EducationView:
    claim_id: str
    strength: EvidenceStrength
    review: ClaimReviewState
    accepted: bool
    sensitivity: PrivacyClass
    institution: str | None
    degree: str
    subject: str
    classification: str | None
    start: str | None
    end: str | None
    modules: tuple[str, ...]
    dissertation: str | None
    evidence: tuple[EvidenceView, ...]
    conflicted_attributes: tuple[str, ...]


@dataclass(frozen=True)
class SourceView:
    source_id: str
    label: str
    source_type: SourceType
    source_family_id: str
    version: int
    privacy_class: PrivacyClass
    freshness: Freshness
    ingested_at: str
    refreshed_at: str
    resource_type: str
    accepted_claims: int
    pending_candidates: int
    conflicts: int
    candidate_extraction: str


@dataclass(frozen=True)
class ProfileSnapshot:
    claims: tuple[ClaimView, ...]
    publications: tuple[PublicationView, ...]
    education: tuple[EducationView, ...]
    timeline: ExperienceSummary
    sources: tuple[SourceView, ...]
    conflicts: tuple[Conflict, ...]
    gaps: tuple[ProfileGap, ...]


@dataclass(frozen=True)
class ExperienceAnswer:
    """Experience computed by code from resolved dates. Never a model estimate."""

    subject: str | None
    verified: bool
    months: int
    years: int
    remainder_months: int
    conservative_months: int
    reason: str


@dataclass(frozen=True)
class IngestSummary:
    status: str  # ingested | refreshed | unchanged | duplicate
    source_id: str
    claims_created: int = 0
    claims_updated: int = 0
    claims_skipped_rejected: int = 0
    evidence_added: int = 0
    conflicts_open: int = 0
    candidate_extraction: str = "off"
    candidates_proposed: int = 0
    candidates_accepted: int = 0
    candidates_rejected: int = 0
    unmapped_skills: int = 0
    claim_ids: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class RemovalSummary:
    source_id: str
    claims_affected: int
    claims_removed: int


__all__ = [
    "EducationView",
    "ExperienceAnswer",
    "IngestSummary",
    "OpResult",
    "ProfileSnapshot",
    "PublicationView",
    "RemovalSummary",
    "SourceView",
]
