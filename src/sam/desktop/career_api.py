"""Career & PhD routes for the desktop UI.

Every route is OWNER-bound (``owner_bridge_runtime``): while Guest Mode is
active it is refused before the request body is used, so a guest can neither
view private opportunities or applications nor draft, answer, approve, submit,
send, change preferences or approve anything. The routes hold no authority:
every operation goes through ``CareerService`` and the PermissionEngine under
``PermissionResource.CAREER``. SUBMIT and SEND always need a confirmation bound
to the exact package or message; this runtime configures no submission adapter
and no e-mail tool, so both fail closed here.

The activity log records THAT something happened, never a CV line, an answer,
a message body or an opportunity description.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from sam.career.models import (
    ApplicationDocument,
    CareerPreferences,
    ContactRole,
    DocumentLine,
    OpportunityType,
    OutreachChannel,
    OutreachKind,
    OwnerContactDetails,
    SourceKind,
    WorkMode,
)
from sam.career.service import OpResult
from sam.career.sources import RawListing
from sam.career.tracking import state as follow_up_state
from sam.desktop.api import _challenge
from sam.desktop.career_models import (
    CareerAlignmentItem,
    CareerApplicationItem,
    CareerClaimItem,
    CareerContactItem,
    CareerContactRequest,
    CareerDocumentItem,
    CareerDraftRequest,
    CareerFitItem,
    CareerFitRequest,
    CareerFitResponse,
    CareerFollowUpItem,
    CareerItemResponse,
    CareerLine,
    CareerOpportunityItem,
    CareerOpportunityRequest,
    CareerOutreachItem,
    CareerOutreachRequest,
    CareerOverviewResponse,
    CareerPreferencesItem,
    CareerPreferencesRequest,
    CareerQuestionItem,
    CareerReviewItem,
    CareerSendRequest,
    CareerSubmitRequest,
    ProRequirementItem,
)
from sam.desktop.models import OperationResult, OperationStatus
from sam.desktop.runtime import DesktopRuntime
from sam.desktop.security import owner_bridge_runtime

router = APIRouter(prefix="/desktop/v1/career", tags=["desktop-career"])
_owner_runtime = Depends(owner_bridge_runtime)
_DEFAULT = "The request couldn't be completed."
_MESSAGES = {
    "permission_denied": "Sam isn't permitted to do that.",
    "confirmation_required": "This needs your confirmation.",
    "confirmation_invalid": (
        "That confirmation is no longer valid for this exact item."
    ),
    "submission_unavailable": (
        "Sam can't submit applications yet. Review the package and apply yourself."
    ),
    "email_unavailable": (
        "Sam can't send e-mail yet. Copy the draft and send it yourself."
    ),
    "drafts_only_channel": "LinkedIn and recruiter messages are drafts only.",
    "not_approved_for_submission": "Approve the application package first.",
    "not_ready_for_approval": "The draft still needs your input.",
    "unresolved_lines": "Some lines need your review before approval.",
    "unanswered_questions": "Some questions still need your answer.",
    "document_not_approved": "Approve every document first.",
    "opportunity_closed": "This opportunity is closed.",
    "deadline_passed": "The deadline has passed.",
    "opportunity_not_recently_verified": "Re-check the listing before submitting.",
    "opportunity_changed": "The listing changed since you approved. Review it again.",
    "no_official_application_url": "There is no official application page for this.",
    "endpoint_domain_mismatch": "The application page isn't the official one.",
    "email_not_in_evidence": "That e-mail address isn't in the quoted source.",
    "name_not_in_evidence": "That name isn't in the quoted source.",
    "organization_not_in_evidence": "That organization isn't in the quoted source.",
    "full_name_required": "A full name is required.",
    "title_or_organization_unknown": "The listing has no clear title or organization.",
    "secret_detected": "That looked like it contained a secret, so it was not saved.",
    "evidence_unavailable": "Your professional evidence couldn't be read.",
    "opportunity_not_found": "That opportunity no longer exists.",
    "draft_not_found": "That application no longer exists.",
    "missing_field": "That request was incomplete.",
    "invalid_request": "That request isn't valid.",
}


def _message(code: str | None) -> str | None:
    if code is None:
        return None
    if code.startswith("url_"):
        return "That link isn't a safe https address."
    return _MESSAGES.get(code, _DEFAULT)


def _operation(runtime: DesktopRuntime, result: OpResult[Any]) -> OperationResult:
    reference = result.operation_id or None
    if result.permission == "confirm_required":
        return OperationResult(
            status="confirmation_required",
            reason_code="confirmation_required",
            message=_message("confirmation_required"),
            reference_id=reference,
            challenge=_challenge(runtime, result.confirmation_id),
        )
    if result.permission == "deny" and not result.ok:
        code = result.reason or "permission_denied"
        return OperationResult(
            status="denied",
            reason_code=code,
            message=_message(code),
            reference_id=reference,
        )
    if result.ok:
        return OperationResult(status="ok", reference_id=reference)
    code = result.reason or "failed"
    status: OperationStatus = "rejected"
    return OperationResult(
        status=status, reason_code=code, message=_message(code), reference_id=reference
    )


def _missing() -> CareerItemResponse:
    return CareerItemResponse(
        status="rejected",
        reason_code="missing_field",
        message=_message("missing_field"),
    )


def _lines(lines: tuple[DocumentLine, ...]) -> list[CareerLine]:
    return [
        CareerLine(
            text=line.text,
            fact=line.fact,
            evidence_count=len(line.evidence),
            flagged=line.flagged,
            flag_reason=line.flag_reason,
        )
        for line in lines
    ]


def _document(doc: ApplicationDocument) -> CareerDocumentItem:
    return CareerDocumentItem(
        document_id=doc.document_id,
        kind=doc.kind.value,
        version=doc.version,
        approved=doc.approved,
        sha256=doc.sha256,
        unresolved=len(doc.unresolved),
        lines=_lines(doc.lines),
    )


@router.get("/overview", response_model=CareerOverviewResponse)
def career_overview(runtime: DesktopRuntime = _owner_runtime) -> CareerOverviewResponse:
    result = runtime.career.overview(runtime.principal)
    base = _operation(runtime, result)
    data = result.data
    if data is None:
        return CareerOverviewResponse(**base.model_dump())
    documents = {d.document_id: d for d in data.documents}
    now = runtime.clock()
    prefs = data.preferences
    return CareerOverviewResponse(
        **base.model_dump(),
        opportunities=[
            CareerOpportunityItem(
                opportunity_id=o.opportunity_id,
                type=o.type.value,
                title=o.title,
                organization=o.organization,
                location=o.location,
                work_mode=o.work_mode.value,
                source=o.canonical_source.value,
                canonical_url=o.canonical_url,
                source_count=len(o.sources),
                deadline=o.deadline.isoformat() if o.deadline else None,
                status=o.status.value,
                compensation=o.compensation,
                funding=o.funding,
                sponsorship=o.sponsorship.value,
                requirements=[
                    ProRequirementItem(text=r.text, preferred=r.preferred)
                    for r in o.requirements
                ],
                research_topics=list(o.research_topics),
                tracked=o.tracked,
                has_official_application_url=o.application_url is not None,
                last_verified=o.last_verified.isoformat(),
            )
            for o in data.opportunities
        ],
        applications=[
            CareerApplicationItem(
                draft_id=d.draft_id,
                opportunity_id=d.opportunity_id,
                state=d.state.value,
                version=d.version,
                documents=[
                    _document(documents[i]) for i in d.document_ids if i in documents
                ],
                questions=[
                    CareerQuestionItem(
                        question_id=q.question_id,
                        text=q.text,
                        kind=q.kind.value,
                        classification=q.classification.value,
                        answered=any(a.question_id == q.question_id for a in d.answers),
                        answer=next(
                            (
                                a.text
                                for a in d.answers
                                if a.question_id == q.question_id
                            ),
                            None,
                        ),
                        answer_source=next(
                            (
                                a.source.value
                                for a in d.answers
                                if a.question_id == q.question_id
                            ),
                            None,
                        ),
                    )
                    for q in d.questions
                ],
                last_failure=d.last_failure,
                submitted_at=d.submitted_at.isoformat() if d.submitted_at else None,
                approved=d.approved_binding is not None,
            )
            for d in data.drafts
        ],
        contacts=[
            CareerContactItem(
                contact_id=c.contact_id,
                name=c.name,
                role=c.role.value,
                organization=c.organization,
                email=c.email,
                source_url=c.evidence[0].url,
                source_kind=c.evidence[0].source_kind.value,
                opportunity_id=c.opportunity_id,
                research_topics=list(c.research_topics),
            )
            for c in data.contacts
        ],
        outreach=[
            CareerOutreachItem(
                outreach_id=o.outreach_id,
                kind=o.kind.value,
                channel=o.channel.value,
                contact_id=o.contact_id,
                opportunity_id=o.opportunity_id,
                subject=o.subject,
                state=o.state.value,
                lines=_lines(o.lines),
                unresolved=len(o.unresolved),
                sendable=o.channel is OutreachChannel.EMAIL,
            )
            for o in data.outreach
        ],
        follow_ups=[
            CareerFollowUpItem(
                follow_up_id=f.follow_up_id,
                opportunity_id=f.opportunity_id,
                contact_id=f.contact_id,
                due_at=f.due_at.isoformat(),
                count=f.count,
                state=follow_up_state(f, now).value,
            )
            for f in data.follow_ups
        ],
        review_queue=[
            CareerReviewItem(
                kind=i.kind,
                label=i.label,
                opportunity_id=i.opportunity_id,
                item_id=i.item_id,
            )
            for i in data.review_queue
        ],
        preferences=CareerPreferencesItem(
            preferred_roles=list(prefs.preferred_roles),
            locations=list(prefs.locations),
            work_modes=[m.value for m in prefs.work_modes],
            role_types=list(prefs.role_types),
            salary_preference=prefs.salary_preference,
            needs_sponsorship=prefs.needs_sponsorship,
            contact_name=prefs.contact.name,
            contact_email=prefs.contact.email,
            contact_phone=prefs.contact.phone,
            portfolio_url=prefs.contact.portfolio_url,
            linkedin_url=prefs.contact.linkedin_url,
        ),
        submission_available=data.submission_available,
        sending_available=data.sending_available,
    )


def _item(
    runtime: DesktopRuntime,
    result: OpResult[Any],
    item_id: str | None = None,
    state: str | None = None,
) -> CareerItemResponse:
    base = _operation(runtime, result)
    return CareerItemResponse(**base.model_dump(), item_id=item_id, state=state)


@router.post("/opportunity", response_model=CareerItemResponse)
def career_opportunity(
    payload: CareerOpportunityRequest, runtime: DesktopRuntime = _owner_runtime
) -> CareerItemResponse:
    service, principal = runtime.career, runtime.principal
    if payload.action == "import":
        if not (
            payload.text
            and payload.url
            and payload.source_kind
            and payload.opportunity_type
        ):
            return _missing()
        result: OpResult[Any] = service.import_listing(
            principal,
            RawListing(
                kind=SourceKind(payload.source_kind),
                opportunity_type=OpportunityType(payload.opportunity_type),
                url=payload.url,
                text=payload.text,
                retrieved_at=runtime.clock(),
                external_id=payload.external_id,
                application_url=payload.application_url,
            ),
        )
        runtime.activity.add(
            "career", "Opportunity import", "ok" if result.ok else "refused"
        )
        data = result.data
        return _item(runtime, result, data.opportunity_id if data else None)
    if payload.action == "track" and payload.opportunity_id:
        result = service.track(principal, payload.opportunity_id)
    elif payload.action == "review" and payload.draft_id:
        result = service.mark_reviewed(principal, payload.draft_id)
    else:
        return _missing()
    runtime.activity.add(
        "career", f"Opportunity {payload.action}", "ok" if result.ok else "refused"
    )
    data = result.data
    return _item(
        runtime,
        result,
        data.draft_id if data else None,
        data.state.value if data else None,
    )


@router.post("/fit", response_model=CareerFitResponse)
def career_fit(
    payload: CareerFitRequest, runtime: DesktopRuntime = _owner_runtime
) -> CareerFitResponse:
    service, principal = runtime.career, runtime.principal
    fit = service.fit(principal, payload.opportunity_id)
    base = _operation(runtime, fit)
    if fit.data is None:
        return CareerFitResponse(**base.model_dump())
    alignment = service.alignment(principal, payload.opportunity_id, payload.contact_id)

    def claims(items: Any) -> list[CareerClaimItem]:
        return [
            CareerClaimItem(
                claim_id=c.claim_id,
                category=c.category,
                statement=c.statement,
                evidence_count=len(c.evidence_ids),
            )
            for c in items
        ]

    report = alignment.data
    return CareerFitResponse(
        **base.model_dump(),
        requirements=[
            CareerFitItem(
                requirement=r.requirement,
                preferred=r.preferred,
                status=r.status.value,
                claims=claims(r.claims),
                unsupported=list(r.unsupported),
            )
            for r in fit.data.requirements
        ],
        alignment_status=report.status.value if report and report.topics else None,
        alignment=[
            CareerAlignmentItem(
                topic=t.topic, status=t.status.value, claims=claims(t.claims)
            )
            for t in (report.topics if report else ())
        ],
    )


@router.post("/draft", response_model=CareerItemResponse)
def career_draft(
    payload: CareerDraftRequest, runtime: DesktopRuntime = _owner_runtime
) -> CareerItemResponse:
    service, principal = runtime.career, runtime.principal
    action = payload.action
    result: OpResult[Any]
    if action == "create" and payload.opportunity_id:
        result = service.create_draft(
            principal,
            payload.opportunity_id,
            questions=payload.questions,
            motivation=payload.motivation,
            direction=payload.direction,
        )
    elif (
        action == "answer"
        and payload.draft_id
        and payload.question_id
        and payload.text is not None
    ):
        result = service.answer_question(
            principal, payload.draft_id, payload.question_id, payload.text
        )
    elif action == "edit_document" and payload.document_id:
        result = service.edit_document(principal, payload.document_id, payload.lines)
    elif action == "approve_document" and payload.document_id:
        result = service.approve_document(principal, payload.document_id)
    elif action == "approve_submission" and payload.draft_id:
        result = service.approve_for_submission(principal, payload.draft_id)
    elif action == "withdraw" and payload.draft_id:
        result = service.withdraw(principal, payload.draft_id, payload.confirmation_id)
    else:
        return _missing()
    runtime.activity.add(
        "career",
        f"Application {action.replace('_', ' ')}",
        "ok" if result.ok else "refused",
    )
    data = result.data
    item_id = getattr(data, "draft_id", None) or getattr(data, "document_id", None)
    state = getattr(getattr(data, "state", None), "value", None)
    return _item(runtime, result, item_id, state)


@router.post("/submit", response_model=CareerItemResponse)
def career_submit(
    payload: CareerSubmitRequest, runtime: DesktopRuntime = _owner_runtime
) -> CareerItemResponse:
    result = runtime.career.submit(
        runtime.principal, payload.draft_id, payload.confirmation_id
    )
    runtime.activity.add(
        "career",
        "Application submit",
        "submitted" if result.ok else (result.reason or "refused"),
    )
    data = result.data
    return _item(runtime, result, payload.draft_id, data.state.value if data else None)


@router.post("/contact", response_model=CareerItemResponse)
def career_contact(
    payload: CareerContactRequest, runtime: DesktopRuntime = _owner_runtime
) -> CareerItemResponse:
    result = runtime.career.add_contact(
        runtime.principal,
        name=payload.name,
        role=ContactRole(payload.role),
        organization=payload.organization,
        source_kind=SourceKind(payload.source_kind),
        url=payload.url,
        quote=payload.quote,
        email=payload.email,
        opportunity_id=payload.opportunity_id,
        research_topics=payload.research_topics,
    )
    runtime.activity.add("career", "Contact added", "ok" if result.ok else "refused")
    return _item(runtime, result, result.data.contact_id if result.data else None)


@router.post("/outreach", response_model=CareerItemResponse)
def career_outreach(
    payload: CareerOutreachRequest, runtime: DesktopRuntime = _owner_runtime
) -> CareerItemResponse:
    service, principal = runtime.career, runtime.principal
    result: OpResult[Any]
    if (
        payload.action == "create"
        and payload.contact_id
        and payload.kind
        and payload.channel
    ):
        result = service.draft_outreach(
            principal,
            contact_id=payload.contact_id,
            kind=OutreachKind(payload.kind),
            channel=OutreachChannel(payload.channel),
            opportunity_id=payload.opportunity_id,
            note=payload.note,
        )
    elif payload.action == "approve" and payload.outreach_id:
        result = service.approve_outreach(principal, payload.outreach_id)
    elif payload.action == "follow_up" and payload.follow_up_id:
        result = service.draft_follow_up(principal, payload.follow_up_id)
    else:
        return _missing()
    runtime.activity.add(
        "career",
        f"Outreach {payload.action.replace('_', ' ')}",
        "ok" if result.ok else "refused",
    )
    data = result.data
    return _item(
        runtime,
        result,
        data.outreach_id if data else None,
        data.state.value if data else None,
    )


@router.post("/send", response_model=CareerItemResponse)
def career_send(
    payload: CareerSendRequest, runtime: DesktopRuntime = _owner_runtime
) -> CareerItemResponse:
    result = runtime.career.send_outreach(
        runtime.principal,
        payload.outreach_id,
        payload.confirmation_id,
        payload.email_confirmation_id,
    )
    runtime.activity.add(
        "career", "Outreach send", "sent" if result.ok else (result.reason or "refused")
    )
    data = result.data
    return _item(
        runtime, result, payload.outreach_id, data.state.value if data else None
    )


@router.post("/preferences", response_model=CareerItemResponse)
def career_preferences(
    payload: CareerPreferencesRequest, runtime: DesktopRuntime = _owner_runtime
) -> CareerItemResponse:
    try:
        prefs = CareerPreferences(
            preferred_roles=tuple(payload.preferred_roles),
            locations=tuple(payload.locations),
            work_modes=tuple(WorkMode(m) for m in payload.work_modes),
            role_types=tuple(payload.role_types),
            salary_preference=payload.salary_preference,
            needs_sponsorship=payload.needs_sponsorship,
            contact=OwnerContactDetails(
                name=payload.contact_name,
                email=payload.contact_email,
                phone=payload.contact_phone,
                portfolio_url=payload.portfolio_url,
                linkedin_url=payload.linkedin_url,
            ),
        )
    except ValueError:
        return CareerItemResponse(
            status="rejected",
            reason_code="invalid_request",
            message=_message("invalid_request"),
        )
    result = runtime.career.set_preferences(runtime.principal, prefs)
    runtime.activity.add(
        "career", "Career preferences", "ok" if result.ok else "refused"
    )
    return _item(runtime, result)


__all__ = ["router"]
