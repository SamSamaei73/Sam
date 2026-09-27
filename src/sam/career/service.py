"""``CareerService``: the one Sam-owned entry point to the Career & PhD Agent.

REVIEW FIRST. Sam may discover, normalize, deduplicate, analyse fit, retrieve
evidence, prepare local drafts, fill safe deterministic fields and track
opportunities. It never submits or sends by default.

Authorization (PermissionEngine, ``PermissionResource.CAREER``):

    READ    inspect opportunities, applications, evidence, review queue  LOW
    CREATE  import/discover/track, create drafts, contacts, outreach     MEDIUM
    UPDATE  answer questions, edit/approve documents, approve packages   MEDIUM
    SUBMIT  submit an application package                               HIGH + confirm
    SEND    send outreach (plus the e-mail tool's own GMAIL SEND)        HIGH + confirm
    DELETE  withdraw an application                                      HIGH + confirm

A SUBMIT / SEND confirmation is bound to one opportunity, draft or message
version and the hash of an immutable manifest of the exact external payload
(see ``manifests``). PROACTIVE
EXECUTE authorizes none of this: Career asks only for CAREER (and GMAIL for the
e-mail tool) permissions, and a proactive run can only ever request its own
EXECUTE and READs.

Submission happens only through a trusted ``SubmissionAdapter``; sending only
through a trusted ``EmailTool``. Neither is configured in Phase 16's desktop
runtime, so both fail closed there.

At most once (see ``attempts``): every check, the confirmation consumption and
the creation of the attempt happen under one lock, in this order: freshness,
destination, unresolved questions, manifest, permissions + confirmation
binding, one-time consumption, attempt IN_FLIGHT; only then, outside the lock,
the adapter. A second request for the same item while an attempt is in flight,
unknown or verified never reaches the adapter again. An outcome that cannot be
proven (timeout, reset, lost response, malformed or mismatched result) is
OUTCOME_UNKNOWN: never retried, never SUBMITTED, never "safely failed".
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Concatenate, Protocol

from sam.career.applications import (
    SubmissionAdapter,
    SubmissionDocument,
    SubmissionPackage,
    TransitionRefused,
    advance,
    blocking_reasons,
    destination_guard,
    destination_problem,
    freshness_problems,
    package_matches,
    submission_manifest,
    submission_scope,
)
from sam.career.attempts import (
    AttemptLedger,
    AttemptReference,
    AttemptState,
    ExternalAction,
    ExternalActionAttempt,
    ExternalActionResult,
    ExternalActionStatus,
    Interpretation,
    interpret,
)
from sam.career.audit import (
    CareerAuditSink,
    CareerOperation,
    InMemoryCareerAuditSink,
    new_event,
)
from sam.career.claims import ClaimChecker
from sam.career.contacts import ContactRejected, build_contact
from sam.career.dedup import merge, same_opportunity
from sam.career.documents import (
    MotivationWriter,
    build_cv,
    build_letter,
    build_proposal_outline,
    build_research_statement,
    make_document,
    revalidate,
)
from sam.career.evidence import EvidenceClaim, EvidencePort, NoPapers, PaperPort
from sam.career.manifests import SendManifest
from sam.career.matching import (
    AlignmentReport,
    FitReport,
    analyze_fit,
    research_alignment,
)
from sam.career.models import (
    AnswerSource,
    ApplicationDocument,
    ApplicationDraft,
    ApplicationState,
    CareerOpportunity,
    CareerPreferences,
    Contact,
    ContactRole,
    DocumentKind,
    DocumentLine,
    DraftAnswer,
    FollowUp,
    OpportunityType,
    OutreachChannel,
    OutreachDraft,
    OutreachKind,
    OutreachState,
    QuestionClass,
    SourceKind,
    clean_text,
)
from sam.career.opportunities import ListingRejected, normalize_listing
from sam.career.outreach import SENDABLE_CHANNELS, build_outreach
from sam.career.questions import SENSITIVE, make_questions, prefill, unresolved
from sam.career.repository import (
    CareerRepository,
    InMemoryCareerRepository,
    RepositoryFull,
)
from sam.career.sources import (
    DestinationGuard,
    OpportunitySource,
    RawListing,
    SearchQuery,
)
from sam.career.tracking import (
    FollowUpState,
    record_draft,
    record_sent,
    schedule,
    state,
)
from sam.models.models import PrivacyClass
from sam.models.policies import PrivacyPolicy
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    DecisionOutcome,
    PermissionAction,
    PermissionRequest,
    PermissionResource,
    PermissionScope,
    Principal,
    utc_now,
)

_R = PermissionResource.CAREER
_ROOT = PermissionScope.from_path("career")
_PRIVACY = PrivacyPolicy()
DEADLINE_SOON = timedelta(days=3)
S = ApplicationState
_UNKNOWN = ExternalActionStatus.OUTCOME_UNKNOWN
# No edit, answer, approval or expiry may touch a draft in these states.
_CLOSED = (S.SUBMITTING, S.SUBMITTED, S.OUTCOME_UNKNOWN, S.WITHDRAWN, S.EXPIRED)
_ATTEMPT_ANSWERS = {
    AttemptState.IN_FLIGHT: "already_in_flight",
    AttemptState.OUTCOME_UNKNOWN: "outcome_unknown",
    AttemptState.VERIFIED_SUCCESS: "already_done",
    AttemptState.VERIFIED_FAILURE: "attempt_failed",
    AttemptState.RELEASED_BY_OWNER: "attempt_released",
}


@dataclass(frozen=True)
class OpResult[T]:
    permission: str
    ok: bool
    reason: str | None = None
    confirmation_id: str | None = None
    data: T | None = None
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class OutboundEmail:
    attempt: AttemptReference
    to: str
    subject: str
    body: str
    manifest_hash: str


class EmailTool(Protocol):
    """Sam's controlled e-mail tool boundary. Career never holds credentials.

    Same contract as ``SubmissionAdapter``: return an ``ExternalActionResult``
    for this attempt and manifest; ``destination`` is the recipient actually
    used; a transport timeout or reset AFTER handing the message over is
    OUTCOME_UNKNOWN unless the provider proves non-delivery; an exception is
    OUTCOME_UNKNOWN. ``reconcile`` is a read-only lookup."""

    def send(self, message: OutboundEmail) -> ExternalActionResult: ...

    def reconcile(self, attempt: AttemptReference) -> ExternalActionResult: ...


@dataclass(frozen=True)
class _Reserved[T]:
    """A validated, confirmed, reserved external action, ready to dispatch."""

    attempt: ExternalActionAttempt
    item: T
    permission: str
    confirmation_id: str | None
    destination_ok: Callable[[str | None], bool]


def _serialized[**P, R](
    method: Callable[Concatenate[CareerService, P], R],
) -> Callable[Concatenate[CareerService, P], R]:
    """Run a state-changing method under the single-flight lock, so it can
    never interleave with the reservation or completion of a SUBMIT / SEND."""

    def wrapper(self: CareerService, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._attempts.lock:
            return method(self, *args, **kwargs)

    functools.update_wrapper(wrapper, method)
    return wrapper


@dataclass(frozen=True)
class ReviewItem:
    kind: str  # closed set: needs_answer, needs_review, ready_for_approval, ...
    label: str
    opportunity_id: str | None = None
    item_id: str | None = None


@dataclass(frozen=True)
class Overview:
    opportunities: tuple[CareerOpportunity, ...]
    drafts: tuple[ApplicationDraft, ...]
    documents: tuple[ApplicationDocument, ...]
    contacts: tuple[Contact, ...]
    outreach: tuple[OutreachDraft, ...]
    follow_ups: tuple[FollowUp, ...]
    review_queue: tuple[ReviewItem, ...]
    preferences: CareerPreferences
    submission_available: bool
    sending_available: bool


def _words(value: str) -> str:
    return value.replace("_", " ")


def _doc_id(
    kind: DocumentKind, draft_id: str, version: int, *, edited: bool = False
) -> str:
    base = f"{kind.value.replace('_', '-')}-{draft_id[3:]}-v{version}"
    return f"{base}-e{uuid.uuid4().hex[:6]}" if edited else base


def _is_secret(*texts: str) -> bool:
    return _PRIVACY.classify(texts, PrivacyClass.PUBLIC) is PrivacyClass.SECRET


class CareerService:
    def __init__(
        self,
        *,
        permission_engine: PermissionEngine,
        evidence: EvidencePort,
        papers: PaperPort | None = None,
        repository: CareerRepository | None = None,
        audit_sink: CareerAuditSink | None = None,
        sources: Sequence[OpportunitySource] = (),
        submitter: SubmissionAdapter | None = None,
        email: EmailTool | None = None,
        motivation_writer: MotivationWriter | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._permissions = permission_engine
        self._evidence = evidence
        self._papers = papers or NoPapers()
        self.repository: CareerRepository = repository or InMemoryCareerRepository()
        self.audit: CareerAuditSink = audit_sink or InMemoryCareerAuditSink()
        self._sources = tuple(sources)
        self._submitter = submitter
        self._email = email
        self._writer = motivation_writer
        self._clock = clock
        self._checker = ClaimChecker(evidence, self._papers)
        self._attempts = AttemptLedger()

    @property
    def attempts(self) -> AttemptLedger:
        return self._attempts

    # ------------------------------------------------------------ plumbing

    def _authorize(
        self,
        principal: Principal,
        action: PermissionAction,
        scope: PermissionScope,
        *,
        confirmation_id: str | None = None,
        target: str | None = None,
        resource: PermissionResource = _R,
    ) -> tuple[bool, str, str | None, str | None]:
        try:
            request = PermissionRequest(
                principal=principal,
                action=action,
                resource=resource,
                scope=scope,
                target=target,
            )
            decision = self._permissions.evaluate(
                request, confirmation_id=confirmation_id
            )
        except Exception:
            return False, "deny", "permission_denied", None
        if decision.outcome is DecisionOutcome.ALLOW:
            return True, "allow", None, None
        if decision.outcome is DecisionOutcome.CONFIRM_REQUIRED:
            pending = (
                decision.confirmation.confirmation_id if decision.confirmation else None
            )
            return False, "confirm_required", "confirmation_required", pending
        invalid = (
            decision.reason is not None
            and decision.reason.value == "confirmation_invalid"
        )
        return (
            False,
            "deny",
            "confirmation_invalid" if invalid else "permission_denied",
            None,
        )

    def _record(
        self,
        operation: CareerOperation,
        principal: Principal,
        permission: str,
        status: str,
        **fields: object,
    ) -> None:
        self.audit.record(
            new_event(
                operation,
                at=self._clock(),
                principal_id=principal.id,
                permission=permission,
                status=status,
                **fields,
            )
        )

    def _denied[T](
        self,
        operation: CareerOperation,
        principal: Principal,
        permission: str,
        reason: str | None,
        pending: str | None,
        **fields: object,
    ) -> OpResult[T]:
        self._record(
            operation, principal, permission, "denied", reason_code=reason, **fields
        )
        return OpResult(permission, False, reason, pending)

    def _gate(
        self,
        operation: CareerOperation,
        principal: Principal,
        action: PermissionAction,
        scope: PermissionScope,
    ) -> OpResult[Any] | None:
        allowed, permission, reason, pending = self._authorize(principal, action, scope)
        if allowed:
            return None
        return self._denied(operation, principal, permission, reason, pending)

    def _draft(self, principal: Principal, draft_id: str) -> ApplicationDraft | None:
        draft = self.repository.get_draft(draft_id)
        return draft if draft is not None and draft.owner_id == principal.id else None

    def _claims(self, principal: Principal) -> tuple[EvidenceClaim, ...] | None:
        return self._evidence.claims(principal)

    def _documents(self, draft: ApplicationDraft) -> list[ApplicationDocument]:
        docs = [self.repository.get_document(i) for i in draft.document_ids]
        return [d for d in docs if d is not None]

    @staticmethod
    def _scope(*parts: str) -> PermissionScope:
        return PermissionScope.from_path("/".join(("career", *parts)))

    # --------------------------------------------------------------- reads

    def overview(self, principal: Principal) -> OpResult[Overview]:
        denied = self._gate(
            CareerOperation.READ, principal, PermissionAction.READ, _ROOT
        )
        if denied is not None:
            return denied
        drafts = tuple(
            d for d in self.repository.list_drafts() if d.owner_id == principal.id
        )
        doc_ids = {i for d in drafts for i in d.document_ids}
        data = Overview(
            opportunities=self.repository.list_opportunities(),
            drafts=drafts,
            documents=tuple(
                d for d in self.repository.list_documents() if d.document_id in doc_ids
            ),
            contacts=self.repository.list_contacts(),
            outreach=self.repository.list_outreach(),
            follow_ups=self.repository.list_follow_ups(),
            review_queue=self._queue(principal, drafts),
            preferences=self.repository.get_preferences(principal.id),
            submission_available=self._submitter is not None,
            sending_available=self._email is not None,
        )
        self._record(CareerOperation.READ, principal, "allow", "ok")
        return OpResult("allow", True, data=data)

    def fit(self, principal: Principal, opportunity_id: str) -> OpResult[FitReport]:
        denied = self._gate(
            CareerOperation.READ, principal, PermissionAction.READ, _ROOT
        )
        if denied is not None:
            return denied
        opportunity = self.repository.get_opportunity(opportunity_id)
        if opportunity is None:
            return OpResult("allow", False, "opportunity_not_found")
        return OpResult(
            "allow", True, data=analyze_fit(principal, opportunity, self._evidence)
        )

    def alignment(
        self, principal: Principal, opportunity_id: str, contact_id: str | None = None
    ) -> OpResult[AlignmentReport]:
        denied = self._gate(
            CareerOperation.READ, principal, PermissionAction.READ, _ROOT
        )
        if denied is not None:
            return denied
        opportunity = self.repository.get_opportunity(opportunity_id)
        if opportunity is None:
            return OpResult("allow", False, "opportunity_not_found")
        contact = self.repository.get_contact(contact_id) if contact_id else None
        topics = (
            contact.research_topics if contact else ()
        ) or opportunity.research_topics
        return OpResult(
            "allow", True, data=research_alignment(principal, topics, self._evidence)
        )

    def counts(self, principal: Principal) -> OpResult[Mapping[str, int]]:
        """Content-free counts for Proactive watches (CAREER READ)."""

        denied = self._gate(
            CareerOperation.READ, principal, PermissionAction.READ, _ROOT
        )
        if denied is not None:
            return denied
        drafts = tuple(
            d for d in self.repository.list_drafts() if d.owner_id == principal.id
        )
        queue = self._queue(principal, drafts)
        return OpResult(
            "allow",
            True,
            data={
                "review": sum(1 for i in queue if i.kind != "deadline_soon"),
                "deadlines": sum(1 for i in queue if i.kind == "deadline_soon"),
                "follow_ups": sum(1 for i in queue if i.kind == "follow_up_due"),
            },
        )

    def _queue(
        self, principal: Principal, drafts: Sequence[ApplicationDraft]
    ) -> tuple[ReviewItem, ...]:
        now = self._clock()
        items: list[ReviewItem] = []
        for draft in drafts:
            opportunity = self.repository.get_opportunity(draft.opportunity_id)
            name = opportunity.title if opportunity else draft.opportunity_id
            for q in unresolved(draft.questions, draft.answers):
                label = f"Needs your answer: {q.kind.value.replace('_', ' ')} ({name})"
                items.append(
                    ReviewItem(
                        "needs_answer", label, draft.opportunity_id, draft.draft_id
                    )
                )
            for doc in self._documents(draft):
                if doc.unresolved:
                    items.append(
                        ReviewItem(
                            "needs_review",
                            f"Needs your review: {_words(doc.kind.value)} ({name})",
                            draft.opportunity_id,
                            doc.document_id,
                        )
                    )
            if draft.state is S.READY_FOR_OWNER_REVIEW:
                items.append(
                    ReviewItem(
                        "ready_for_approval",
                        f"Ready for your approval: {name}",
                        draft.opportunity_id,
                        draft.draft_id,
                    )
                )
            if draft.state is S.OUTCOME_UNKNOWN:
                items.append(
                    ReviewItem(
                        "outcome_unknown",
                        f"Outcome unknown: {name} may or may not have been "
                        "submitted. Sam will not retry; please check.",
                        draft.opportunity_id,
                        draft.draft_id,
                    )
                )
            if draft.state is S.APPROVED_FOR_SUBMISSION:
                items.append(
                    ReviewItem(
                        "ready_for_submission",
                        f"Ready for submission (needs your confirmation): {name}",
                        draft.opportunity_id,
                        draft.draft_id,
                    )
                )
            if (
                opportunity is not None
                and opportunity.deadline is not None
                and draft.state not in _CLOSED
                and now.date() <= opportunity.deadline <= (now + DEADLINE_SOON).date()
            ):
                days = (opportunity.deadline - now.date()).days
                items.append(
                    ReviewItem(
                        "deadline_soon",
                        f"Deadline: {name} closes in {days} day(s)",
                        draft.opportunity_id,
                        draft.draft_id,
                    )
                )
        for message in self.repository.list_outreach():
            if message.state is OutreachState.READY_FOR_OWNER_REVIEW:
                items.append(
                    ReviewItem(
                        "needs_approval",
                        f"Needs approval: {_words(message.kind.value)} message",
                        message.opportunity_id,
                        message.outreach_id,
                    )
                )
            elif message.state is OutreachState.OUTCOME_UNKNOWN:
                items.append(
                    ReviewItem(
                        "outcome_unknown",
                        f"Outcome unknown: a {_words(message.kind.value)} message "
                        "may or may not have been sent. Sam will not resend.",
                        message.opportunity_id,
                        message.outreach_id,
                    )
                )
            elif message.state is OutreachState.APPROVED:
                items.append(
                    ReviewItem(
                        "ready_to_send",
                        "Ready to send (needs your confirmation): "
                        f"{_words(message.kind.value)} message",
                        message.opportunity_id,
                        message.outreach_id,
                    )
                )
        for follow_up in self.repository.list_follow_ups():
            if state(follow_up, now) is FollowUpState.FOLLOW_UP_DUE:
                items.append(
                    ReviewItem(
                        "follow_up_due",
                        "Follow-up due",
                        follow_up.opportunity_id,
                        follow_up.follow_up_id,
                    )
                )
        return tuple(items)

    # --------------------------------------------------------- opportunities

    def import_listing(
        self, principal: Principal, listing: RawListing
    ) -> OpResult[CareerOpportunity]:
        denied = self._gate(
            CareerOperation.IMPORT,
            principal,
            PermissionAction.CREATE,
            self._scope("opportunities"),
        )
        if denied is not None:
            return denied
        return self._import(principal, listing)

    @_serialized
    def _import(
        self, principal: Principal, listing: RawListing
    ) -> OpResult[CareerOpportunity]:
        try:
            incoming = normalize_listing(listing, self._clock())
        except ListingRejected as error:
            self._record(
                CareerOperation.IMPORT,
                principal,
                "allow",
                "rejected",
                source=listing.kind.value,
                reason_code=error.code,
            )
            return OpResult("allow", False, error.code)
        existing = next(
            (
                o
                for o in self.repository.list_opportunities()
                if same_opportunity(o, incoming)
            ),
            None,
        )
        stored = merge(existing, incoming) if existing else incoming
        try:
            self.repository.put_opportunity(stored)
        except RepositoryFull as error:
            return OpResult("allow", False, error.code)
        self._record(
            CareerOperation.IMPORT,
            principal,
            "allow",
            "merged" if existing else "created",
            opportunity_id=stored.opportunity_id,
            version=stored.version,
            source=listing.kind.value,
        )
        return OpResult("allow", True, data=stored)

    def discover(self, principal: Principal, query: SearchQuery) -> OpResult[int]:
        """Run the configured read-only source adapters. Page content is
        untrusted data; nothing in it can direct Sam."""

        denied = self._gate(
            CareerOperation.DISCOVER,
            principal,
            PermissionAction.CREATE,
            self._scope("opportunities"),
        )
        if denied is not None:
            return denied
        imported = 0
        for source in self._sources:
            try:
                listings = list(source.search(query))[:100]
            except Exception:
                continue
            for listing in listings:
                if self._import(principal, listing).ok:
                    imported += 1
        return OpResult("allow", True, data=imported)

    @_serialized
    def track(
        self, principal: Principal, opportunity_id: str
    ) -> OpResult[ApplicationDraft]:
        denied = self._gate(
            CareerOperation.TRACK,
            principal,
            PermissionAction.CREATE,
            self._scope("applications"),
        )
        if denied is not None:
            return denied
        opportunity = self.repository.get_opportunity(opportunity_id)
        if opportunity is None:
            return OpResult("allow", False, "opportunity_not_found")
        existing = next(
            (
                d
                for d in self.repository.list_drafts()
                if d.opportunity_id == opportunity_id and d.owner_id == principal.id
            ),
            None,
        )
        if existing is not None:
            return OpResult("allow", True, data=existing)
        now = self._clock()
        draft = ApplicationDraft(
            draft_id="ap_" + uuid.uuid4().hex[:20],
            opportunity_id=opportunity_id,
            owner_id=principal.id,
            state=S.DISCOVERED,
            opportunity_checksum=opportunity.checksum,
            created_at=now,
            updated_at=now,
        )
        self.repository.put_draft(draft)
        self.repository.put_opportunity(
            opportunity.model_copy(update={"tracked": True})
        )
        self._record(
            CareerOperation.TRACK,
            principal,
            "allow",
            "ok",
            opportunity_id=opportunity_id,
            item_id=draft.draft_id,
        )
        return OpResult("allow", True, data=draft)

    @_serialized
    def mark_reviewed(
        self, principal: Principal, draft_id: str
    ) -> OpResult[ApplicationDraft]:
        return self._update_state(principal, draft_id, S.REVIEWED)

    def _update_state(
        self, principal: Principal, draft_id: str, target: ApplicationState
    ) -> OpResult[ApplicationDraft]:
        denied = self._gate(
            CareerOperation.EDIT,
            principal,
            PermissionAction.UPDATE,
            self._scope("applications", draft_id),
        )
        if denied is not None:
            return denied
        draft = self._draft(principal, draft_id)
        if draft is None:
            return OpResult("allow", False, "draft_not_found")
        try:
            updated = advance(draft, target, self._clock())
        except TransitionRefused as error:
            return OpResult("allow", False, error.code)
        self.repository.put_draft(updated)
        return OpResult("allow", True, data=updated)

    # -------------------------------------------------------------- drafting

    @_serialized
    def create_draft(
        self,
        principal: Principal,
        opportunity_id: str,
        *,
        questions: Sequence[str] = (),
        motivation: str | None = None,
        direction: str | None = None,
        use_model: bool = False,
    ) -> OpResult[ApplicationDraft]:
        denied = self._gate(
            CareerOperation.DRAFT,
            principal,
            PermissionAction.CREATE,
            self._scope("applications"),
        )
        if denied is not None:
            return denied
        opportunity = self.repository.get_opportunity(opportunity_id)
        if opportunity is None:
            return OpResult("allow", False, "opportunity_not_found")
        for text in (motivation or "", direction or "", *questions):
            if _is_secret(text):
                return OpResult("allow", False, "secret_detected")
        claims = self._claims(principal)
        if claims is None:
            return OpResult("allow", False, "evidence_unavailable")
        tracked = self.track(principal, opportunity_id)
        if not tracked.ok or tracked.data is None:
            return OpResult(tracked.permission, False, tracked.reason)
        record = tracked.data
        if record.state in _CLOSED:
            return OpResult("allow", False, "application_closed")
        now = self._clock()
        fit = analyze_fit(principal, opportunity, self._evidence)
        lines_motivation: list[str] = [motivation] if motivation else []
        if not lines_motivation and use_model and self._writer is not None:
            skills = [
                c.attributes.get("display", c.statement)
                for c in fit.supported_claims
                if c.category in ("skill", "technology")
            ]
            written = self._writer.write(opportunity, skills[:8])
            lines_motivation = [clean_text(x, 1_000) for x in (written or ())][:4]
        version = record.version + (0 if record.cv_document_id is None else 1)
        documents: list[ApplicationDocument] = []

        def doc(
            kind: DocumentKind, lines: Sequence[DocumentLine]
        ) -> ApplicationDocument:
            document = make_document(
                document_id=_doc_id(kind, record.draft_id, version),
                opportunity_id=opportunity_id,
                kind=kind,
                version=version,
                lines=lines,
                now=now,
            )
            self.repository.put_document(document)
            documents.append(document)
            return document

        cv = doc(DocumentKind.CV, build_cv(claims, fit))
        letter = doc(
            DocumentKind.COVER_LETTER,
            build_letter(
                principal, opportunity, fit, claims, self._checker, lines_motivation
            ),
        )
        extra: list[str] = []
        if opportunity.type is OpportunityType.PHD:
            alignment = research_alignment(
                principal, opportunity.research_topics, self._evidence
            )
            extra.append(
                doc(
                    DocumentKind.RESEARCH_STATEMENT,
                    build_research_statement(
                        principal,
                        opportunity,
                        alignment,
                        direction,
                        claims,
                        self._checker,
                    ),
                ).document_id
            )
            extra.append(
                doc(
                    DocumentKind.PROPOSAL_OUTLINE,
                    build_proposal_outline(
                        principal,
                        opportunity,
                        alignment,
                        direction,
                        claims,
                        self._checker,
                    ),
                ).document_id
            )
        asked = make_questions(questions)
        prefs = self.repository.get_preferences(principal.id)
        draft = record.model_copy(
            update={
                "version": version,
                "cv_document_id": cv.document_id,
                "letter_document_id": letter.document_id,
                "extra_document_ids": tuple(extra),
                "questions": asked,
                # A fresh draft never reuses an earlier application's answers.
                "answers": prefill(asked, prefs, fit),
                "opportunity_checksum": opportunity.checksum,
                "approved_binding": None,
                "updated_at": now,
            }
        )
        draft = self._evaluate(draft, opportunity)
        self.repository.put_draft(draft)
        self._record(
            CareerOperation.DRAFT,
            principal,
            "allow",
            draft.state.value,
            opportunity_id=opportunity_id,
            item_id=draft.draft_id,
            version=draft.version,
        )
        return OpResult("allow", True, data=draft)

    def _evaluate(
        self, draft: ApplicationDraft, opportunity: CareerOpportunity
    ) -> ApplicationDraft:
        """Deterministic next state after any change to a draft."""

        now = self._clock()
        if draft.state in (
            S.DISCOVERED,
            S.REVIEWED,
            S.APPROVED_FOR_SUBMISSION,
            S.SUBMISSION_FAILED,
            S.BLOCKED,
        ):
            draft = advance(draft, S.DRAFTING, now).model_copy(
                update={"approved_binding": None}
            )
        if draft.state not in (
            S.DRAFTING,
            S.NEEDS_OWNER_INPUT,
            S.READY_FOR_OWNER_REVIEW,
        ):
            return draft
        reasons = set(
            blocking_reasons(
                draft, self._documents(draft), opportunity, now, require_approval=False
            )
        )
        hard = {
            "opportunity_closed",
            "deadline_passed",
            "document_for_another_opportunity",
        }
        stale = {"opportunity_not_recently_verified", "opportunity_changed"}
        if reasons & (hard | stale):
            target = S.BLOCKED
        elif reasons:
            target = S.NEEDS_OWNER_INPUT
        else:
            target = S.READY_FOR_OWNER_REVIEW
        return advance(draft, target, now)

    def _changed(self, draft: ApplicationDraft, **update: object) -> ApplicationDraft:
        """Any change: new version, approval and binding cleared."""

        opportunity = self.repository.get_opportunity(draft.opportunity_id)
        changed = draft.model_copy(
            update={
                **update,
                "version": draft.version + 1,
                "approved_binding": None,
                "updated_at": self._clock(),
            }
        )
        return self._evaluate(changed, opportunity) if opportunity else changed

    @_serialized
    def answer_question(
        self, principal: Principal, draft_id: str, question_id: str, text: str
    ) -> OpResult[ApplicationDraft]:
        denied = self._gate(
            CareerOperation.ANSWER,
            principal,
            PermissionAction.UPDATE,
            self._scope("applications", draft_id),
        )
        if denied is not None:
            return denied
        draft = self._draft(principal, draft_id)
        if draft is None:
            return OpResult("allow", False, "draft_not_found")
        if draft.state in _CLOSED:
            return OpResult("allow", False, "application_closed")
        question = next(
            (q for q in draft.questions if q.question_id == question_id), None
        )
        if question is None:
            return OpResult("allow", False, "question_not_found")
        answer = clean_text(text, 2_000)
        if _is_secret(answer):
            return OpResult("allow", False, "secret_detected")
        sensitive = (
            question.kind in SENSITIVE
            or question.classification is QuestionClass.OWNER_REVIEW_REQUIRED
        )
        answers = [a for a in draft.answers if a.question_id != question_id]
        if answer:
            answers.append(
                DraftAnswer(
                    question_id=question_id,
                    text=answer,
                    source=AnswerSource.OWNER,
                    sensitive=sensitive,
                )
            )
        updated = self._changed(draft, answers=tuple(answers))
        self.repository.put_draft(updated)
        # The answer text (possibly salary, visa ...) is never audited.
        self._record(
            CareerOperation.ANSWER,
            principal,
            "allow",
            "ok",
            opportunity_id=draft.opportunity_id,
            item_id=draft_id,
            version=updated.version,
        )
        return OpResult("allow", True, data=updated)

    @_serialized
    def edit_document(
        self, principal: Principal, document_id: str, lines: Sequence[str]
    ) -> OpResult[ApplicationDocument]:
        denied = self._gate(
            CareerOperation.EDIT,
            principal,
            PermissionAction.UPDATE,
            self._scope("documents", document_id),
        )
        if denied is not None:
            return denied
        document = self.repository.get_document(document_id)
        draft = next(
            (
                d
                for d in self.repository.list_drafts()
                if document_id in d.document_ids and d.owner_id == principal.id
            ),
            None,
        )
        if document is None or draft is None:
            return OpResult("allow", False, "document_not_found")
        if draft.state in _CLOSED:
            return OpResult("allow", False, "application_closed")
        if _is_secret(*lines):
            return OpResult("allow", False, "secret_detected")
        claims = self._claims(principal)
        if claims is None:
            return OpResult("allow", False, "evidence_unavailable")
        checked = revalidate(
            principal, lines[:400], document, claims, self._evidence, self._checker
        )
        version = document.version + 1
        new = make_document(
            document_id=_doc_id(document.kind, draft.draft_id, version, edited=True),
            opportunity_id=document.opportunity_id,
            kind=document.kind,
            version=version,
            lines=checked,
            now=self._clock(),
        )
        self.repository.put_document(new)
        refs = {
            "cv_document_id": new.document_id
            if draft.cv_document_id == document_id
            else draft.cv_document_id,
            "letter_document_id": new.document_id
            if draft.letter_document_id == document_id
            else draft.letter_document_id,
            "extra_document_ids": tuple(
                new.document_id if i == document_id else i
                for i in draft.extra_document_ids
            ),
        }
        self.repository.put_draft(self._changed(draft, **refs))
        self._record(
            CareerOperation.EDIT,
            principal,
            "allow",
            "ok",
            opportunity_id=draft.opportunity_id,
            item_id=new.document_id,
            version=version,
        )
        return OpResult("allow", True, data=new)

    @_serialized
    def approve_document(
        self, principal: Principal, document_id: str
    ) -> OpResult[ApplicationDocument]:
        denied = self._gate(
            CareerOperation.APPROVE,
            principal,
            PermissionAction.UPDATE,
            self._scope("documents", document_id),
        )
        if denied is not None:
            return denied
        document = self.repository.get_document(document_id)
        draft = next(
            (
                d
                for d in self.repository.list_drafts()
                if document_id in d.document_ids and d.owner_id == principal.id
            ),
            None,
        )
        if document is None or draft is None:
            return OpResult("allow", False, "document_not_found")
        if draft.state in _CLOSED:
            return OpResult("allow", False, "application_closed")
        if document.unresolved:
            return OpResult("allow", False, "unresolved_lines")
        approved = document.model_copy(update={"approved": True})
        self.repository.put_document(approved)
        opportunity = self.repository.get_opportunity(draft.opportunity_id)
        if opportunity is not None:
            self.repository.put_draft(self._evaluate(draft, opportunity))
        self._record(
            CareerOperation.APPROVE,
            principal,
            "allow",
            "document",
            opportunity_id=draft.opportunity_id,
            item_id=document_id,
            version=document.version,
        )
        return OpResult("allow", True, data=approved)

    # ------------------------------------------------------------ submission

    @_serialized
    def approve_for_submission(
        self, principal: Principal, draft_id: str
    ) -> OpResult[ApplicationDraft]:
        denied = self._gate(
            CareerOperation.APPROVE,
            principal,
            PermissionAction.UPDATE,
            self._scope("applications", draft_id),
        )
        if denied is not None:
            return denied
        draft = self._draft(principal, draft_id)
        if draft is None:
            return OpResult("allow", False, "draft_not_found")
        opportunity = self.repository.get_opportunity(draft.opportunity_id)
        if opportunity is None:
            return OpResult("allow", False, "opportunity_not_found")
        if draft.state is not S.READY_FOR_OWNER_REVIEW:
            return OpResult("allow", False, "not_ready_for_approval")
        documents = self._documents(draft)
        reasons = blocking_reasons(
            draft, documents, opportunity, self._clock(), require_approval=True
        )
        if reasons:
            return OpResult("allow", False, reasons[0])
        problem = destination_problem(opportunity)
        if problem:
            return OpResult("allow", False, problem)
        manifest = submission_manifest(draft, documents, opportunity)
        approved = advance(draft, S.APPROVED_FOR_SUBMISSION, self._clock()).model_copy(
            update={"approved_binding": manifest.digest}
        )
        self.repository.put_draft(approved)
        self._record(
            CareerOperation.APPROVE,
            principal,
            "allow",
            "package",
            opportunity_id=draft.opportunity_id,
            item_id=draft_id,
            version=draft.version,
            manifest_hash=manifest.digest,
        )
        return OpResult("allow", True, data=approved)

    def _attempt_answer[T](
        self, attempt: ExternalActionAttempt, item: T
    ) -> OpResult[T]:
        """A repeated request is answered from the ledger, never dispatched."""

        return OpResult(
            "none",
            False,
            _ATTEMPT_ANSWERS[attempt.state],
            data=item,
        )

    def submit(
        self,
        principal: Principal,
        draft_id: str,
        confirmation_id: str | None = None,
        *,
        attempt_id: str | None = None,
    ) -> OpResult[ApplicationDraft]:
        """CAREER SUBMIT, bound to this exact manifest, at most once. Review
        first: without a trusted submission adapter this always fails closed.
        ``attempt_id`` asks about an existing attempt; it never dispatches."""

        if attempt_id is not None:
            return self._submission_status(principal, draft_id, attempt_id)
        with self._attempts.lock:
            reserved = self._reserve_submission(principal, draft_id, confirmation_id)
        if not isinstance(reserved, _Reserved):
            return reserved
        submitter, package, guard = reserved.item
        try:
            result: object = submitter.submit(package, guard)
        except Exception:
            result = None  # a lost response is not proof that nothing happened
        with self._attempts.lock:
            outcome = (
                interpret(result, reserved.attempt, reserved.destination_ok)
                if result is not None
                else Interpretation(_UNKNOWN, None, "adapter_error")
            )
            return self._settle_submission(
                principal, reserved.attempt, outcome, reserved.confirmation_id
            )

    def _submission_status(
        self, principal: Principal, draft_id: str, attempt_id: str
    ) -> OpResult[ApplicationDraft]:
        draft = self._draft(principal, draft_id)
        attempt = self._attempts.get(attempt_id)
        if (
            draft is None
            or attempt is None
            or attempt.action is not ExternalAction.SUBMIT
            or attempt.item_id != draft_id
            or attempt.owner_id != principal.id
        ):
            return OpResult("deny", False, "attempt_not_found")
        return self._attempt_answer(attempt, draft)

    def _reserve_submission(
        self, principal: Principal, draft_id: str, confirmation_id: str | None
    ) -> (
        OpResult[ApplicationDraft]
        | _Reserved[tuple[SubmissionAdapter, SubmissionPackage, DestinationGuard]]
    ):
        """Steps 1-8 under the single-flight lock: freshness, destination,
        unresolved questions, manifest, permissions + binding, one-time
        consumption, attempt IN_FLIGHT. Nothing external happens here."""

        op = CareerOperation.SUBMIT
        draft = self._draft(principal, draft_id)
        if draft is None:
            return self._denied(op, principal, "deny", "draft_not_found", None)
        existing = self._attempts.blocking(ExternalAction.SUBMIT, draft_id)
        if existing is not None:
            return self._attempt_answer(existing, draft)
        opportunity = self.repository.get_opportunity(draft.opportunity_id)
        if opportunity is None:
            return OpResult("allow", False, "opportunity_not_found")
        if (
            draft.state is not S.APPROVED_FOR_SUBMISSION
            or draft.approved_binding is None
        ):
            return OpResult("deny", False, "not_approved_for_submission")
        if self._submitter is None:
            return OpResult("deny", False, "submission_unavailable")
        now = self._clock()
        documents = self._documents(draft)
        problems = list(
            blocking_reasons(draft, documents, opportunity, now, require_approval=True)
        )
        problem = destination_problem(opportunity)
        if problem:
            problems.append(problem)
        if problems:
            blocked = advance(draft, S.BLOCKED, now).model_copy(
                update={"last_failure": problems[0], "approved_binding": None}
            )
            self.repository.put_draft(blocked)
            self._record(
                op,
                principal,
                "none",
                "blocked",
                opportunity_id=draft.opportunity_id,
                item_id=draft_id,
                version=draft.version,
                reason_code=blocked.last_failure,
            )
            return OpResult("deny", False, blocked.last_failure)
        guard = destination_guard(opportunity)
        manifest = submission_manifest(draft, documents, opportunity)
        if manifest.digest != draft.approved_binding:
            self._record(
                op,
                principal,
                "none",
                "rejected",
                opportunity_id=draft.opportunity_id,
                item_id=draft_id,
                reason_code="package_changed",
            )
            return OpResult("deny", False, "package_changed")
        questions = {q.question_id: q.text for q in draft.questions}
        package = SubmissionPackage(
            attempt=AttemptReference(
                "pending",
                ExternalAction.SUBMIT,
                draft.opportunity_id,
                draft_id,
                manifest.digest,
            ),
            opportunity_id=draft.opportunity_id,
            draft_id=draft_id,
            draft_version=draft.version,
            destination=guard.url,
            documents=tuple(
                SubmissionDocument(
                    d.document_id,
                    d.kind.value,
                    d.version,
                    d.sha256,
                    d.text.encode("utf-8"),
                )
                for d in documents
            ),
            answers={a.question_id: a.text for a in draft.answers},
            manifest_hash=manifest.digest,
        )
        if not package_matches(package, manifest, questions):
            return OpResult("deny", False, "package_changed")
        scope = submission_scope(draft)
        target = f"manifest:{manifest.digest}"
        audit = {
            "opportunity_id": draft.opportunity_id,
            "item_id": draft_id,
            "version": draft.version,
            "manifest_hash": manifest.digest,
        }
        if confirmation_id is None:
            allowed, permission, reason, pending = self._authorize(
                principal, PermissionAction.SUBMIT, scope, target=target
            )
            return self._denied(
                op,
                principal,
                permission,
                reason if not allowed else "confirmation_required",
                pending,
                confirmation_id=pending,
                **audit,
            )
        if not self._attempts.has_capacity():
            return OpResult("deny", False, "attempt_ledger_full")
        allowed, permission, reason, pending = self._authorize(
            principal,
            PermissionAction.SUBMIT,
            scope,
            confirmation_id=confirmation_id,
            target=target,
        )
        if not allowed:
            return self._denied(
                op,
                principal,
                permission,
                reason,
                pending,
                confirmation_id=pending or confirmation_id,
                **audit,
            )
        attempt = self._attempts.start(
            action=ExternalAction.SUBMIT,
            owner_id=principal.id,
            opportunity_id=draft.opportunity_id,
            item_id=draft_id,
            item_version=draft.version,
            manifest_hash=manifest.digest,
            now=now,
        )
        submitting = advance(draft, S.SUBMITTING, now).model_copy(
            update={"attempt_id": attempt.attempt_id, "last_failure": None}
        )
        self.repository.put_draft(submitting)
        self._record(
            op,
            principal,
            permission,
            "in_flight",
            confirmation_id=confirmation_id,
            attempt_id=attempt.attempt_id,
            **audit,
        )
        return _Reserved(
            attempt,
            (
                self._submitter,
                replace(package, attempt=attempt.reference),
                guard,
            ),
            permission,
            confirmation_id,
            guard.allows,
        )

    def _settle_submission(
        self,
        principal: Principal,
        attempt: ExternalActionAttempt,
        outcome: Interpretation,
        confirmation_id: str | None,
    ) -> OpResult[ApplicationDraft]:
        """Apply a (verified or unknown) outcome to the ledger and the draft."""

        op = CareerOperation.SUBMIT
        now = self._clock()
        draft = self.repository.get_draft(attempt.item_id)
        if draft is None or draft.attempt_id != attempt.attempt_id:
            return OpResult("deny", False, "attempt_not_found")
        if outcome.status is _UNKNOWN and draft.state is S.OUTCOME_UNKNOWN:
            return OpResult("allow", False, "outcome_still_unknown", data=draft)
        self._attempts.finish(attempt.attempt_id, outcome, now)
        audit = {
            "opportunity_id": draft.opportunity_id,
            "item_id": draft.draft_id,
            "version": draft.version,
            "attempt_id": attempt.attempt_id,
            "manifest_hash": attempt.manifest_hash,
            "confirmation_id": confirmation_id,
        }
        if outcome.status is ExternalActionStatus.VERIFIED_SUCCESS:
            done = advance(draft, S.SUBMITTED, now).model_copy(
                update={
                    "submitted_at": now,
                    "submission_reference": clean_text(outcome.receipt_id or "", 200)
                    or None,
                    "last_failure": None,
                }
            )
            self.repository.put_draft(done)
            self.repository.put_follow_up(
                schedule("fu_" + uuid.uuid4().hex[:20], draft.opportunity_id, None, now)
            )
            self._record(op, principal, "allow", "submitted", **audit)
            return OpResult("allow", True, data=done)
        if outcome.status is ExternalActionStatus.VERIFIED_FAILURE:
            failed = advance(draft, S.SUBMISSION_FAILED, now).model_copy(
                update={"last_failure": outcome.reason_code, "approved_binding": None}
            )
            self.repository.put_draft(failed)
            self._record(
                op,
                principal,
                "allow",
                "failed",
                reason_code=failed.last_failure,
                **audit,
            )
            return OpResult("allow", False, failed.last_failure, data=failed)
        unknown = advance(draft, S.OUTCOME_UNKNOWN, now).model_copy(
            update={"last_failure": outcome.reason_code, "approved_binding": None}
        )
        self.repository.put_draft(unknown)
        self._record(
            op,
            principal,
            "allow",
            "outcome_unknown",
            reason_code=unknown.last_failure,
            **audit,
        )
        return OpResult("allow", False, "outcome_unknown", data=unknown)

    def reconcile_submission(
        self, principal: Principal, draft_id: str
    ) -> OpResult[ApplicationDraft]:
        """Ask the trusted adapter (read-only) what happened to an attempt
        whose outcome is unknown. Only a matching, trusted result changes
        anything; a mismatched or unknown answer leaves it unresolved."""

        denied = self._gate(
            CareerOperation.SUBMIT,
            principal,
            PermissionAction.UPDATE,
            self._scope("applications", draft_id),
        )
        if denied is not None:
            return denied
        with self._attempts.lock:
            draft = self._draft(principal, draft_id)
            if draft is None:
                return OpResult("allow", False, "draft_not_found")
            attempt = self._attempts.get(draft.attempt_id or "")
            if (
                draft.state is not S.OUTCOME_UNKNOWN
                or attempt is None
                or attempt.state is not AttemptState.OUTCOME_UNKNOWN
            ):
                return OpResult("allow", False, "nothing_to_reconcile")
            opportunity = self.repository.get_opportunity(draft.opportunity_id)
            if self._submitter is None or opportunity is None:
                return OpResult("deny", False, "submission_unavailable")
            submitter, guard = self._submitter, destination_guard(opportunity)
        try:
            result: object = submitter.reconcile(attempt.reference)
        except Exception:
            result = None
        with self._attempts.lock:
            outcome = (
                interpret(result, attempt, guard.allows)
                if result is not None
                else Interpretation(_UNKNOWN, None, "adapter_error")
            )
            current = self._attempts.get(attempt.attempt_id)
            if current is None or current.state is not AttemptState.OUTCOME_UNKNOWN:
                return OpResult("allow", False, "nothing_to_reconcile")
            return self._settle_submission(principal, attempt, outcome, None)

    @_serialized
    def release_unknown_submission(
        self, principal: Principal, draft_id: str, confirmation_id: str | None = None
    ) -> OpResult[ApplicationDraft]:
        """The owner explicitly accepts that an unknown attempt MAY have been
        submitted and sends the draft back to review. HIGH risk: CAREER SUBMIT
        with a confirmation bound to ``release:<attempt_id>``. A new attempt
        then needs a new approval and a new SUBMIT confirmation."""

        op = CareerOperation.SUBMIT
        draft = self._draft(principal, draft_id)
        if draft is None:
            return OpResult("deny", False, "draft_not_found")
        attempt = self._attempts.get(draft.attempt_id or "")
        if (
            draft.state is not S.OUTCOME_UNKNOWN
            or attempt is None
            or attempt.state is not AttemptState.OUTCOME_UNKNOWN
        ):
            return OpResult("deny", False, "nothing_to_release")
        allowed, permission, reason, pending = self._authorize(
            principal,
            PermissionAction.SUBMIT,
            submission_scope(draft),
            confirmation_id=confirmation_id,
            target=f"release:{attempt.attempt_id}",
        )
        if not allowed or confirmation_id is None:
            return self._denied(
                op,
                principal,
                permission,
                reason or "confirmation_required",
                pending,
                item_id=draft_id,
                attempt_id=attempt.attempt_id,
            )
        self._attempts.release(attempt.attempt_id, self._clock())
        reopened = advance(draft, S.DRAFTING, self._clock())
        updated = self._changed(reopened, last_failure="released_by_owner")
        self.repository.put_draft(updated)
        self._record(
            op,
            principal,
            permission,
            "released",
            opportunity_id=draft.opportunity_id,
            item_id=draft_id,
            version=updated.version,
            attempt_id=attempt.attempt_id,
            confirmation_id=confirmation_id,
        )
        return OpResult(permission, True, data=updated)

    @_serialized
    def withdraw(
        self, principal: Principal, draft_id: str, confirmation_id: str | None = None
    ) -> OpResult[ApplicationDraft]:
        draft = self._draft(principal, draft_id)
        if draft is None:
            return OpResult("deny", False, "draft_not_found")
        allowed, permission, reason, pending = self._authorize(
            principal,
            PermissionAction.DELETE,
            self._scope("applications", draft_id),
            confirmation_id=confirmation_id,
        )
        if not allowed:
            return self._denied(
                CareerOperation.WITHDRAW,
                principal,
                permission,
                reason,
                pending,
                item_id=draft_id,
            )
        try:
            done = advance(draft, S.WITHDRAWN, self._clock())
        except TransitionRefused as error:
            return OpResult(permission, False, error.code)
        self.repository.put_draft(done)
        self._record(
            CareerOperation.WITHDRAW,
            principal,
            permission,
            "ok",
            opportunity_id=draft.opportunity_id,
            item_id=draft_id,
        )
        return OpResult(permission, True, data=done)

    # -------------------------------------------------------- contacts/outreach

    @_serialized
    def add_contact(
        self,
        principal: Principal,
        *,
        name: str,
        role: ContactRole,
        organization: str,
        source_kind: SourceKind,
        url: str,
        quote: str,
        email: str | None = None,
        opportunity_id: str | None = None,
        research_topics: Sequence[str] = (),
    ) -> OpResult[Contact]:
        denied = self._gate(
            CareerOperation.CONTACT,
            principal,
            PermissionAction.CREATE,
            self._scope("contacts"),
        )
        if denied is not None:
            return denied
        try:
            contact = build_contact(
                contact_id="ct_" + uuid.uuid4().hex[:20],
                name=name,
                role=role,
                organization=organization,
                source_kind=source_kind,
                url=url,
                quote=quote,
                retrieved_at=self._clock(),
                email=email,
                opportunity_id=opportunity_id,
                research_topics=research_topics,
            )
            self.repository.put_contact(contact)
        except (ContactRejected, RepositoryFull) as error:
            self._record(
                CareerOperation.CONTACT,
                principal,
                "allow",
                "rejected",
                reason_code=error.code,
            )
            return OpResult("allow", False, error.code)
        self._record(
            CareerOperation.CONTACT,
            principal,
            "allow",
            "ok",
            item_id=contact.contact_id,
            source=source_kind.value,
        )
        return OpResult("allow", True, data=contact)

    @_serialized
    def draft_outreach(
        self,
        principal: Principal,
        *,
        contact_id: str,
        kind: OutreachKind,
        channel: OutreachChannel,
        opportunity_id: str | None = None,
        note: str | None = None,
    ) -> OpResult[OutreachDraft]:
        denied = self._gate(
            CareerOperation.OUTREACH,
            principal,
            PermissionAction.CREATE,
            self._scope("outreach"),
        )
        if denied is not None:
            return denied
        contact = self.repository.get_contact(contact_id)
        if contact is None:
            return OpResult("allow", False, "contact_not_found")
        opportunity = (
            self.repository.get_opportunity(opportunity_id) if opportunity_id else None
        )
        if opportunity_id and opportunity is None:
            return OpResult("allow", False, "opportunity_not_found")
        if note and _is_secret(note):
            return OpResult("allow", False, "secret_detected")
        claims = self._claims(principal)
        if claims is None:
            return OpResult("allow", False, "evidence_unavailable")
        fit = (
            analyze_fit(principal, opportunity, self._evidence) if opportunity else None
        )
        topics = contact.research_topics or (
            opportunity.research_topics if opportunity else ()
        )
        alignment = (
            research_alignment(principal, topics, self._evidence)
            if kind in (OutreachKind.SUPERVISOR, OutreachKind.PHD_INQUIRY) and topics
            else None
        )
        draft = build_outreach(
            principal,
            outreach_id="or_" + uuid.uuid4().hex[:20],
            kind=kind,
            channel=channel,
            contact=contact,
            opportunity=opportunity,
            fit=fit,
            alignment=alignment,
            note_text=note,
            claims=claims,
            checker=self._checker,
            now=self._clock(),
        )
        if not draft.unresolved:
            draft = draft.model_copy(
                update={"state": OutreachState.READY_FOR_OWNER_REVIEW}
            )
        self.repository.put_outreach(draft)
        self._record(
            CareerOperation.OUTREACH,
            principal,
            "allow",
            draft.state.value,
            opportunity_id=opportunity_id,
            item_id=draft.outreach_id,
            version=draft.version,
        )
        return OpResult("allow", True, data=draft)

    @_serialized
    def approve_outreach(
        self, principal: Principal, outreach_id: str
    ) -> OpResult[OutreachDraft]:
        denied = self._gate(
            CareerOperation.APPROVE,
            principal,
            PermissionAction.UPDATE,
            self._scope("outreach", outreach_id),
        )
        if denied is not None:
            return denied
        draft = self.repository.get_outreach(outreach_id)
        if draft is None:
            return OpResult("allow", False, "outreach_not_found")
        if draft.unresolved:
            return OpResult("allow", False, "unresolved_lines")
        if draft.state is not OutreachState.READY_FOR_OWNER_REVIEW:
            return OpResult("allow", False, "not_ready_for_approval")
        approved = draft.model_copy(update={"state": OutreachState.APPROVED})
        self.repository.put_outreach(approved)
        self._record(
            CareerOperation.APPROVE,
            principal,
            "allow",
            "outreach",
            item_id=outreach_id,
            version=draft.version,
        )
        return OpResult("allow", True, data=approved)

    def send_outreach(
        self,
        principal: Principal,
        outreach_id: str,
        confirmation_id: str | None = None,
        email_confirmation_id: str | None = None,
        *,
        attempt_id: str | None = None,
    ) -> OpResult[OutreachDraft]:
        """CAREER SEND (bound to this exact message manifest) AND the e-mail
        tool's own GMAIL SEND, each with its own confirmation, at most once.
        Both confirmations are requested before either is consumed, and both
        are consumed before anything is dispatched."""

        if attempt_id is not None:
            attempt = self._attempts.get(attempt_id)
            draft = self.repository.get_outreach(outreach_id)
            if (
                draft is None
                or attempt is None
                or attempt.action is not ExternalAction.SEND
                or attempt.item_id != outreach_id
                or attempt.owner_id != principal.id
            ):
                return OpResult("deny", False, "attempt_not_found")
            return self._attempt_answer(attempt, draft)
        with self._attempts.lock:
            reserved = self._reserve_send(
                principal, outreach_id, confirmation_id, email_confirmation_id
            )
        if not isinstance(reserved, _Reserved):
            return reserved
        email, message = reserved.item
        try:
            result: object = email.send(message)
        except Exception:
            result = None  # a transport error after hand-over proves nothing
        with self._attempts.lock:
            outcome = (
                interpret(result, reserved.attempt, reserved.destination_ok)
                if result is not None
                else Interpretation(_UNKNOWN, None, "adapter_error")
            )
            return self._settle_send(
                principal, reserved.attempt, outcome, reserved.confirmation_id
            )

    def _send_manifest(
        self, draft: OutreachDraft, contact: Contact, recipient: str
    ) -> SendManifest:
        return SendManifest(
            outreach_id=draft.outreach_id,
            outreach_version=draft.version,
            opportunity_id=draft.opportunity_id,
            contact_id=contact.contact_id,
            channel=draft.channel.value,
            recipient=recipient,
            subject=draft.subject,
            body=draft.body,
        )

    def _reserve_send(
        self,
        principal: Principal,
        outreach_id: str,
        confirmation_id: str | None,
        email_confirmation_id: str | None,
    ) -> OpResult[OutreachDraft] | _Reserved[tuple[EmailTool, OutboundEmail]]:
        op = CareerOperation.SEND
        draft = self.repository.get_outreach(outreach_id)
        if draft is None:
            return OpResult("deny", False, "outreach_not_found")
        existing = self._attempts.blocking(ExternalAction.SEND, outreach_id)
        if existing is not None:
            return self._attempt_answer(existing, draft)
        if draft.channel not in SENDABLE_CHANNELS:
            return OpResult("deny", False, "drafts_only_channel")
        if draft.state is not OutreachState.APPROVED:
            return OpResult("deny", False, "not_approved")
        if self._email is None:
            return OpResult("deny", False, "email_unavailable")
        contact = self.repository.get_contact(draft.contact_id)
        if contact is None or not contact.email:
            return OpResult("deny", False, "no_verified_email")
        recipient = contact.email
        manifest = self._send_manifest(draft, contact, recipient)
        scope = self._scope("outreach", outreach_id, f"v{draft.version}")
        mail_scope = PermissionScope.from_path("mail/outbox")
        target = f"manifest:{manifest.digest}"
        audit: dict[str, Any] = {
            "opportunity_id": draft.opportunity_id,
            "item_id": outreach_id,
            "version": draft.version,
            "manifest_hash": manifest.digest,
        }
        if confirmation_id is None:
            allowed, permission, reason, pending = self._authorize(
                principal, PermissionAction.SEND, scope, target=target
            )
            if not allowed:
                return self._denied(op, principal, permission, reason, pending, **audit)
        if email_confirmation_id is None:
            allowed, permission, reason, pending = self._authorize(
                principal,
                PermissionAction.SEND,
                mail_scope,
                target=target,
                resource=PermissionResource.GMAIL,
            )
            if not allowed:
                return self._denied(
                    op,
                    principal,
                    permission,
                    reason,
                    pending,
                    source="email_tool",
                    **audit,
                )
        if confirmation_id is None or email_confirmation_id is None:
            return OpResult("deny", False, "confirmation_required")
        if not self._attempts.has_capacity():
            return OpResult("deny", False, "attempt_ledger_full")
        allowed, permission, reason, pending = self._authorize(
            principal,
            PermissionAction.SEND,
            scope,
            confirmation_id=confirmation_id,
            target=target,
        )
        if not allowed:
            return self._denied(op, principal, permission, reason, pending, **audit)
        allowed, permission, reason, pending = self._authorize(
            principal,
            PermissionAction.SEND,
            mail_scope,
            confirmation_id=email_confirmation_id,
            target=target,
            resource=PermissionResource.GMAIL,
        )
        if not allowed:
            return self._denied(
                op, principal, permission, reason, pending, source="email_tool", **audit
            )
        attempt = self._attempts.start(
            action=ExternalAction.SEND,
            owner_id=principal.id,
            opportunity_id=draft.opportunity_id,
            item_id=outreach_id,
            item_version=draft.version,
            manifest_hash=manifest.digest,
            now=self._clock(),
        )
        self.repository.put_outreach(
            draft.model_copy(
                update={
                    "state": OutreachState.SENDING,
                    "attempt_id": attempt.attempt_id,
                }
            )
        )
        self._record(
            op,
            principal,
            "allow",
            "in_flight",
            confirmation_id=confirmation_id,
            attempt_id=attempt.attempt_id,
            **audit,
        )
        message = OutboundEmail(
            attempt.reference, recipient, draft.subject, draft.body, manifest.digest
        )
        canonical = recipient.strip().casefold()
        return _Reserved(
            attempt,
            (self._email, message),
            "allow",
            confirmation_id,
            lambda reached: (
                reached is not None and reached.strip().casefold() == canonical
            ),
        )

    def _settle_send(
        self,
        principal: Principal,
        attempt: ExternalActionAttempt,
        outcome: Interpretation,
        confirmation_id: str | None,
    ) -> OpResult[OutreachDraft]:
        op = CareerOperation.SEND
        now = self._clock()
        draft = self.repository.get_outreach(attempt.item_id)
        if draft is None or draft.attempt_id != attempt.attempt_id:
            return OpResult("deny", False, "attempt_not_found")
        if outcome.status is _UNKNOWN and draft.state is OutreachState.OUTCOME_UNKNOWN:
            return OpResult("allow", False, "outcome_still_unknown", data=draft)
        self._attempts.finish(attempt.attempt_id, outcome, now)
        audit = {
            "opportunity_id": draft.opportunity_id,
            "item_id": draft.outreach_id,
            "version": draft.version,
            "attempt_id": attempt.attempt_id,
            "manifest_hash": attempt.manifest_hash,
            "confirmation_id": confirmation_id,
        }
        if outcome.status is ExternalActionStatus.VERIFIED_SUCCESS:
            sent = draft.model_copy(
                update={
                    "state": OutreachState.SENT,
                    "sent_at": now,
                    "last_failure": None,
                }
            )
            self.repository.put_outreach(sent)
            if draft.opportunity_id:
                existing = next(
                    (
                        f
                        for f in self.repository.list_follow_ups()
                        if f.opportunity_id == draft.opportunity_id
                        and f.contact_id == draft.contact_id
                    ),
                    None,
                )
                self.repository.put_follow_up(
                    record_sent(existing, now)
                    if existing
                    else schedule(
                        "fu_" + uuid.uuid4().hex[:20],
                        draft.opportunity_id,
                        draft.contact_id,
                        now,
                    )
                )
            self._record(op, principal, "allow", "sent", **audit)
            return OpResult("allow", True, data=sent)
        verified_failure = outcome.status is ExternalActionStatus.VERIFIED_FAILURE
        settled = draft.model_copy(
            update={
                "state": OutreachState.SEND_FAILED
                if verified_failure
                else OutreachState.OUTCOME_UNKNOWN,
                "last_failure": outcome.reason_code,
            }
        )
        self.repository.put_outreach(settled)
        status = "failed" if verified_failure else "outcome_unknown"
        self._record(
            op, principal, "allow", status, reason_code=outcome.reason_code, **audit
        )
        reason = outcome.reason_code if verified_failure else "outcome_unknown"
        return OpResult("allow", False, reason, data=settled)

    def reconcile_outreach(
        self, principal: Principal, outreach_id: str
    ) -> OpResult[OutreachDraft]:
        """Read-only lookup of an outreach send whose outcome is unknown."""

        denied = self._gate(
            CareerOperation.SEND,
            principal,
            PermissionAction.UPDATE,
            self._scope("outreach", outreach_id),
        )
        if denied is not None:
            return denied
        with self._attempts.lock:
            draft = self.repository.get_outreach(outreach_id)
            attempt = self._attempts.get(draft.attempt_id or "") if draft else None
            if (
                draft is None
                or draft.state is not OutreachState.OUTCOME_UNKNOWN
                or attempt is None
                or attempt.state is not AttemptState.OUTCOME_UNKNOWN
                or attempt.owner_id != principal.id
            ):
                return OpResult("allow", False, "nothing_to_reconcile")
            contact = self.repository.get_contact(draft.contact_id)
            if self._email is None or contact is None or not contact.email:
                return OpResult("deny", False, "email_unavailable")
            email, canonical = self._email, contact.email.strip().casefold()
        try:
            result: object = email.reconcile(attempt.reference)
        except Exception:
            result = None
        with self._attempts.lock:
            outcome = (
                interpret(
                    result,
                    attempt,
                    lambda reached: (
                        reached is not None and reached.strip().casefold() == canonical
                    ),
                )
                if result is not None
                else Interpretation(_UNKNOWN, None, "adapter_error")
            )
            current = self._attempts.get(attempt.attempt_id)
            if current is None or current.state is not AttemptState.OUTCOME_UNKNOWN:
                return OpResult("allow", False, "nothing_to_reconcile")
            return self._settle_send(principal, attempt, outcome, None)

    @_serialized
    def release_unknown_outreach(
        self, principal: Principal, outreach_id: str, confirmation_id: str | None = None
    ) -> OpResult[OutreachDraft]:
        """The owner accepts that an unknown send MAY have been delivered and
        returns the message to review (new version: new approval and new
        confirmations are needed). CAREER SEND bound to ``release:<attempt>``."""

        draft = self.repository.get_outreach(outreach_id)
        attempt = self._attempts.get(draft.attempt_id or "") if draft else None
        if (
            draft is None
            or draft.state is not OutreachState.OUTCOME_UNKNOWN
            or attempt is None
            or attempt.state is not AttemptState.OUTCOME_UNKNOWN
        ):
            return OpResult("deny", False, "nothing_to_release")
        allowed, permission, reason, pending = self._authorize(
            principal,
            PermissionAction.SEND,
            self._scope("outreach", outreach_id, f"v{draft.version}"),
            confirmation_id=confirmation_id,
            target=f"release:{attempt.attempt_id}",
        )
        if not allowed or confirmation_id is None:
            return self._denied(
                CareerOperation.SEND,
                principal,
                permission,
                reason or "confirmation_required",
                pending,
                item_id=outreach_id,
                attempt_id=attempt.attempt_id,
            )
        self._attempts.release(attempt.attempt_id, self._clock())
        reopened = draft.model_copy(
            update={
                "state": OutreachState.READY_FOR_OWNER_REVIEW,
                "version": draft.version + 1,
                "last_failure": "released_by_owner",
            }
        )
        self.repository.put_outreach(reopened)
        self._record(
            CareerOperation.SEND,
            principal,
            permission,
            "released",
            item_id=outreach_id,
            version=reopened.version,
            attempt_id=attempt.attempt_id,
            confirmation_id=confirmation_id,
        )
        return OpResult(permission, True, data=reopened)

    @_serialized
    def draft_follow_up(
        self, principal: Principal, follow_up_id: str
    ) -> OpResult[OutreachDraft]:
        denied = self._gate(
            CareerOperation.FOLLOW_UP,
            principal,
            PermissionAction.CREATE,
            self._scope("outreach"),
        )
        if denied is not None:
            return denied
        follow_up = next(
            (
                f
                for f in self.repository.list_follow_ups()
                if f.follow_up_id == follow_up_id
            ),
            None,
        )
        if follow_up is None:
            return OpResult("allow", False, "follow_up_not_found")
        now = self._clock()
        current = state(follow_up, now)
        if current is FollowUpState.DRAFTED:
            existing = next(
                (
                    o
                    for o in self.repository.list_outreach()
                    if o.kind is OutreachKind.FOLLOW_UP
                    and o.opportunity_id == follow_up.opportunity_id
                    and o.state is not OutreachState.SENT
                ),
                None,
            )
            return OpResult(
                "allow",
                existing is not None,
                None if existing else "follow_up_cooldown",
                data=existing,
            )
        if current is not FollowUpState.FOLLOW_UP_DUE:
            return OpResult("allow", False, f"follow_up_{current.value}")
        contact = (
            self.repository.get_contact(follow_up.contact_id)
            if follow_up.contact_id
            else None
        )
        if contact is None:
            return OpResult("allow", False, "no_contact_for_follow_up")
        result = self.draft_outreach(
            principal,
            contact_id=contact.contact_id,
            kind=OutreachKind.FOLLOW_UP,
            channel=OutreachChannel.EMAIL,
            opportunity_id=follow_up.opportunity_id,
        )
        if result.ok:
            self.repository.put_follow_up(record_draft(follow_up, now))
        return result

    # ----------------------------------------------------------- preferences

    @_serialized
    def set_preferences(
        self, principal: Principal, prefs: CareerPreferences
    ) -> OpResult[CareerPreferences]:
        denied = self._gate(
            CareerOperation.PREFERENCES,
            principal,
            PermissionAction.UPDATE,
            self._scope("preferences"),
        )
        if denied is not None:
            return denied
        values = [
            *prefs.preferred_roles,
            *prefs.locations,
            *prefs.role_types,
            prefs.salary_preference or "",
        ]
        if _is_secret(*values):
            return OpResult("allow", False, "secret_detected")
        self.repository.set_preferences(principal.id, prefs)
        self._record(CareerOperation.PREFERENCES, principal, "allow", "ok")
        return OpResult("allow", True, data=prefs)

    @_serialized
    def expire_stale(self, principal: Principal) -> OpResult[int]:
        """Mark applications whose opportunity is closed or past its deadline."""

        denied = self._gate(
            CareerOperation.EDIT,
            principal,
            PermissionAction.UPDATE,
            self._scope("applications"),
        )
        if denied is not None:
            return denied
        now = self._clock()
        count = 0
        for draft in self.repository.list_drafts():
            if draft.owner_id != principal.id or draft.state in _CLOSED:
                continue
            opportunity = self.repository.get_opportunity(draft.opportunity_id)
            if opportunity and {"opportunity_closed", "deadline_passed"} & set(
                freshness_problems(draft, opportunity, now)
            ):
                self.repository.put_draft(advance(draft, S.EXPIRED, now))
                count += 1
        return OpResult("allow", True, data=count)


__all__ = [
    "CareerService",
    "EmailTool",
    "OpResult",
    "OutboundEmail",
    "Overview",
    "ReviewItem",
]
