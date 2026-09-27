"""Typed, immutable domain models for Professional Intelligence.

The evidence model is multi-source: a ``ProfessionalClaim`` never owns a single
``source_id``. Every claim has zero or more ``EvidenceRef`` records, each
pointing at one ``SourceRecord``.

Three concepts are deliberately INDEPENDENT (see ``sam.professional.evidence``):

* ``EvidenceNature``: what one piece of evidence is (a documentary statement, a
  rule-derived relationship, the owner's attestation, a model candidate);
* ``EvidenceStrength``: how much DOCUMENTARY support a claim has, counted in
  independent source families. Owner attestation never adds to it;
* ``ClaimReviewState``: what the owner decided about the claim.

Strength and acceptance are COMPUTED from evidence and review on every read,
never assigned by a model and never trusted from storage.

This module performs no I/O, parsing, or provider calls. It imports nothing
from ``sam.memory``: Professional Intelligence is a separate trust domain from
Memory and from Knowledge (see ``docs/professional-intelligence.md``).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sam.models.models import PrivacyClass

# ---------------------------------------------------------------- bounds

MAX_LABEL_LENGTH = 200
MAX_STATEMENT_LENGTH = 500
MAX_ATTRIBUTE_VALUE_LENGTH = 1_000
MAX_ATTRIBUTES = 24
MAX_REFERENCE_LENGTH = 240
MAX_EVIDENCE_PER_CLAIM = 200
MAX_CLAIMS_PER_SOURCE = 2_000
MAX_QUERY_LENGTH = 300

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ATTRIBUTE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

# Identifiers that are never professional evidence. An attribute with one of
# these names is rejected outright, so a student number or an account number
# can never be stored as if it were a fact.
FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "student_id",
        "student_number",
        "student_no",
        "account_number",
        "account_no",
        "national_id",
        "passport_number",
        "password",
        "token",
        "api_key",
        "secret",
        "email",
        "phone",
        "address",
        "date_of_birth",
        "dob",
    }
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def clean_text(value: str, *, max_length: int) -> str:
    cleaned = _CONTROL.sub("", value).strip()
    return re.sub(r"[ \t]+", " ", cleaned)[:max_length]


def stable_id(prefix: str, *parts: str, length: int = 24) -> str:
    """Deterministic identifier: the same inputs always give the same id, so
    re-ingesting identical content is idempotent and ids are reproducible."""

    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:length]}"


# ----------------------------------------------------------------- enums


class ClaimCategory(StrEnum):
    EDUCATION = "education"
    EMPLOYMENT = "employment"
    PROJECT = "project"
    SKILL = "skill"
    TECHNOLOGY = "technology"
    CERTIFICATION = "certification"
    PUBLICATION = "publication"
    RESEARCH = "research"
    ACHIEVEMENT = "achievement"
    RESPONSIBILITY = "responsibility"
    DOMAIN_EXPERIENCE = "domain_experience"
    LANGUAGE = "language"
    PORTFOLIO = "portfolio"
    CAREER_PREFERENCE = "career_preference"


class SourceType(StrEnum):
    """The closed set of owner-approved source types."""

    MASTER_CV = "master_cv"
    PUBLICATION = "publication"
    TRANSCRIPT = "transcript"
    PROJECT_DOCUMENTATION = "project_documentation"
    GITHUB = "github"
    LINKEDIN_EXPORT = "linkedin_export"
    OWNER_DOCUMENT = "owner_document"


class EvidenceNature(StrEnum):
    """What one ``EvidenceRef`` is. Independent of the owner's review.

    ``EXPLICIT_SOURCE``: an owner-selected document states it (documentary).
    ``INFERRED_RELATIONSHIP``: a rule-derived relationship (a dependency
    manifest, a publication topic): never presented as an explicit fact.
    ``OWNER_ATTESTATION``: the owner vouched for the claim through the trusted
    review workflow; it always names the evidence the owner reviewed and is
    never counted as documentary support.
    ``MODEL_CANDIDATE``: a model suggestion, always untrusted."""

    EXPLICIT_SOURCE = "explicit_source"
    INFERRED_RELATIONSHIP = "inferred_relationship"
    OWNER_ATTESTATION = "owner_attestation"
    MODEL_CANDIDATE = "model_candidate"


# Only a document can support a claim documentarily.
DOCUMENTARY_NATURES = frozenset({EvidenceNature.EXPLICIT_SOURCE})
# Evidence that can make a claim ACCEPTED (with the owner's review for an
# attestation). A model candidate and an inferred relationship never can.
TRUSTED_NATURES = frozenset(
    {EvidenceNature.EXPLICIT_SOURCE, EvidenceNature.OWNER_ATTESTATION}
)


class EvidenceStrength(StrEnum):
    """Documentary support, computed from EXPLICIT_SOURCE evidence only and
    counted in independent source FAMILIES (never in files or source types)."""

    NONE = "none"
    SINGLE_SOURCE = "single_source"
    CORROBORATED = "corroborated"


class ClaimReviewState(StrEnum):
    """The owner's decision about a claim, recorded only by the owner's review
    workflow. It never changes the claim's documentary strength."""

    UNREVIEWED = "unreviewed"
    OWNER_CONFIRMED = "owner_confirmed"
    OWNER_REJECTED = "owner_rejected"


