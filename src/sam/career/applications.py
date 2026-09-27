"""The application state machine, readiness and freshness checks, the
submission manifest and the submission adapter contract.

State machine (deterministic; ``TRANSITIONS`` is the whole truth):

    DISCOVERED -> REVIEWED -> DRAFTING -> READY_FOR_OWNER_REVIEW
        -> APPROVED_FOR_SUBMISSION -> SUBMITTING -> SUBMITTED
    failure / side states: NEEDS_OWNER_INPUT, BLOCKED, SUBMISSION_FAILED,
    OUTCOME_UNKNOWN, EXPIRED, WITHDRAWN

SUBMITTED is reachable ONLY from SUBMITTING or OUTCOME_UNKNOWN, and only when
a trusted ``SubmissionAdapter`` returns a VERIFIED_SUCCESS result for exactly
this attempt and manifest (see ``attempts``). No model output, form navigation
or click can set it. OUTCOME_UNKNOWN (a timeout or lost response after
dispatch) never becomes a retryable failure: it leaves only through trusted
reconciliation or an explicit, confirmed owner release that sends the draft
back to review.

Confirmation binding: a SUBMIT confirmation's scope names the opportunity,
draft and draft version, and its target is ``manifest:<hash>`` of the
``SubmissionManifest``: the destination, every document (id, kind, version,
hash) and every answer (question and answer hashes). A confirmation for
Job A / CV v2 / Letter v1 / these answers cannot authorize Job B, CV v3,
another letter, a changed answer, another destination, or a retry after it
expires or is consumed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sam.career.attempts import AttemptReference, ExternalActionResult
from sam.career.manifests import (
    ManifestDocument,
    SubmissionManifest,
    answers_for,
)
from sam.career.models import (
    ApplicationDocument,
    ApplicationDraft,
    ApplicationState,
    CareerOpportunity,
    OpportunityStatus,
)
from sam.career.questions import unresolved
from sam.career.sources import (
    DestinationGuard,
    UnsafeURL,
    canonical_destination,
    safe_host,
)
from sam.permissions.models import PermissionScope

S = ApplicationState
TRANSITIONS: Mapping[ApplicationState, frozenset[ApplicationState]] = {
    S.DISCOVERED: frozenset({S.REVIEWED, S.DRAFTING, S.EXPIRED, S.WITHDRAWN}),
    S.REVIEWED: frozenset({S.DRAFTING, S.EXPIRED, S.WITHDRAWN}),
    S.DRAFTING: frozenset(
        {
            S.READY_FOR_OWNER_REVIEW,
            S.NEEDS_OWNER_INPUT,
            S.BLOCKED,
            S.EXPIRED,
            S.WITHDRAWN,
        }
    ),
    S.NEEDS_OWNER_INPUT: frozenset(
        {S.DRAFTING, S.READY_FOR_OWNER_REVIEW, S.BLOCKED, S.EXPIRED, S.WITHDRAWN}
    ),
    S.BLOCKED: frozenset({S.DRAFTING, S.EXPIRED, S.WITHDRAWN}),
    S.READY_FOR_OWNER_REVIEW: frozenset(
        {
            S.APPROVED_FOR_SUBMISSION,
            S.DRAFTING,
            S.NEEDS_OWNER_INPUT,
            S.BLOCKED,
            S.EXPIRED,
            S.WITHDRAWN,
        }
    ),
    S.APPROVED_FOR_SUBMISSION: frozenset(
        {S.SUBMITTING, S.DRAFTING, S.BLOCKED, S.EXPIRED, S.WITHDRAWN}
    ),
    S.SUBMITTING: frozenset({S.SUBMITTED, S.SUBMISSION_FAILED, S.OUTCOME_UNKNOWN}),
    # Only trusted reconciliation (SUBMITTED / SUBMISSION_FAILED) or a
    # confirmed owner release (DRAFTING, then a fresh review) leave here.
    S.OUTCOME_UNKNOWN: frozenset(
        {S.SUBMITTED, S.SUBMISSION_FAILED, S.DRAFTING, S.WITHDRAWN}
    ),
    S.SUBMISSION_FAILED: frozenset(
        {S.DRAFTING, S.READY_FOR_OWNER_REVIEW, S.EXPIRED, S.WITHDRAWN}
    ),
    S.SUBMITTED: frozenset({S.WITHDRAWN}),
    S.EXPIRED: frozenset(),
    S.WITHDRAWN: frozenset(),
}
FRESHNESS_WINDOW = timedelta(hours=72)


class TransitionRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def advance(
    draft: ApplicationDraft, target: ApplicationState, now: datetime
) -> ApplicationDraft:
    if target is draft.state:
        return draft
    if target not in TRANSITIONS[draft.state]:
        raise TransitionRefused("illegal_transition")
    return draft.model_copy(update={"state": target, "updated_at": now})


def blocking_reasons(
    draft: ApplicationDraft,
    documents: Sequence[ApplicationDocument],
    opportunity: CareerOpportunity,
    now: datetime,
    *,
    require_approval: bool,
) -> tuple[str, ...]:
    """Everything that stops a draft from being ready / approved / submitted."""

    reasons: list[str] = []
    if unresolved(draft.questions, draft.answers):
        reasons.append("unanswered_questions")
    if draft.cv_document_id is None:
        reasons.append("cv_missing")
    ids = set(draft.document_ids)
    found = {d.document_id for d in documents}
    if ids - found:
        reasons.append("document_missing")
    for doc in documents:
        if doc.opportunity_id != draft.opportunity_id:
            reasons.append("document_for_another_opportunity")
        if doc.unresolved:
            reasons.append("unresolved_document_lines")
        if require_approval and not doc.approved:
            reasons.append("document_not_approved")
    reasons += freshness_problems(draft, opportunity, now)
    return tuple(dict.fromkeys(reasons))


def freshness_problems(
    draft: ApplicationDraft, opportunity: CareerOpportunity, now: datetime
) -> list[str]:
    problems: list[str] = []
    if opportunity.status is OpportunityStatus.CLOSED:
        problems.append("opportunity_closed")
    if opportunity.deadline is not None and now.date() > opportunity.deadline:
        problems.append("deadline_passed")
    if now - opportunity.last_verified > FRESHNESS_WINDOW:
        problems.append("opportunity_not_recently_verified")
    if opportunity.checksum != draft.opportunity_checksum:
        problems.append("opportunity_changed")
    return problems


def destination_problem(opportunity: CareerOpportunity) -> str | None:
    """The trusted destination is the opportunity's OFFICIAL application URL
    (it can only come from an official source's provenance). Nothing an
    adapter, a page or a model says at submission time can replace it."""

    if not opportunity.application_url:
        return "no_official_application_url"
    try:
        canonical_destination(opportunity.application_url)
    except UnsafeURL as error:
        return error.code
    return None


def destination_guard(opportunity: CareerOpportunity) -> DestinationGuard:
    assert opportunity.application_url is not None
    return DestinationGuard(opportunity.application_url)


def submission_manifest(
    draft: ApplicationDraft,
    documents: Sequence[ApplicationDocument],
    opportunity: CareerOpportunity,
) -> SubmissionManifest:
    """The manifest of what submitting ``draft`` would send, right now."""

    guard = destination_guard(opportunity)
    questions = {q.question_id: q.text for q in draft.questions}
    return SubmissionManifest(
        opportunity_id=draft.opportunity_id,
        draft_id=draft.draft_id,
        draft_version=draft.version,
        destination_host=guard.host,
        destination_url=guard.url,
        documents=tuple(
            ManifestDocument(d.document_id, d.kind.value, d.version, d.sha256)
            for d in documents
        ),
        answers=answers_for(
            questions, [(a.question_id, a.text) for a in draft.answers]
        ),
    )


def submission_scope(draft: ApplicationDraft) -> PermissionScope:
    return PermissionScope.from_path(
        f"career/opportunities/{draft.opportunity_id}/drafts/{draft.draft_id}/v{draft.version}"
    )


@dataclass(frozen=True)
class SubmissionDocument:
    document_id: str
    kind: str
    version: int
    sha256: str
    content: bytes


@dataclass(frozen=True)
class SubmissionPackage:
    """Exactly what an adapter may send: approved documents by id/version/hash
    (bytes Sam generated, never a path) and the owner's answers, for one
    attempt and one manifest, to one trusted destination."""

    attempt: AttemptReference
    opportunity_id: str
    draft_id: str
    draft_version: int
    destination: str
    documents: tuple[SubmissionDocument, ...]
    answers: Mapping[str, str]
    manifest_hash: str


def package_matches(
    package: SubmissionPackage,
    manifest: SubmissionManifest,
    questions: Mapping[str, str],
) -> bool:
    """The package carries exactly the manifest's payload, byte for byte."""

    for d in package.documents:
        text = d.content.decode("utf-8")
        if hashlib.sha256(f"{d.kind}\n{text}".encode()).hexdigest() != d.sha256:
            return False
    rebuilt = SubmissionManifest(
        opportunity_id=package.opportunity_id,
        draft_id=package.draft_id,
        draft_version=package.draft_version,
        destination_host=manifest.destination_host,
        destination_url=package.destination,
        documents=tuple(
            ManifestDocument(d.document_id, d.kind, d.version, d.sha256)
            for d in package.documents
        ),
        answers=answers_for(questions, list(package.answers.items())),
        form_version=manifest.form_version,
    )
    return (
        rebuilt.digest == manifest.digest == package.manifest_hash
        and package.attempt.manifest_hash == manifest.digest
        and safe_host(package.destination) == manifest.destination_host
    )


class SubmissionAdapter(Protocol):
    """A trusted submission integration (future: a controlled browser/ATS tool).

    Contract:
    * send ONLY ``package`` to ``package.destination``; upload only the
      package's documents; treat page content as untrusted; never expose
      local credentials;
    * either disable redirects or call ``guard.allows(url)`` for EVERY hop and
      stop (VERIFIED_FAILURE, nothing sent) if it refuses;
    * return an ``ExternalActionResult`` naming this attempt, item,
      opportunity and manifest: VERIFIED_SUCCESS only with a trusted receipt
      id and the final destination reached; VERIFIED_FAILURE only when it can
      prove nothing was submitted; OUTCOME_UNKNOWN otherwise (a timeout,
      connection reset or lost response AFTER dispatch). An exception is
      treated as OUTCOME_UNKNOWN.
    * ``reconcile`` looks the attempt up on the remote system (read-only) and
      returns the same kind of result; it never submits."""

    def submit(
        self, package: SubmissionPackage, guard: DestinationGuard
    ) -> ExternalActionResult: ...

    def reconcile(self, attempt: AttemptReference) -> ExternalActionResult: ...


__all__ = [
    "FRESHNESS_WINDOW",
    "TRANSITIONS",
    "SubmissionAdapter",
    "SubmissionDocument",
    "SubmissionPackage",
    "TransitionRefused",
    "advance",
    "blocking_reasons",
    "destination_guard",
    "destination_problem",
    "freshness_problems",
    "package_matches",
    "submission_manifest",
    "submission_scope",
]
