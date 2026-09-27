"""Closed vocabulary and immutable records for the Career & PhD Agent.

Rules encoded here:

* Unknown stays unknown. Salary, funding, deadline, remote status, sponsorship,
  hiring manager and supervisor interest are ``None`` unless a source states
  them explicitly; nothing is ever inferred or invented.
* Opportunity text is untrusted DATA. It is stored bounded and never read as an
  instruction.
* No record carries authority: there is no field for a permission, a
  confirmation, a filesystem path, a command or a credential, and every model
  refuses unknown fields.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sam.models.models import PrivacyClass

_ID = r"^[a-z0-9_-]{1,64}$"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_TITLE = 200
MAX_DESCRIPTION = 20_000
MAX_REQUIREMENT = 300
MAX_REQUIREMENTS = 40
MAX_DOCUMENT = 20_000
MAX_ANSWER = 2_000


def clean_text(value: str, limit: int) -> str:
    return _CONTROL.sub("", value).strip()[:limit]


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ------------------------------------------------------------- vocabulary


class OpportunityType(StrEnum):
    JOB = "job"
    PHD = "phd"


class WorkMode(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class SourceKind(StrEnum):
    """Where a listing came from, in trust order per opportunity type."""

    OFFICIAL_CAREER_PAGE = "official_career_page"
    OFFICIAL_ATS = "official_ats"
    LINKEDIN = "linkedin"
    INDEED = "indeed"
    GLASSDOOR = "glassdoor"
    UNIVERSITY_PAGE = "university_page"
    FUNDING_PAGE = "funding_page"
    SUPERVISOR_PAGE = "supervisor_page"
    ACADEMIC_SOURCE = "academic_source"
    OTHER = "other"


class OpportunityStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class Sponsorship(StrEnum):
    """Only what a source states. Never inferred."""

    AVAILABLE = "available"
    NOT_AVAILABLE = "not_available"
    UNKNOWN = "unknown"


class FitStatus(StrEnum):
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    NOT_SUPPORTED = "not_supported"
    UNKNOWN = "unknown"


class AlignmentStatus(StrEnum):
    SUPPORTED_ALIGNMENT = "supported_alignment"
    PARTIAL_ALIGNMENT = "partial_alignment"
    NO_EVIDENCE = "no_evidence"
    UNKNOWN = "unknown"


class ApplicationState(StrEnum):
    DISCOVERED = "discovered"
    REVIEWED = "reviewed"
    DRAFTING = "drafting"
    READY_FOR_OWNER_REVIEW = "ready_for_owner_review"
    APPROVED_FOR_SUBMISSION = "approved_for_submission"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    NEEDS_OWNER_INPUT = "needs_owner_input"
    BLOCKED = "blocked"
    SUBMISSION_FAILED = "submission_failed"
    # The adapter could not prove whether the external action happened (a
    # timeout or lost response after dispatch). Never retried automatically.
    OUTCOME_UNKNOWN = "outcome_unknown"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"


class DocumentKind(StrEnum):
    CV = "cv"
    COVER_LETTER = "cover_letter"
    RESEARCH_STATEMENT = "research_statement"
    PROPOSAL_OUTLINE = "proposal_outline"


class QuestionKind(StrEnum):
    NAME = "name"
    EMAIL = "email"
    PHONE = "phone"
    PORTFOLIO = "portfolio"
    LINKEDIN_PROFILE = "linkedin_profile"
    TECHNICAL_EXPERIENCE = "technical_experience"
    MOTIVATION = "motivation"
    SALARY = "salary"
    NOTICE_PERIOD = "notice_period"
    RELOCATION = "relocation"
    WORK_AUTHORIZATION = "work_authorization"
    SPONSORSHIP = "sponsorship"
    DISABILITY = "disability"
    DEMOGRAPHIC = "demographic"
    CRIMINAL_HISTORY = "criminal_history"
    LEGAL_ATTESTATION = "legal_attestation"
    CONFLICT_NONCOMPETE = "conflict_noncompete"
    REFERENCES = "references"
    CONSENT_PRIVACY = "consent_privacy"
    AVAILABILITY = "availability"
    OTHER = "other"


class QuestionClass(StrEnum):
    SAFE = "safe"
    EVIDENCE_BACKED = "evidence_backed"
    OWNER_REVIEW_REQUIRED = "owner_review_required"


class AnswerSource(StrEnum):
    OWNER = "owner"  # typed by the owner for THIS draft
    OWNER_PROFILE = "owner_profile"  # owner-approved contact details
    EVIDENCE = "evidence"  # built from Phase 14 evidence


class ContactRole(StrEnum):
    RECRUITER = "recruiter"
    HIRING_MANAGER = "hiring_manager"
    TEAM_LEAD = "team_lead"
    PROFESSOR = "professor"
    SUPERVISOR = "supervisor"
    RESEARCH_GROUP = "research_group"


class OutreachChannel(StrEnum):
    EMAIL = "email"
    RECRUITER_MESSAGE = "recruiter_message"
    LINKEDIN_CONNECTION_NOTE = "linkedin_connection_note"
    LINKEDIN_MESSAGE = "linkedin_message"


class OutreachKind(StrEnum):
    RECRUITER = "recruiter"
    HIRING_MANAGER = "hiring_manager"
    SUPERVISOR = "supervisor"
    PHD_INQUIRY = "phd_inquiry"
    FOLLOW_UP = "follow_up"


class OutreachState(StrEnum):
    DRAFT = "draft"
    READY_FOR_OWNER_REVIEW = "ready_for_owner_review"
    APPROVED = "approved"
    SENDING = "sending"
    SENT = "sent"
    SEND_FAILED = "send_failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


# ------------------------------------------------------------------ records


class SourceRecord(_Frozen):
    """One place a listing was seen. Provenance, never authority."""

    kind: SourceKind
    url: str = Field(max_length=2_000)
    domain: str = Field(max_length=253)
    external_id: str | None = Field(default=None, max_length=200)
    retrieved_at: datetime
    content_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")


class Requirement(_Frozen):
    text: str = Field(min_length=1, max_length=MAX_REQUIREMENT)
    preferred: bool = False


class CareerOpportunity(_Frozen):
    opportunity_id: str = Field(pattern=_ID)
    type: OpportunityType
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    organization: str = Field(min_length=1, max_length=MAX_TITLE)
    location: str | None = Field(default=None, max_length=MAX_TITLE)
    work_mode: WorkMode = WorkMode.UNKNOWN
    canonical_source: SourceKind
    canonical_url: str = Field(max_length=2_000)
    canonical_domain: str = Field(max_length=253)
    application_url: str | None = Field(default=None, max_length=2_000)
    external_reference_id: str | None = Field(default=None, max_length=200)
    first_seen: datetime
    last_seen: datetime
    last_verified: datetime
    posted_date: date | None = None
    deadline: date | None = None
    description: str = Field(default="", max_length=MAX_DESCRIPTION)
    requirements: tuple[Requirement, ...] = Field(
        default=(), max_length=MAX_REQUIREMENTS
    )
    compensation: str | None = Field(default=None, max_length=300)  # explicit only
    funding: str | None = Field(default=None, max_length=500)  # explicit only (PhD)
    sponsorship: Sponsorship = Sponsorship.UNKNOWN
    research_topics: tuple[str, ...] = Field(default=(), max_length=20)
    status: OpportunityStatus = OpportunityStatus.UNKNOWN
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: int = Field(default=1, ge=1)
    sources: tuple[SourceRecord, ...] = Field(min_length=1)
    privacy_class: PrivacyClass = PrivacyClass.PERSONAL
    tracked: bool = False

    @model_validator(mode="after")
    def _types(self) -> Self:
        if self.type is OpportunityType.JOB and self.funding is not None:
            raise ValueError("funding is a PhD attribute")
        return self


class EvidenceLink(_Frozen):
    """The Phase 14 claim and evidence records behind one factual line."""

    claim_id: str = Field(max_length=128)
    evidence_ids: tuple[str, ...] = ()


class DocumentLine(_Frozen):
    """One line of a generated document. A FACT line always carries evidence;
    a MOTIVATION line carries none and may not state owner facts."""

    text: str = Field(max_length=2_000)
    fact: bool
    evidence: tuple[EvidenceLink, ...] = ()
    flagged: bool = False
    flag_reason: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")

    @model_validator(mode="after")
    def _facts_have_evidence(self) -> Self:
        if self.fact and not self.evidence and not self.flagged:
            raise ValueError("a factual line needs evidence or a review flag")
        return self


class ApplicationDocument(_Frozen):
    """A document Sam generated (or the owner edited) for ONE opportunity. It is
    the only thing that can ever be part of a submission package: there is no
    path, only an id, a version and a content hash."""

    document_id: str = Field(pattern=_ID)
    opportunity_id: str = Field(pattern=_ID)
    kind: DocumentKind
    version: int = Field(ge=1)
    lines: tuple[DocumentLine, ...]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: bool = False
    created_at: datetime

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def unresolved(self) -> tuple[DocumentLine, ...]:
        return tuple(line for line in self.lines if line.flagged)


class ApplicationQuestion(_Frozen):
    question_id: str = Field(pattern=_ID)
    text: str = Field(min_length=1, max_length=500)
    kind: QuestionKind
    classification: QuestionClass
    required: bool = True


class DraftAnswer(_Frozen):
    question_id: str = Field(pattern=_ID)
    text: str = Field(max_length=MAX_ANSWER)
    source: AnswerSource
    evidence: tuple[EvidenceLink, ...] = ()
    sensitive: bool = False


class ApplicationDraft(_Frozen):
    draft_id: str = Field(pattern=_ID)
    opportunity_id: str = Field(pattern=_ID)
    owner_id: str = Field(min_length=1, max_length=200)
    version: int = Field(default=1, ge=1)
    state: ApplicationState = ApplicationState.DRAFTING
    cv_document_id: str | None = None
    letter_document_id: str | None = None
    extra_document_ids: tuple[str, ...] = ()
    questions: tuple[ApplicationQuestion, ...] = ()
    answers: tuple[DraftAnswer, ...] = ()
    opportunity_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_binding: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    submitted_at: datetime | None = None
    submission_reference: str | None = Field(default=None, max_length=200)
    last_failure: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")
    attempt_id: str | None = Field(default=None, pattern=_ID)
    created_at: datetime
    updated_at: datetime

    @property
    def document_ids(self) -> tuple[str, ...]:
        ids = [self.cv_document_id, self.letter_document_id, *self.extra_document_ids]
        return tuple(i for i in ids if i)


class ContactEvidence(_Frozen):
    source_kind: SourceKind
    url: str = Field(max_length=2_000)
    quote: str = Field(max_length=500)  # the exact text that names the person
    retrieved_at: datetime


class Contact(_Frozen):
    contact_id: str = Field(pattern=_ID)
    name: str = Field(min_length=1, max_length=200)
    role: ContactRole
    organization: str = Field(min_length=1, max_length=MAX_TITLE)
    email: str | None = Field(default=None, max_length=254)  # only if stated
    opportunity_id: str | None = Field(default=None, pattern=_ID)
    research_topics: tuple[str, ...] = Field(default=(), max_length=20)
    evidence: tuple[ContactEvidence, ...] = Field(min_length=1)


class OutreachDraft(_Frozen):
    outreach_id: str = Field(pattern=_ID)
    kind: OutreachKind
    channel: OutreachChannel
    contact_id: str = Field(pattern=_ID)
    opportunity_id: str | None = Field(default=None, pattern=_ID)
    subject: str = Field(default="", max_length=200)
    lines: tuple[DocumentLine, ...]
    version: int = Field(default=1, ge=1)
    state: OutreachState = OutreachState.DRAFT
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sent_at: datetime | None = None
    attempt_id: str | None = Field(default=None, pattern=_ID)
    last_failure: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")
    created_at: datetime

    @property
    def body(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def unresolved(self) -> tuple[DocumentLine, ...]:
        return tuple(line for line in self.lines if line.flagged)


class FollowUp(_Frozen):
    follow_up_id: str = Field(pattern=_ID)
    opportunity_id: str = Field(pattern=_ID)
    contact_id: str | None = Field(default=None, pattern=_ID)
    last_contact_at: datetime
    due_at: datetime
    count: int = Field(default=0, ge=0)
    last_draft_at: datetime | None = None


class OwnerContactDetails(_Frozen):
    """Owner-approved contact data for SAFE deterministic form fields."""

    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    portfolio_url: str | None = Field(default=None, max_length=500)
    linkedin_url: str | None = Field(default=None, max_length=500)


class CareerPreferences(_Frozen):
    """Explicit, owner-approved preferences. Never inferred from applications.
    Salary and sponsorship preferences are PRIVATE by classification."""

    preferred_roles: tuple[str, ...] = Field(default=(), max_length=20)
    locations: tuple[str, ...] = Field(default=(), max_length=20)
    work_modes: tuple[WorkMode, ...] = ()
    role_types: tuple[str, ...] = Field(default=(), max_length=20)
    salary_preference: str | None = Field(default=None, max_length=100)
    needs_sponsorship: bool | None = None
    contact: OwnerContactDetails = OwnerContactDetails()

    @field_validator("preferred_roles", "locations", "role_types")
    @classmethod
    def _bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(clean_text(v, 100) for v in value)
        return tuple(v for v in cleaned if v)

    @property
    def privacy_class(self) -> PrivacyClass:
        sensitive = (
            self.salary_preference is not None or self.needs_sponsorship is not None
        )
        return PrivacyClass.PRIVATE if sensitive else PrivacyClass.PERSONAL


__all__ = [
    "AlignmentStatus",
    "AnswerSource",
    "ApplicationDocument",
    "ApplicationDraft",
    "ApplicationQuestion",
    "ApplicationState",
    "CareerOpportunity",
    "CareerPreferences",
    "Contact",
    "ContactEvidence",
    "ContactRole",
    "DocumentKind",
    "DocumentLine",
    "DraftAnswer",
    "EvidenceLink",
    "FitStatus",
    "FollowUp",
    "OpportunityStatus",
    "OpportunityType",
    "OutreachChannel",
    "OutreachDraft",
    "OutreachKind",
    "OutreachState",
    "OwnerContactDetails",
    "QuestionClass",
    "QuestionKind",
    "Requirement",
    "SourceKind",
    "SourceRecord",
    "Sponsorship",
    "WorkMode",
    "clean_text",
    "sha256",
]