class Freshness(StrEnum):
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"


class ConflictKind(StrEnum):
    ATTRIBUTE = "attribute"
    ROLE_OVERLAP = "role_overlap"


class MatchStatus(StrEnum):
    MATCHED = "matched"
    PARTIALLY_SUPPORTED = "partially_supported"
    NOT_SUPPORTED = "not_supported"
    UNKNOWN = "unknown"


# The only attributes each category may carry. A candidate or extractor that
# offers any other name (a student number, an email) is rejected at validation.
ATTRIBUTE_ALLOWLIST: dict[ClaimCategory, frozenset[str]] = {
    ClaimCategory.EDUCATION: frozenset(
        {
            "institution",
            "degree",
            "subject",
            "classification",
            "start",
            "end",
            "modules",
            "dissertation",
        }
    ),
    ClaimCategory.EMPLOYMENT: frozenset({"employer", "title", "start", "end"}),
    ClaimCategory.PROJECT: frozenset(
        {"name", "description", "start", "end", "role", "outcome"}
    ),
    ClaimCategory.SKILL: frozenset({"skill_id", "display"}),
    ClaimCategory.TECHNOLOGY: frozenset({"skill_id", "display"}),
    ClaimCategory.CERTIFICATION: frozenset({"name", "issuer", "date"}),
    ClaimCategory.PUBLICATION: frozenset(
        {
            "title",
            "authors",
            "owner_author_position",
            "venue",
            "year",
            "doi",
            "abstract",
            "methods",
            "models",
            "datasets",
            "findings",
            "limitations",
            "topics",
            "owner_contribution",
        }
    ),
    ClaimCategory.RESEARCH: frozenset({"topic"}),
    ClaimCategory.ACHIEVEMENT: frozenset({"description", "metric"}),
    ClaimCategory.RESPONSIBILITY: frozenset({"description"}),
    ClaimCategory.DOMAIN_EXPERIENCE: frozenset({"domain"}),
    ClaimCategory.LANGUAGE: frozenset({"language", "level"}),
    ClaimCategory.PORTFOLIO: frozenset({"url", "label"}),
    ClaimCategory.CAREER_PREFERENCE: frozenset({"kind", "value"}),
}

# Attributes whose value is a date and can therefore conflict across sources.
DATE_ATTRIBUTES = frozenset({"start", "end", "date"})

# Sensitivity floor per category. Salary/visa/clearance text is raised to
# PRIVATE by the claim policy on top of this.
CATEGORY_SENSITIVITY: dict[ClaimCategory, PrivacyClass] = {
    ClaimCategory.CAREER_PREFERENCE: PrivacyClass.PERSONAL,
}


# ------------------------------------------------------------ small models


