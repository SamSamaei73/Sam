"""Typed bridge models for the Professional Intelligence UI.

Requests are strict (unknown fields are refused) and carry no principal, no
permission, no verification state, no path and no endpoint: the UI can only
select an owner-chosen file's bytes, a privacy class, and answer the owner's own
review questions. Responses are display data: provenance, documentary evidence
strength, the owner's review and acceptance (three separate facts), never a
credential.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sam.desktop.models import OperationResult

SourceTypeLiteral = Literal[
    "master_cv",
    "publication",
    "transcript",
    "project_documentation",
    "github",
    "linkedin_export",
    "owner_document",
]
SourcePrivacyLiteral = Literal["public", "personal", "private"]
ResourceTypeLiteral = Literal["pdf", "txt", "markdown", "json", "csv"]
StrengthLiteral = Literal["none", "single_source", "corroborated"]
ReviewLiteral = Literal["unreviewed", "owner_confirmed", "owner_rejected"]
NatureLiteral = Literal[
    "explicit_source", "inferred_relationship", "owner_attestation", "model_candidate"
]
SensitivityLiteral = Literal["public", "normal", "personal", "private"]
FreshnessLiteral = Literal["fresh", "aging", "stale"]
MatchLiteral = Literal["matched", "partially_supported", "not_supported", "unknown"]

MAX_DOCUMENT_BASE64 = 28_000_000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------- requests


class ProfessionalIngestRequest(_Strict):
    name: str = Field(min_length=1, max_length=200)
    source_type: SourceTypeLiteral
    privacy_class: SourcePrivacyLiteral
    resource_type: ResourceTypeLiteral
    content_base64: str = Field(min_length=1, max_length=MAX_DOCUMENT_BASE64)
    use_candidates: bool = False


class ProfessionalReviewRequest(_Strict):
    action: Literal["confirm", "reject", "resolve", "set_privacy"]
    claim_id: str | None = Field(default=None, max_length=64)
    conflict_id: str | None = Field(default=None, max_length=64)
    option_id: str | None = Field(default=None, max_length=64)
    source_id: str | None = Field(default=None, max_length=64)
    privacy_class: SourcePrivacyLiteral | None = None


class ProfessionalRemoveRequest(_Strict):
    source_id: str = Field(min_length=1, max_length=64)
    confirmation_id: str | None = Field(default=None, max_length=100)


class ProfessionalQueryRequest(_Strict):
    mode: Literal["search", "evidence_for"]
    text: str = Field(min_length=1, max_length=300)


# -------------------------------------------------------------- view data


class ProEvidence(BaseModel):
    evidence_id: str
    source_id: str
    source_label: str
    source_type: SourceTypeLiteral
    source_family_id: str
    nature: NatureLiteral
    page_number: int | None = None
    section_title: str | None = None
    paragraph_index: int | None = None
    character_start: int | None = None
    character_end: int | None = None
    reference: str = ""
    context_claim_id: str | None = None
    context_statement: str | None = None
    basis_evidence_id: str | None = None


class ProClaim(BaseModel):
    claim_id: str
    category: str
    statement: str
    strength: StrengthLiteral
    review: ReviewLiteral
    accepted: bool
    inferred: bool = False
    candidate: bool = False
    sensitivity: SensitivityLiteral
    attributes: dict[str, str] = Field(default_factory=dict)
    conflicted_attributes: list[str] = Field(default_factory=list)
    source_count: int = 0
    source_family_count: int = 0
    canonical: bool = True
    evidence: list[ProEvidence] = Field(default_factory=list)


class ProPublication(BaseModel):
    claim_id: str
    strength: StrengthLiteral
    review: ReviewLiteral
    accepted: bool
    sensitivity: SensitivityLiteral
    title: str
    authors: list[str] = Field(default_factory=list)
    owner_author_position: int | None = None
    venue: str | None = None
    year: str | None = None
    doi: str | None = None
    abstract: str | None = None
    methods: str | None = None
    models: list[str] = Field(default_factory=list)
    datasets: str | None = None
    findings: str | None = None
    limitations: str | None = None
    topics: list[str] = Field(default_factory=list)
    owner_contribution: str | None = None
    evidence: list[ProEvidence] = Field(default_factory=list)
    conflicted_attributes: list[str] = Field(default_factory=list)


class ProEducation(BaseModel):
    claim_id: str
    strength: StrengthLiteral
    review: ReviewLiteral
    accepted: bool
    sensitivity: SensitivityLiteral
    institution: str | None = None
    degree: str
    subject: str
    classification: str | None = None
    start: str | None = None
    end: str | None = None
    modules: list[str] = Field(default_factory=list)
    dissertation: str | None = None
    evidence: list[ProEvidence] = Field(default_factory=list)
    conflicted_attributes: list[str] = Field(default_factory=list)


class ProTimelineEntry(BaseModel):
    claim_id: str
    employer: str
    title: str
    start: str | None = None
    end: str | None = None
    status: Literal["resolved", "conflicted", "incomplete"]
    months: int = 0
    approximate: bool = False


class ProExperience(BaseModel):
    entries: list[ProTimelineEntry] = Field(default_factory=list)
    total_months: int = 0
    total_years: int = 0
    remainder_months: int = 0
    conservative_months: int = 0
    excluded_conflicted: int = 0
    excluded_incomplete: int = 0
    gaps: list[str] = Field(default_factory=list)


class ProConflictOption(BaseModel):
    option_id: str
    value: str
    claim_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)


class ProConflict(BaseModel):
    conflict_id: str
    kind: Literal["attribute", "role_overlap"]
    attribute: str
    claim_ids: list[str]
    options: list[ProConflictOption]
    resolved: bool = False
    resolved_value: str | None = None


class ProGap(BaseModel):
    kind: str
    detail: str
    claim_id: str | None = None
    source_id: str | None = None


class ProSource(BaseModel):
    source_id: str
    label: str
    source_type: SourceTypeLiteral
    source_family_id: str
    version: int = 1
    privacy_class: SourcePrivacyLiteral
    freshness: FreshnessLiteral
    ingested_at: str
    refreshed_at: str
    resource_type: str
    accepted_claims: int
    pending_candidates: int
    conflicts: int
    candidate_extraction: str


# --------------------------------------------------------------- responses


class ProfessionalProfileResponse(OperationResult):
    claims: list[ProClaim] = Field(default_factory=list)
    publications: list[ProPublication] = Field(default_factory=list)
    education: list[ProEducation] = Field(default_factory=list)
    experience: ProExperience | None = None
    conflicts: list[ProConflict] = Field(default_factory=list)
    gaps: list[ProGap] = Field(default_factory=list)
    sources: list[ProSource] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class ProfessionalIngestResponse(OperationResult):
    source_id: str | None = None
    ingest_status: str | None = None
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


class ProRequirement(BaseModel):
    status: MatchLiteral
    normalized_requirement: str = ""
    skills: list[ProClaim] = Field(default_factory=list)
    inferred_skills: list[ProClaim] = Field(default_factory=list)
    projects: list[ProClaim] = Field(default_factory=list)
    employment: list[ProClaim] = Field(default_factory=list)
    research: list[ProClaim] = Field(default_factory=list)
    education: list[ProClaim] = Field(default_factory=list)
    unsupported_aspects: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ProfessionalQueryResponse(OperationResult):
    mode: Literal["search", "evidence_for"] | None = None
    claims: list[ProClaim] = Field(default_factory=list)
    requirement: ProRequirement | None = None


__all__ = [
    "MAX_DOCUMENT_BASE64",
    "ProClaim",
    "ProConflict",
    "ProConflictOption",
    "ProEducation",
    "ProEvidence",
    "ProExperience",
    "ProGap",
    "ProPublication",
    "ProRequirement",
    "ProSource",
    "ProTimelineEntry",
    "ProfessionalIngestRequest",
    "ProfessionalIngestResponse",
    "ProfessionalProfileResponse",
    "ProfessionalQueryRequest",
    "ProfessionalQueryResponse",
    "ProfessionalRemoveRequest",
    "ProfessionalReviewRequest",
]
