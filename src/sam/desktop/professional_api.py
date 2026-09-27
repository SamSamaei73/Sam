"""Professional Intelligence routes for the desktop UI.

Every route is OWNER-bound: ``owner_bridge_runtime`` refuses it while Guest Mode
is active, before the request body is processed, before any file is decoded and
before any provider is called. The routes hold no authority of their own: every
operation goes through ``ProfessionalService`` and the PermissionEngine under
``PermissionResource.PROFESSIONAL`` (removal always asks for a confirmation).

Requests accept no principal, no verification state, no path and no endpoint. The
activity log records THAT something happened, never a file name or a fact.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Depends

from sam.desktop.api import _challenge, _decode
from sam.desktop.models import OperationResult, OperationStatus
from sam.desktop.professional_models import (
    ProClaim,
    ProConflict,
    ProConflictOption,
    ProEducation,
    ProEvidence,
    ProExperience,
    ProfessionalIngestRequest,
    ProfessionalIngestResponse,
    ProfessionalProfileResponse,
    ProfessionalQueryRequest,
    ProfessionalQueryResponse,
    ProfessionalRemoveRequest,
    ProfessionalReviewRequest,
    ProGap,
    ProPublication,
    ProRequirement,
    ProSource,
    ProTimelineEntry,
)
from sam.desktop.runtime import DesktopRuntime
from sam.desktop.security import owner_bridge_runtime
from sam.models.models import PrivacyClass
from sam.professional.matching import RequirementEvidence
from sam.professional.models import Conflict
from sam.professional.profile import ClaimView, EvidenceView
from sam.professional.views import (
    EducationView,
    OpResult,
    PublicationView,
    SourceView,
)

router = APIRouter(prefix="/desktop/v1/professional", tags=["desktop-professional"])
_owner_runtime = Depends(owner_bridge_runtime)

_MESSAGES = {
    "permission_denied": "Sam isn't permitted to do that.",
    "confirmation_required": "This needs your confirmation.",
    "confirmation_invalid": "That confirmation is no longer valid.",
    "secret_detected": "That looked like it contained a secret, so it was withheld.",
    "secret_not_ingestible": "Secret material is never added to your profile.",
    "unsupported_type": "That file type isn't supported.",
    "unsupported_source_type": "That source type isn't supported.",
    "parsing_error": "That file couldn't be read.",
    "no_text": "No readable text was found in that file.",
    "too_large": "That file is too large.",
    "invalid_source": "That file name isn't allowed.",
    "invalid_encoding": "The file data was malformed.",
    "duplicate_source": "That content is already in your profile.",
    "privacy_class_invalid": "Choose public, personal or private.",
    "invalid_extraction": "Nothing could be safely extracted from that file.",
    "too_many_claims": "That file produced too many items to add safely.",
    "no_title": "The publication has no readable title.",
    "claim_not_found": "That item no longer exists.",
    "unknown_skill": (
        "That skill isn't in Sam's trusted vocabulary, so it can't be confirmed."
    ),
    "no_evidence": "That item has no source to confirm against.",
    "conflict_not_found": "That conflict no longer exists.",
    "option_not_found": "That choice isn't one of the conflicting values.",
    "source_not_found": "That source no longer exists.",
    "query_invalid": "That search text can't be used.",
    "requirement_invalid": "That requirement can't be used.",
    "missing_field": "That request was incomplete.",
    "ingestion_failed": "That source couldn't be added.",
    "internal_error": "Sam couldn't complete that request.",
}
_FAILED = frozenset({"internal_error", "ingestion_failed", "read_error"})
_DEFAULT = "The request couldn't be completed."
_PRIVACY = {
    "public": PrivacyClass.PUBLIC,
    "personal": PrivacyClass.PERSONAL,
    "private": PrivacyClass.PRIVATE,
}
_MAX_EVIDENCE = 12


def _message(code: str | None) -> str | None:
    return None if code is None else _MESSAGES.get(code, _DEFAULT)


def _operation(runtime: DesktopRuntime, result: OpResult[Any]) -> OperationResult:
    """One translation of the engine's outcome into the UI's five states."""

    reference = result.operation_id or None
    if result.permission == "confirm_required":
        return OperationResult(
            status="confirmation_required",
            reason_code="confirmation_required",
            message=_message("confirmation_required"),
            reference_id=reference,
            challenge=_challenge(runtime, result.confirmation_id),
        )
    if result.permission == "deny":
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
    status: OperationStatus = "failed" if code in _FAILED else "rejected"
    return OperationResult(
        status=status,
        reason_code=code,
        message=_message(code),
        reference_id=reference,
    )


def _evidence(view: EvidenceView) -> ProEvidence:
    return ProEvidence(
        evidence_id=view.evidence_id,
        source_id=view.source_id,
        source_label=view.source_label,
        source_type=view.source_type.value,
        source_family_id=view.source_family_id,
        nature=view.nature.value,
        page_number=view.location.page_number,
        section_title=view.location.section_title,
        paragraph_index=view.location.paragraph_index,
        character_start=view.location.character_start,
        character_end=view.location.character_end,
        reference=view.reference,
        context_claim_id=view.context_claim_id,
        context_statement=view.context_statement,
        basis_evidence_id=view.basis_evidence_id,
    )


def _sensitivity(value: PrivacyClass) -> Any:
    return value.name.lower()


def _claim(view: ClaimView) -> ProClaim:
    return ProClaim(
        claim_id=view.claim_id,
        category=view.category.value,
        statement=view.statement,
        strength=view.strength.value,
        review=view.review.value,
        accepted=view.accepted,
        inferred=view.inferred,
        candidate=view.candidate,
        sensitivity=_sensitivity(view.sensitivity),
        attributes=dict(view.attributes),
        conflicted_attributes=list(view.conflicted_attributes),
        source_count=view.source_count,
        source_family_count=view.source_family_count,
        canonical=view.canonical,
        evidence=[_evidence(e) for e in view.evidence[:_MAX_EVIDENCE]],
    )


def _claims(views: Sequence[ClaimView]) -> list[ProClaim]:
    return [_claim(v) for v in views]


def _publication(view: PublicationView) -> ProPublication:
    return ProPublication(
        claim_id=view.claim_id,
        strength=view.strength.value,
        review=view.review.value,
        accepted=view.accepted,
        sensitivity=_sensitivity(view.sensitivity),
        title=view.title,
        authors=list(view.authors),
        owner_author_position=view.owner_author_position,
        venue=view.venue,
        year=view.year,
        doi=view.doi,
        abstract=view.abstract,
        methods=view.methods,
        models=list(view.models),
        datasets=view.datasets,
        findings=view.findings,
        limitations=view.limitations,
        topics=list(view.topics),
        owner_contribution=view.owner_contribution,
        evidence=[_evidence(e) for e in view.evidence[:_MAX_EVIDENCE]],
        conflicted_attributes=list(view.conflicted_attributes),
    )


def _education(view: EducationView) -> ProEducation:
    return ProEducation(
        claim_id=view.claim_id,
        strength=view.strength.value,
        review=view.review.value,
        accepted=view.accepted,
        sensitivity=_sensitivity(view.sensitivity),
        institution=view.institution,
        degree=view.degree,
        subject=view.subject,
        classification=view.classification,
        start=view.start,
        end=view.end,
        modules=list(view.modules),
        dissertation=view.dissertation,
        evidence=[_evidence(e) for e in view.evidence[:_MAX_EVIDENCE]],
        conflicted_attributes=list(view.conflicted_attributes),
    )


def _conflict(conflict: Conflict) -> ProConflict:
    return ProConflict(
        conflict_id=conflict.conflict_id,
        kind=conflict.kind.value,
        attribute=conflict.attribute,
        claim_ids=list(conflict.claim_ids),
        options=[
            ProConflictOption(
                option_id=o.option_id,
                value=o.value,
                claim_id=o.claim_id,
                source_ids=list(o.source_ids),
            )
            for o in conflict.options
        ],
        resolved=conflict.resolved,
        resolved_value=conflict.resolved_value,
    )


def _source(view: SourceView) -> ProSource:
    return ProSource(
        source_id=view.source_id,
        label=view.label,
        source_type=view.source_type.value,
        source_family_id=view.source_family_id,
        version=view.version,
        privacy_class=_sensitivity(view.privacy_class),
        freshness=view.freshness.value,
        ingested_at=view.ingested_at,
        refreshed_at=view.refreshed_at,
        resource_type=view.resource_type,
        accepted_claims=view.accepted_claims,
        pending_candidates=view.pending_candidates,
        conflicts=view.conflicts,
        candidate_extraction=view.candidate_extraction,
    )


def _requirement(data: RequirementEvidence) -> ProRequirement:
    return ProRequirement(
        status=data.status.value,
        normalized_requirement=data.normalized_requirement,
        skills=_claims(data.skills),
        inferred_skills=_claims(data.inferred_skills),
        projects=_claims(data.projects),
        employment=_claims(data.employment),
        research=_claims(data.research),
        education=_claims(data.education),
        unsupported_aspects=list(data.unsupported_aspects),
        notes=list(data.notes),
    )


# ------------------------------------------------------------------ routes


@router.get("/profile", response_model=ProfessionalProfileResponse)
def professional_profile(
    runtime: DesktopRuntime = _owner_runtime,
) -> ProfessionalProfileResponse:
    result = runtime.professional.get_profile(runtime.principal)
    base = _operation(runtime, result)
    data = result.data
    if not result.ok or data is None:
        return ProfessionalProfileResponse(**base.model_dump())
    counts: dict[str, int] = {}
    for claim in data.claims:
        counts[claim.category.value] = counts.get(claim.category.value, 0) + 1
    timeline = data.timeline
    experience = ProExperience(
        entries=[
            ProTimelineEntry(
                claim_id=e.claim_id,
                employer=e.employer,
                title=e.title,
                start=e.start,
                end=e.end,
                status=e.status.value,
                months=e.months,
                approximate=e.approximate,
            )
            for e in timeline.entries
        ],
        total_months=timeline.total.months,
        total_years=timeline.total.years,
        remainder_months=timeline.total.remainder_months,
        conservative_months=timeline.conservative_total.months,
        excluded_conflicted=len(timeline.excluded_conflicted),
        excluded_incomplete=len(timeline.excluded_incomplete),
        gaps=[f"{a} to {b} ({m} months)" for a, b, m in timeline.gaps],
    )
    runtime.activity.add("professional", "Professional profile viewed", "ok")
    return ProfessionalProfileResponse(
        **base.model_dump(),
        claims=_claims(data.claims),
        publications=[_publication(p) for p in data.publications],
        education=[_education(e) for e in data.education],
        experience=experience,
        conflicts=[_conflict(c) for c in data.conflicts],
        gaps=[
            ProGap(
                kind=g.kind.value,
                detail=g.detail,
                claim_id=g.claim_id,
                source_id=g.source_id,
            )
            for g in data.gaps
        ],
        sources=[_source(s) for s in data.sources],
        counts=counts,
    )


@router.post("/ingest", response_model=ProfessionalIngestResponse)
def professional_ingest(
    payload: ProfessionalIngestRequest, runtime: DesktopRuntime = _owner_runtime
) -> ProfessionalIngestResponse:
    content = _decode(payload.content_base64)
    if content is None or not content:
        return ProfessionalIngestResponse(
            status="rejected",
            reason_code="invalid_encoding",
            message=_message("invalid_encoding"),
        )
    result = runtime.professional.ingest_source(
        runtime.principal,
        name=payload.name,
        source_type=payload.source_type,
        privacy_class=_PRIVACY[payload.privacy_class],
        resource_type=payload.resource_type,
        content=content,
        use_llm=payload.use_candidates,
    )
    runtime.activity.add(
        "professional",
        "Professional source ingest",
        (result.data.status if result.data else (result.reason or "refused")),
    )
    base = _operation(runtime, result)
    summary = result.data
    if summary is None:
        return ProfessionalIngestResponse(**base.model_dump())
    if summary.status == "duplicate":
        base = base.model_copy(update={"message": _message("duplicate_source")})
    return ProfessionalIngestResponse(
        **base.model_dump(),
        source_id=summary.source_id,
        ingest_status=summary.status,
        claims_created=summary.claims_created,
        claims_updated=summary.claims_updated,
        claims_skipped_rejected=summary.claims_skipped_rejected,
        evidence_added=summary.evidence_added,
        conflicts_open=summary.conflicts_open,
        candidate_extraction=summary.candidate_extraction,
        candidates_proposed=summary.candidates_proposed,
        candidates_accepted=summary.candidates_accepted,
        candidates_rejected=summary.candidates_rejected,
        unmapped_skills=summary.unmapped_skills,
    )


@router.post("/review", response_model=OperationResult)
def professional_review(
    payload: ProfessionalReviewRequest, runtime: DesktopRuntime = _owner_runtime
) -> OperationResult:
    principal = runtime.principal
    service = runtime.professional
    result: OpResult[Any]
    if payload.action == "confirm" and payload.claim_id:
        result = service.confirm_claim(principal, payload.claim_id)
    elif payload.action == "reject" and payload.claim_id:
        result = service.reject_claim(principal, payload.claim_id)
    elif payload.action == "resolve" and payload.conflict_id and payload.option_id:
        result = service.resolve_conflict(
            principal, payload.conflict_id, payload.option_id
        )
    elif (
        payload.action == "set_privacy" and payload.source_id and payload.privacy_class
    ):
        result = service.set_source_privacy(
            principal, payload.source_id, _PRIVACY[payload.privacy_class]
        )
    else:
        return OperationResult(
            status="rejected",
            reason_code="missing_field",
            message=_message("missing_field"),
        )
    runtime.activity.add(
        "professional",
        f"Professional {payload.action}",
        "ok" if result.ok else "refused",
    )
    return _operation(runtime, result)


@router.post("/remove", response_model=OperationResult)
def professional_remove(
    payload: ProfessionalRemoveRequest, runtime: DesktopRuntime = _owner_runtime
) -> OperationResult:
    result = runtime.professional.remove_source(
        runtime.principal, payload.source_id, payload.confirmation_id
    )
    runtime.activity.add(
        "professional", "Professional source removal", result.permission
    )
    return _operation(runtime, result)


@router.post("/query", response_model=ProfessionalQueryResponse)
def professional_query(
    payload: ProfessionalQueryRequest, runtime: DesktopRuntime = _owner_runtime
) -> ProfessionalQueryResponse:
    mode = payload.mode
    if mode == "search":
        found = runtime.professional.search(runtime.principal, payload.text)
        base = _operation(runtime, found)
        runtime.activity.add("professional", "Professional search", found.permission)
        return ProfessionalQueryResponse(
            **base.model_dump(),
            mode=mode,
            claims=_claims(found.data or ()),
        )
    matched = runtime.professional.evidence_for(runtime.principal, payload.text)
    base = _operation(runtime, matched)
    runtime.activity.add(
        "professional", "Professional evidence lookup", matched.permission
    )
    return ProfessionalQueryResponse(
        **base.model_dump(),
        mode=mode,
        requirement=_requirement(matched.data) if matched.data else None,
    )


__all__ = ["router"]