class SourceLocation(BaseModel):
    """Where evidence sits inside a source: the same fields Knowledge exposes,
    every one optional and never fabricated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_number: int | None = Field(default=None, ge=1)
    section_title: str | None = Field(default=None, max_length=300)
    paragraph_index: int | None = Field(default=None, ge=0)
    character_start: int | None = Field(default=None, ge=0)
    character_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _offsets(self) -> Self:
        if (
            self.character_start is not None
            and self.character_end is not None
            and self.character_end < self.character_start
        ):
            raise ValueError("character_end must not be before character_start")
        return self


def _validate_attributes(value: Mapping[str, str], *, field: str) -> Mapping[str, str]:
    if len(value) > MAX_ATTRIBUTES:
        raise ValueError(f"too many {field}")
    cleaned: dict[str, str] = {}
    for name, item in value.items():
        if not _ATTRIBUTE_NAME.match(name) or name in FORBIDDEN_ATTRIBUTES:
            raise ValueError(f"attribute name not allowed in {field}")
        text = clean_text(item, max_length=MAX_ATTRIBUTE_VALUE_LENGTH)
        if text:
            cleaned[name] = text
    return dict(sorted(cleaned.items()))


class ProfessionalClaim(BaseModel):
    """One professional fact about the owner, identified by a deterministic key.

    ``attributes`` holds only the IDENTITY of the claim (employer and title,
    degree and subject, the skill id ...). Everything a source asserts about it
    (dates, classification ...) lives on the evidence, so two sources that
    disagree produce a visible conflict instead of one silently winning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(min_length=1, max_length=64)
    category: ClaimCategory
    canonical_statement: str = Field(min_length=1, max_length=MAX_STATEMENT_LENGTH)
    key: str = Field(min_length=1, max_length=300)
    attributes: dict[str, str] = Field(default_factory=dict)
    # The category's sensitivity FLOOR. What a source says about the claim
    # (a salary cue ...) raises it through the evidence, never through here.
    sensitivity: PrivacyClass = PrivacyClass.PERSONAL
    created_at: datetime
    updated_at: datetime

    @field_validator("canonical_statement", mode="before")
    @classmethod
    def _statement(cls, value: str) -> str:
        return clean_text(value, max_length=MAX_STATEMENT_LENGTH)

    @field_validator("attributes")
    @classmethod
    def _attributes(cls, value: dict[str, str]) -> dict[str, str]:
        return dict(_validate_attributes(value, field="attributes"))

    @field_validator("sensitivity")
    @classmethod
    def _sensitivity(cls, value: PrivacyClass) -> PrivacyClass:
        if value is PrivacyClass.SECRET:
            raise ValueError("SECRET material is never a professional claim")
        return value

    @model_validator(mode="after")
    def _shape(self) -> Self:
        allowed = ATTRIBUTE_ALLOWLIST[self.category]
        if not set(self.attributes) <= allowed:
            raise ValueError("attribute not allowed for this category")
        if not self.canonical_statement.strip():
            raise ValueError("statement must not be blank")
        return self


def _no_secret(value: PrivacyClass) -> PrivacyClass:
    if value is PrivacyClass.SECRET:
        raise ValueError("SECRET material is never professional evidence")
    return value


class EvidenceRef(BaseModel):
    """One piece of evidence supporting one claim, from exactly one source.

    ``evidence_reference`` is a short scrubbed excerpt of the source line. It is
    shown to the OWNER only: it never enters an audit event and never reaches a
    model through the professional tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=64)
    claim_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=64)
    source_type: SourceType
    source_location: SourceLocation = Field(default_factory=SourceLocation)
    evidence_reference: str = Field(default="", max_length=MAX_REFERENCE_LENGTH)
    nature: EvidenceNature
    # What THIS evidence asserts about the claim (dates, classification ...).
    observed: dict[str, str] = Field(default_factory=dict)
    # The project / employment / publication / education claim this evidence
    # sits inside (the skill graph edge: skill -> evidence -> context -> source).
    context_claim_id: str | None = Field(default=None, max_length=64)
    # How sensitive what THIS evidence says is (a salary cue makes it PRIVATE).
    sensitivity: PrivacyClass = PrivacyClass.PUBLIC
    # OWNER_ATTESTATION only: the evidence the owner reviewed (its provenance).
    basis_evidence_id: str | None = Field(default=None, max_length=64)
    created_at: datetime

    @field_validator("sensitivity")
    @classmethod
    def _sensitivity(cls, value: PrivacyClass) -> PrivacyClass:
        return _no_secret(value)

    @model_validator(mode="after")
    def _attestation(self) -> Self:
        attesting = self.nature is EvidenceNature.OWNER_ATTESTATION
        if attesting != (self.basis_evidence_id is not None):
            raise ValueError("only an owner attestation names a basis")
        if attesting and self.observed:
            raise ValueError("an owner attestation asserts no attribute values")
        if self.nature is EvidenceNature.MODEL_CANDIDATE and self.observed:
            raise ValueError("a model candidate asserts no attribute values")
        return self

    @field_validator("evidence_reference", mode="before")
    @classmethod
    def _reference(cls, value: str) -> str:
        return clean_text(value, max_length=MAX_REFERENCE_LENGTH)

    @field_validator("observed")
    @classmethod
    def _observed(cls, value: dict[str, str]) -> dict[str, str]:
        return dict(_validate_attributes(value, field="observed"))


MAX_LINE_FINGERPRINTS = 4_000


class SourceRecord(BaseModel):
    """An owner-selected source.

    ``source_id`` is the LOGICAL source and never changes when its content does;
    ``checksum`` and ``version`` describe the content currently ingested.
    ``independence_key`` and the content fingerprints let code (never a model)
    decide which sources belong to one source FAMILY: two versions of one CV, a
    renamed copy, or the same text under another file name are one family and
    can never corroborate each other (see ``sam.professional.lineage``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1, max_length=64)
    source_type: SourceType
    label: str = Field(min_length=1, max_length=MAX_LABEL_LENGTH)
    checksum: str = Field(min_length=64, max_length=64)
    version: int = Field(default=1, ge=1)
    independence_key: str = Field(min_length=1, max_length=100)
    # sha256 of the normalized text, and short hashes of its normalized lines:
    # comparison material only, never text, never shown.
    content_fingerprint: str = Field(min_length=64, max_length=64)
    line_fingerprints: frozenset[str] = Field(default_factory=frozenset)
    privacy_class: PrivacyClass
    resource_type: str = Field(min_length=1, max_length=20)
    segment_count: int = Field(ge=0)
    extractor_version: str = Field(min_length=1, max_length=20)
    candidate_extraction: str = Field(default="off", max_length=20)
    ingested_at: datetime
    refreshed_at: datetime

    @field_validator("label", mode="before")
    @classmethod
    def _label(cls, value: str) -> str:
        return clean_text(value, max_length=MAX_LABEL_LENGTH)

    @field_validator("privacy_class")
    @classmethod
    def _privacy(cls, value: PrivacyClass) -> PrivacyClass:
        # SECRET is never ingested; NORMAL is not an owner choice for a source.
        if value not in (
            PrivacyClass.PUBLIC,
            PrivacyClass.PERSONAL,
            PrivacyClass.PRIVATE,
        ):
            raise ValueError("privacy class must be public, personal or private")
        return value

    @field_validator("line_fingerprints")
    @classmethod
    def _fingerprints(cls, value: frozenset[str]) -> frozenset[str]:
        if len(value) > MAX_LINE_FINGERPRINTS:
            raise ValueError("too many line fingerprints")
        return value

    def freshness(self, now: datetime) -> Freshness:
        age = now - self.refreshed_at
        if age <= timedelta(days=180):
            return Freshness.FRESH
        if age <= timedelta(days=365):
            return Freshness.AGING
        return Freshness.STALE


