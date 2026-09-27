"""Typed bridge models for the Career & PhD UI.

Requests are strict (unknown fields are refused) and carry no principal, no
permission, no filesystem path, no application state, no provider and no
command: the owner can paste a listing, answer questions, edit and approve
drafts, and ask for a CONFIRMED submit/send. Responses are display data.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sam.desktop.models import OperationResult

_ID = r"^[a-z0-9_-]{1,80}$"
SourceKindLiteral = Literal[
    "official_career_page",
    "official_ats",
    "linkedin",
    "indeed",
    "glassdoor",
    "university_page",
    "funding_page",
    "supervisor_page",
    "academic_source",
    "other",
]
RoleLiteral = Literal[
    "recruiter",
    "hiring_manager",
    "team_lead",
    "professor",
    "supervisor",
    "research_group",
]
ChannelLiteral = Literal[
    "email", "recruiter_message", "linkedin_connection_note", "linkedin_message"
]
OutreachKindLiteral = Literal[
    "recruiter", "hiring_manager", "supervisor", "phd_inquiry", "follow_up"
]
WorkModeLiteral = Literal["onsite", "hybrid", "remote", "unknown"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------- requests


class CareerOpportunityRequest(_Strict):
    action: Literal["import", "track", "review"]
    opportunity_id: str | None = Field(default=None, pattern=_ID)
    draft_id: str | None = Field(default=None, pattern=_ID)
    text: str | None = Field(default=None, max_length=40_000)
    url: str | None = Field(default=None, max_length=2_000)
    source_kind: SourceKindLiteral | None = None
    opportunity_type: Literal["job", "phd"] | None = None
    application_url: str | None = Field(default=None, max_length=2_000)
    external_id: str | None = Field(default=None, max_length=200)


class CareerFitRequest(_Strict):
    opportunity_id: str = Field(pattern=_ID)
    contact_id: str | None = Field(default=None, pattern=_ID)


class CareerDraftRequest(_Strict):
    action: Literal[
        "create",
        "answer",
        "edit_document",
        "approve_document",
        "approve_submission",
        "withdraw",
    ]
    opportunity_id: str | None = Field(default=None, pattern=_ID)
    draft_id: str | None = Field(default=None, pattern=_ID)
    document_id: str | None = Field(default=None, pattern=_ID)
    question_id: str | None = Field(default=None, pattern=_ID)
    text: str | None = Field(default=None, max_length=2_000)
    questions: list[str] = Field(default_factory=list, max_length=40)
    lines: list[str] = Field(default_factory=list, max_length=400)
    motivation: str | None = Field(default=None, max_length=2_000)
    direction: str | None = Field(default=None, max_length=2_000)
    confirmation_id: str | None = Field(default=None, max_length=100)


class CareerSubmitRequest(_Strict):
    draft_id: str = Field(pattern=_ID)
    confirmation_id: str | None = Field(default=None, max_length=100)


class CareerContactRequest(_Strict):
    name: str = Field(min_length=1, max_length=200)
    role: RoleLiteral
    organization: str = Field(min_length=1, max_length=200)
    source_kind: SourceKindLiteral
    url: str = Field(min_length=1, max_length=2_000)
    quote: str = Field(min_length=1, max_length=500)
    email: str | None = Field(default=None, max_length=254)
    opportunity_id: str | None = Field(default=None, pattern=_ID)
    research_topics: list[str] = Field(default_factory=list, max_length=20)


class CareerOutreachRequest(_Strict):
    action: Literal["create", "approve", "follow_up"]
    outreach_id: str | None = Field(default=None, pattern=_ID)
    contact_id: str | None = Field(default=None, pattern=_ID)
    follow_up_id: str | None = Field(default=None, pattern=_ID)
    kind: OutreachKindLiteral | None = None
    channel: ChannelLiteral | None = None
    opportunity_id: str | None = Field(default=None, pattern=_ID)
    note: str | None = Field(default=None, max_length=2_000)


class CareerSendRequest(_Strict):
    outreach_id: str = Field(pattern=_ID)
    confirmation_id: str | None = Field(default=None, max_length=100)
    email_confirmation_id: str | None = Field(default=None, max_length=100)


class CareerPreferencesRequest(_Strict):
    preferred_roles: list[str] = Field(default_factory=list, max_length=20)
    locations: list[str] = Field(default_factory=list, max_length=20)
    work_modes: list[WorkModeLiteral] = Field(default_factory=list, max_length=4)
    role_types: list[str] = Field(default_factory=list, max_length=20)
    salary_preference: str | None = Field(default=None, max_length=100)
    needs_sponsorship: bool | None = None
    contact_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = Field(default=None, max_length=254)
    contact_phone: str | None = Field(default=None, max_length=40)
    portfolio_url: str | None = Field(default=None, max_length=500)
    linkedin_url: str | None = Field(default=None, max_length=500)


# -------------------------------------------------------------- responses


class ProRequirementItem(BaseModel):
    text: str
    preferred: bool


class CareerOpportunityItem(BaseModel):
    opportunity_id: str
    type: Literal["job", "phd"]
    title: str
    organization: str
    location: str | None = None
    work_mode: WorkModeLiteral
    source: str
    canonical_url: str
    source_count: int
    deadline: str | None = None
    status: str
    compensation: str | None = None
    funding: str | None = None
    sponsorship: str
    requirements: list[ProRequirementItem] = Field(default_factory=list)
    research_topics: list[str] = Field(default_factory=list)
    tracked: bool
    has_official_application_url: bool
    last_verified: str


class CareerLine(BaseModel):
    text: str
    fact: bool
    evidence_count: int
    flagged: bool
    flag_reason: str | None = None


class CareerDocumentItem(BaseModel):
    document_id: str
    kind: str
    version: int
    approved: bool
    sha256: str
    unresolved: int
    lines: list[CareerLine] = Field(default_factory=list)


class CareerQuestionItem(BaseModel):
    question_id: str
    text: str
    kind: str
    classification: str
    answered: bool
    answer: str | None = None
    answer_source: str | None = None


class CareerApplicationItem(BaseModel):
    draft_id: str
    opportunity_id: str
    state: str
    version: int
    documents: list[CareerDocumentItem] = Field(default_factory=list)
    questions: list[CareerQuestionItem] = Field(default_factory=list)
    last_failure: str | None = None
    submitted_at: str | None = None
    approved: bool


class CareerContactItem(BaseModel):
    contact_id: str
    name: str
    role: str
    organization: str
    email: str | None = None
    source_url: str
    source_kind: str
    opportunity_id: str | None = None
    research_topics: list[str] = Field(default_factory=list)


class CareerOutreachItem(BaseModel):
    outreach_id: str
    kind: str
    channel: str
    contact_id: str
    opportunity_id: str | None = None
    subject: str
    state: str
    lines: list[CareerLine] = Field(default_factory=list)
    unresolved: int
    sendable: bool


class CareerFollowUpItem(BaseModel):
    follow_up_id: str
    opportunity_id: str
    contact_id: str | None = None
    due_at: str
    count: int
    state: str


class CareerReviewItem(BaseModel):
    kind: str
    label: str
    opportunity_id: str | None = None
    item_id: str | None = None


class CareerPreferencesItem(BaseModel):
    preferred_roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    work_modes: list[str] = Field(default_factory=list)
    role_types: list[str] = Field(default_factory=list)
    salary_preference: str | None = None
    needs_sponsorship: bool | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    portfolio_url: str | None = None
    linkedin_url: str | None = None


class CareerOverviewResponse(OperationResult):
    opportunities: list[CareerOpportunityItem] = Field(default_factory=list)
    applications: list[CareerApplicationItem] = Field(default_factory=list)
    contacts: list[CareerContactItem] = Field(default_factory=list)
    outreach: list[CareerOutreachItem] = Field(default_factory=list)
    follow_ups: list[CareerFollowUpItem] = Field(default_factory=list)
    review_queue: list[CareerReviewItem] = Field(default_factory=list)
    preferences: CareerPreferencesItem | None = None
    submission_available: bool = False
    sending_available: bool = False


class CareerClaimItem(BaseModel):
    claim_id: str
    category: str
    statement: str
    evidence_count: int


class CareerFitItem(BaseModel):
    requirement: str
    preferred: bool
    status: str
    claims: list[CareerClaimItem] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)


class CareerAlignmentItem(BaseModel):
    topic: str
    status: str
    claims: list[CareerClaimItem] = Field(default_factory=list)


class CareerFitResponse(OperationResult):
    requirements: list[CareerFitItem] = Field(default_factory=list)
    alignment_status: str | None = None
    alignment: list[CareerAlignmentItem] = Field(default_factory=list)


class CareerItemResponse(OperationResult):
    item_id: str | None = None
    state: str | None = None


__all__ = [
    "CareerAlignmentItem",
    "CareerApplicationItem",
    "CareerClaimItem",
    "CareerContactItem",
    "CareerContactRequest",
    "CareerDocumentItem",
    "CareerDraftRequest",
    "CareerFitItem",
    "CareerFitRequest",
    "CareerFitResponse",
    "CareerFollowUpItem",
    "CareerItemResponse",
    "CareerLine",
    "CareerOpportunityItem",
    "CareerOpportunityRequest",
    "CareerOutreachItem",
    "CareerOutreachRequest",
    "CareerOverviewResponse",
    "CareerPreferencesItem",
    "CareerPreferencesRequest",
    "CareerQuestionItem",
    "CareerReviewItem",
    "CareerSendRequest",
    "CareerSubmitRequest",
    "ProRequirementItem",
]