class ClaimReview(BaseModel):
    """The owner's recorded decision about one claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(min_length=1, max_length=64)
    state: ClaimReviewState
    reviewed_at: datetime


class Resolution(BaseModel):
    """The owner's choice among conflicting values for one claim attribute."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    attribute: str
    value: str = Field(max_length=MAX_ATTRIBUTE_VALUE_LENGTH)
    resolved_at: datetime


class ConflictOption(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    option_id: str
    value: str
    claim_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()


class Conflict(BaseModel):
    """Conflicting evidence, preserved (never merged). Only the owner resolves
    it; a model can neither resolve nor hide it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflict_id: str
    kind: ConflictKind
    attribute: str
    claim_ids: tuple[str, ...]
    options: tuple[ConflictOption, ...]
    resolved: bool = False
    resolved_value: str | None = None


def new_claim_id(category: ClaimCategory, key: str) -> str:
    return stable_id("c", category.value, key)


def new_source_id(source_type: SourceType, label: str) -> str:
    return stable_id("s", source_type.value, label.lower())


def new_evidence_id(
    claim_id: str,
    source_id: str,
    nature: EvidenceNature,
    location: SourceLocation,
    reference: str,
) -> str:
    return stable_id(
        "e",
        claim_id,
        source_id,
        nature.value,
        str(location.page_number),
        str(location.paragraph_index),
        str(location.character_start),
        reference,
    )


__all__ = [
    "ATTRIBUTE_ALLOWLIST",
    "CATEGORY_SENSITIVITY",
    "DATE_ATTRIBUTES",
    "FORBIDDEN_ATTRIBUTES",
    "MAX_ATTRIBUTES",
    "MAX_CLAIMS_PER_SOURCE",
    "MAX_EVIDENCE_PER_CLAIM",
    "MAX_LABEL_LENGTH",
    "MAX_QUERY_LENGTH",
    "MAX_REFERENCE_LENGTH",
    "MAX_STATEMENT_LENGTH",
    "DOCUMENTARY_NATURES",
    "MAX_LINE_FINGERPRINTS",
    "TRUSTED_NATURES",
    "ClaimCategory",
    "ClaimReview",
    "ClaimReviewState",
    "Conflict",
    "ConflictKind",
    "ConflictOption",
    "EvidenceNature",
    "EvidenceRef",
    "EvidenceStrength",
    "Freshness",
    "MatchStatus",
    "ProfessionalClaim",
    "Resolution",
    "SourceLocation",
    "SourceRecord",
    "SourceType",
    "clean_text",
    "new_claim_id",
    "new_evidence_id",
    "new_source_id",
    "stable_id",
    "utc_now",
]
