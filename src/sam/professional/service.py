"""ProfessionalService: the one Sam-owned entry point to Professional Intelligence.

Every operation is authorized by the PermissionEngine under the distinct
``PermissionResource.PROFESSIONAL`` (never KNOWLEDGE):

* READ   (LOW)    search, evidence_for, lists, timeline, gaps, sources
* WRITE  (MEDIUM) ingest / refresh an owner-selected source
* UPDATE (MEDIUM) confirm or reject a claim, resolve a conflict, set a
                  source's privacy class
* DELETE (HIGH + confirmation) remove a source and its derived evidence

The service takes no arbitrary path, SQL or expression; documents arrive as
bytes from the owner's selection and are parsed by Knowledge's pipeline. A
model can never call the write, update or delete operations: the model-facing
boundary (``sam.professional.tool``) exposes reads only.

Domain separation: this module never imports ``sam.memory``. Nothing is copied
into Memory, and no raw document is copied into Knowledge.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import TypeVar
from uuid import uuid4

from pydantic import ValidationError

from sam.models.models import PrivacyClass
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    DecisionOutcome,
    DenialReason,
    PermissionAction,
    PermissionRequest,
    PermissionResource,
    PermissionScope,
    Principal,
)
from sam.professional import claims as claim_policy
from sam.professional.audit import (
    ProfessionalAuditSink,
    ProfessionalOperation,
    new_event,
)
from sam.professional.candidates import CandidateExtractor, CandidateOutcome
from sam.professional.extract import (
    EXTRACTOR_VERSION,
    ExtractionError,
    ExtractionResult,
    extract,
)
from sam.professional.gaps import ProfileGap, analyze_gaps
from sam.professional.identity import OwnerProfessionalIdentity
from sam.professional.lineage import fingerprints, independence_key
from sam.professional.matching import RequirementEvidence, match_requirement
from sam.professional.models import (
    ATTRIBUTE_ALLOWLIST,
    CATEGORY_SENSITIVITY,
    MAX_QUERY_LENGTH,
    ClaimCategory,
    ClaimReview,
    ClaimReviewState,
    Conflict,
    ConflictKind,
    EvidenceNature,
    EvidenceRef,
    EvidenceStrength,
    ProfessionalClaim,
    Resolution,
    SourceRecord,
    SourceType,
    new_claim_id,
    new_evidence_id,
    new_source_id,
    utc_now,
)
from sam.professional.profile import ClaimView, ExperienceSummary, Profile
from sam.professional.reader import (
    DocumentReader,
    KnowledgePipelineReader,
    SourceFileError,
    read_source_file,
    safe_label,
)
from sam.professional.repository import IngestionBatch, ProfessionalRepository
from sam.professional.views import (
    EducationView,
    ExperienceAnswer,
    IngestSummary,
    OpResult,
    ProfileSnapshot,
    PublicationView,
    RemovalSummary,
    SourceView,
)
from sam.professional.vocabulary import find_mentions, normalize_skill

T = TypeVar("T")

_R = PermissionResource.PROFESSIONAL
_SCOPE_READ = PermissionScope.identifier("profile:read")
_SCOPE_INGEST = PermissionScope.identifier("profile:ingest")
_SCOPE_REVIEW = PermissionScope.identifier("profile:review")

_ALLOWED_SOURCE_CLASSES = (
    PrivacyClass.PUBLIC,
    PrivacyClass.PERSONAL,
    PrivacyClass.PRIVATE,
)
_SUFFIX_TYPES = {
    ".pdf": "pdf",
    ".txt": "txt",
    ".md": "markdown",
    ".markdown": "markdown",
    ".json": "json",
    ".csv": "csv",
}
_STRENGTH_RANK = {
    EvidenceStrength.CORROBORATED: 0,
    EvidenceStrength.SINGLE_SOURCE: 1,
    EvidenceStrength.NONE: 2,
}
# Which existing evidence an owner attestation should name as its basis: what
# the owner was actually asked to vouch for comes first.
_BASIS_ORDER = {
    EvidenceNature.MODEL_CANDIDATE: 0,
    EvidenceNature.INFERRED_RELATIONSHIP: 1,
    EvidenceNature.EXPLICIT_SOURCE: 2,
}


def _rank(view: ClaimView) -> tuple[int, int]:
    return (0 if view.accepted else 1, _STRENGTH_RANK[view.strength])


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(p.strip() for p in (value or "").split(";") if p.strip())


class ProfessionalService:
    def __init__(
        self,
        *,
        repository: ProfessionalRepository,
        permission_engine: PermissionEngine,
        reader: DocumentReader | None = None,
        audit_sink: ProfessionalAuditSink | None = None,
        candidate_extractor: CandidateExtractor | None = None,
        owner_identity: OwnerProfessionalIdentity | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repo = repository
        self._permissions = permission_engine
        self._reader = reader if reader is not None else KnowledgePipelineReader()
        self._audit = audit_sink
        self._candidates = candidate_extractor
        # Trusted local configuration only, frozen at construction: never
        # derived from a document or a model, and no operation can change it.
        self._identity = owner_identity
        self._clock = clock
        self._policy = claim_policy.ProfessionalClaimPolicy()
        self._write_lock = RLock()

    # ------------------------------------------------------ authorization

    def _authorize(
        self,
        principal: Principal,
        action: PermissionAction,
        scope: PermissionScope,
        confirmation_id: str | None = None,
    ) -> tuple[bool, str, str | None, str | None]:
        try:
            decision = self._permissions.evaluate(
                PermissionRequest(
                    principal=principal, action=action, resource=_R, scope=scope
                ),
                confirmation_id=confirmation_id,
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
        reason = (
            "confirmation_invalid"
            if decision.reason is DenialReason.CONFIRMATION_INVALID
            else "permission_denied"
        )
        return False, "deny", reason, None

    def _record(
        self,
        operation: ProfessionalOperation,
        principal: Principal,
        permission: str,
        status: str,
        **fields: object,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                new_event(
                    now=self._clock(),
                    operation=operation,
                    principal_id=principal.id,
                    permission_outcome=permission,
                    status=status,
                    **fields,  # type: ignore[arg-type]
                )
            )
        except Exception:  # auditing must never break an operation
            return

    def _profile(self) -> Profile:
        return Profile(
            claims=self._repo.list_claims(),
            evidence=self._repo.list_all_evidence(),
            sources=self._repo.list_sources(),
            resolutions=self._repo.list_resolutions(),
            today=self._clock().date(),
            reviews={r.claim_id: r.state for r in self._repo.list_reviews()},
        )

    def _read(
        self,
        principal: Principal,
        operation: ProfessionalOperation,
        build: Callable[[Profile], T],
    ) -> OpResult[T]:
        operation_id = uuid4().hex
        allowed, permission, reason, confirmation = self._authorize(
            principal, PermissionAction.READ, _SCOPE_READ
        )
        if not allowed:
            self._record(operation, principal, permission, "denied")
            return OpResult(permission, False, reason, confirmation, None, operation_id)
        try:
            data = build(self._profile())
        except Exception:
            self._record(
                operation,
                principal,
                permission,
                "failed",
                error_category="internal_error",
            )
            return OpResult(
                permission, False, "internal_error", None, None, operation_id
            )
        counts = {"results": len(data)} if isinstance(data, (tuple, list)) else None
        self._record(operation, principal, permission, "ok", counts=counts)
        return OpResult(permission, True, None, None, data, operation_id)

    # -------------------------------------------------------------- reads

    def get_claim(
        self, principal: Principal, claim_id: str
    ) -> OpResult[ClaimView | None]:
        return self._read(
            principal, ProfessionalOperation.READ, lambda p: p.view(claim_id)
        )

    def _list(
        self,
        principal: Principal,
        *categories: ClaimCategory,
        accepted_only: bool = False,
    ) -> OpResult[tuple[ClaimView, ...]]:
        return self._read(
            principal,
            ProfessionalOperation.READ,
            lambda p: _ordered(p.by_category(*categories, accepted_only=accepted_only)),
        )

    def get_skills(
        self, principal: Principal, *, accepted_only: bool = False
    ) -> OpResult[tuple[ClaimView, ...]]:
        return self._list(
            principal,
            ClaimCategory.SKILL,
            ClaimCategory.TECHNOLOGY,
            accepted_only=accepted_only,
        )

    def get_projects(self, principal: Principal) -> OpResult[tuple[ClaimView, ...]]:
        return self._list(principal, ClaimCategory.PROJECT)

    def get_research_topics(
        self, principal: Principal
    ) -> OpResult[tuple[ClaimView, ...]]:
        return self._list(principal, ClaimCategory.RESEARCH)

    def get_publications(
        self, principal: Principal
    ) -> OpResult[tuple[PublicationView, ...]]:
        return self._read(
            principal,
            ProfessionalOperation.READ,
            lambda p: tuple(
                _publication(v)
                for v in _ordered(p.by_category(ClaimCategory.PUBLICATION))
            ),
        )

    def get_education(
        self, principal: Principal
    ) -> OpResult[tuple[EducationView, ...]]:
        return self._read(
            principal,
            ProfessionalOperation.READ,
            lambda p: tuple(
                _education(v) for v in _ordered(p.by_category(ClaimCategory.EDUCATION))
            ),
        )

    def get_timeline(
        self, principal: Principal, *, max_sensitivity: PrivacyClass | None = None
    ) -> OpResult[ExperienceSummary]:
        return self._read(
            principal,
            ProfessionalOperation.READ,
            lambda p: p.timeline(max_sensitivity),
        )

    def get_profile_gaps(
        self, principal: Principal, *, max_sensitivity: PrivacyClass | None = None
    ) -> OpResult[tuple[ProfileGap, ...]]:
        return self._read(
            principal,
            ProfessionalOperation.READ,
            lambda p: analyze_gaps(p, max_sensitivity),
        )

    def get_conflicts(self, principal: Principal) -> OpResult[tuple[Conflict, ...]]:
        return self._read(principal, ProfessionalOperation.READ, lambda p: p.conflicts)

    def get_sources(self, principal: Principal) -> OpResult[tuple[SourceView, ...]]:
        return self._read(principal, ProfessionalOperation.READ, self._source_views)

    def get_profile(self, principal: Principal) -> OpResult[ProfileSnapshot]:
        def build(profile: Profile) -> ProfileSnapshot:
            views = _ordered(profile.views.values())
            return ProfileSnapshot(
                claims=views,
                publications=tuple(
                    _publication(v)
                    for v in views
                    if v.category is ClaimCategory.PUBLICATION
                ),
                education=tuple(
                    _education(v)
                    for v in views
                    if v.category is ClaimCategory.EDUCATION
                ),
                timeline=profile.timeline(),
                sources=self._source_views(profile),
                conflicts=profile.conflicts,
                gaps=analyze_gaps(profile),
            )

        return self._read(principal, ProfessionalOperation.READ, build)

    def _source_views(self, profile: Profile) -> tuple[SourceView, ...]:
        now = self._clock()
        out: list[SourceView] = []
        for source in sorted(profile.sources.values(), key=lambda s: s.source_id):
            accepted = pending = 0
            for claim_id, refs in profile.evidence.items():
                mine = [e for e in refs if e.source_id == source.source_id]
                if not mine:
                    continue
                if profile.views[claim_id].accepted:
                    accepted += 1
                elif all(e.nature is EvidenceNature.MODEL_CANDIDATE for e in mine):
                    pending += 1
            conflicts = sum(
                1
                for c in profile.conflicts
                if any(source.source_id in o.source_ids for o in c.options)
            )
            out.append(
                SourceView(
                    source_id=source.source_id,
                    label=source.label,
                    source_type=source.source_type,
                    source_family_id=profile.families[source.source_id],
                    version=source.version,
                    privacy_class=source.privacy_class,
                    freshness=source.freshness(now),
                    ingested_at=source.ingested_at.isoformat(),
                    refreshed_at=source.refreshed_at.isoformat(),
                    resource_type=source.resource_type,
                    accepted_claims=accepted,
                    pending_candidates=pending,
                    conflicts=conflicts,
                    candidate_extraction=source.candidate_extraction,
                )
            )
        return tuple(out)

    def search(
        self, principal: Principal, query: str
    ) -> OpResult[tuple[ClaimView, ...]]:
        text = query.strip()
        if not text or len(text) > MAX_QUERY_LENGTH:
            return OpResult("allow", False, "query_invalid")
        return self._read(
            principal, ProfessionalOperation.SEARCH, lambda p: _search(p, text)
        )

    def evidence_for(
        self, principal: Principal, requirement: str
    ) -> OpResult[RequirementEvidence]:
        text = requirement.strip()
        if not text or len(text) > MAX_QUERY_LENGTH:
            return OpResult("allow", False, "requirement_invalid")
        return self._read(
            principal,
            ProfessionalOperation.EVIDENCE_FOR,
            lambda p: match_requirement(text, p),
        )

    def assess_claim(
        self, principal: Principal, statement: str
    ) -> OpResult[claim_policy.ClaimAssessment]:
        text = statement.strip()
        if not text or len(text) > 1_000:
            return OpResult("allow", False, "statement_invalid")
        return self._read(
            principal,
            ProfessionalOperation.ASSESS_CLAIM,
            lambda p: self._policy.assess(text, p),
        )

    def experience_for(
        self,
        principal: Principal,
        skill: str | None = None,
        *,
        max_sensitivity: PrivacyClass | None = None,
    ) -> OpResult[ExperienceAnswer]:
        def build(profile: Profile) -> ExperienceAnswer:
            claim_id: str | None = None
            subject: str | None = None
            if skill:
                entry = normalize_skill(skill) or next(
                    (m.entry for m in find_mentions(skill)), None
                )
                if entry is None:
                    return ExperienceAnswer(
                        skill, False, 0, 0, 0, 0, "skill_not_in_vocabulary"
                    )
                subject = entry.display
                view = next(
                    (
                        v
                        for v in profile.by_category(
                            ClaimCategory.SKILL, ClaimCategory.TECHNOLOGY
                        )
                        if v.attributes.get("skill_id") == entry.skill_id
                    ),
                    None,
                )
                if view is None or not view.accepted:
                    return ExperienceAnswer(
                        subject, False, 0, 0, 0, 0, "skill_not_verified"
                    )
                if max_sensitivity is not None and view.sensitivity > max_sensitivity:
                    return ExperienceAnswer(
                        subject, False, 0, 0, 0, 0, "withheld_private"
                    )
                claim_id = view.claim_id
            nominal = profile.experience_months(
                claim_id, conservative=False, max_sensitivity=max_sensitivity
            )
            conservative = profile.experience_months(
                claim_id, conservative=True, max_sensitivity=max_sensitivity
            )
            reason = (
                "resolved_timeline" if conservative else "no_resolved_timeline_evidence"
            )
            return ExperienceAnswer(
                subject,
                True,
                nominal,
                nominal // 12,
                nominal % 12,
                conservative,
                reason,
            )

        return self._read(principal, ProfessionalOperation.READ, build)

    # ---------------------------------------------------------- ingestion

    def ingest_local_file(
        self,
        principal: Principal,
        *,
        root: Path,
        relative_path: str,
        source_type: str,
        privacy_class: PrivacyClass,
        use_llm: bool = False,
        confirmation_id: str | None = None,
    ) -> OpResult[IngestSummary]:
        """TRUSTED callers only (never an HTTP route, never the model): read one
        owner-selected file from inside ``root`` (no traversal, no symlinks).
        The PermissionEngine decides BEFORE the file is opened."""

        operation_id = uuid4().hex
        allowed, permission, reason, pending = self._authorize(
            principal, PermissionAction.WRITE, _SCOPE_INGEST, confirmation_id
        )
        if not allowed:
            self._record(ProfessionalOperation.INGEST, principal, permission, "denied")
            return OpResult(permission, False, reason, pending, None, operation_id)
        try:
            label, content = read_source_file(root, relative_path)
        except SourceFileError as error:
            return OpResult(permission, False, error.code, None, None, operation_id)
        resource_type = _SUFFIX_TYPES.get(Path(label).suffix.lower())
        if resource_type is None:
            return OpResult(
                permission, False, "unsupported_type", None, None, operation_id
            )
        return self._locked_ingest(
            principal,
            operation_id,
            permission,
            label,
            source_type,
            privacy_class,
            resource_type,
            content,
            use_llm,
        )

    def ingest_source(
        self,
        principal: Principal,
        *,
        name: str,
        source_type: str,
        privacy_class: PrivacyClass,
        resource_type: str,
        content: bytes,
        use_llm: bool = False,
        confirmation_id: str | None = None,
    ) -> OpResult[IngestSummary]:
        operation_id = uuid4().hex
        allowed, permission, reason, pending = self._authorize(
            principal, PermissionAction.WRITE, _SCOPE_INGEST, confirmation_id
        )
        if not allowed:
            self._record(ProfessionalOperation.INGEST, principal, permission, "denied")
            return OpResult(permission, False, reason, pending, None, operation_id)
        return self._locked_ingest(
            principal,
            operation_id,
            permission,
            name,
            source_type,
            privacy_class,
            resource_type,
            content,
            use_llm,
        )

    def _locked_ingest(
        self,
        principal: Principal,
        operation_id: str,
        permission: str,
        name: str,
        source_type: str,
        privacy_class: PrivacyClass,
        resource_type: str,
        content: bytes,
        use_llm: bool,
    ) -> OpResult[IngestSummary]:
        """An AUTHORIZED ingest, serialized with every other write."""

        with self._write_lock:
            try:
                return self._ingest(
                    principal,
                    operation_id,
                    name,
                    source_type,
                    privacy_class,
                    resource_type,
                    content,
                    use_llm,
                )
            except Exception:
                self._record(
                    ProfessionalOperation.INGEST,
                    principal,
                    permission,
                    "failed",
                    error_category="ingestion_failed",
                )
                return OpResult(
                    permission, False, "ingestion_failed", None, None, operation_id
                )

    def refresh_source(
        self,
        principal: Principal,
        source_id: str,
        *,
        name: str,
        resource_type: str,
        content: bytes,
        use_llm: bool = False,
        confirmation_id: str | None = None,
    ) -> OpResult[IngestSummary]:
        """Replace the content of an EXISTING logical source with a new version
        (the file may have been renamed). The logical ``source_id`` never
        changes; the checksum and version do. All-or-nothing: if the new version
        cannot be read, extracted or validated, the previous version and every
        piece of its evidence are left exactly as they were."""

        operation_id = uuid4().hex
        allowed, permission, reason, pending = self._authorize(
            principal, PermissionAction.WRITE, _SCOPE_INGEST, confirmation_id
        )
        if not allowed:
            self._record(ProfessionalOperation.INGEST, principal, permission, "denied")
            return OpResult(permission, False, reason, pending, None, operation_id)
        with self._write_lock:
            existing = self._repo.get_source(source_id)
            if existing is None:
                return OpResult(
                    permission, False, "source_not_found", None, None, operation_id
                )
            try:
                return self._ingest(
                    principal,
                    operation_id,
                    name,
                    existing.source_type.value,
                    existing.privacy_class,
                    resource_type,
                    content,
                    use_llm,
                    target=existing,
                )
            except Exception:
                self._record(
                    ProfessionalOperation.INGEST,
                    principal,
                    permission,
                    "failed",
                    source_id=source_id,
                    error_category="ingestion_failed",
                )
                return OpResult(
                    permission, False, "ingestion_failed", None, None, operation_id
                )

    def _reject(
        self,
        principal: Principal,
        operation_id: str,
        reason: str,
    ) -> OpResult[IngestSummary]:
        self._record(
            ProfessionalOperation.INGEST,
            principal,
            "allow",
            "rejected",
            error_category=reason,
        )
        return OpResult("allow", False, reason, None, None, operation_id)

    def _ingest(
        self,
        principal: Principal,
        operation_id: str,
        name: str,
        source_type_text: str,
        privacy: PrivacyClass,
        resource_type: str,
        content: bytes,
        use_llm: bool,
        target: SourceRecord | None = None,
    ) -> OpResult[IngestSummary]:
        """Validate, read and extract into TEMPORARY state; only a complete,
        valid batch reaches the repository's atomic commit. Every early return
        leaves the stored state untouched."""

        try:
            source_type = SourceType(source_type_text)
        except ValueError:
            return self._reject(principal, operation_id, "unsupported_source_type")
        if privacy is PrivacyClass.SECRET:
            return self._reject(principal, operation_id, "secret_not_ingestible")
        if privacy not in _ALLOWED_SOURCE_CLASSES:
            return self._reject(principal, operation_id, "privacy_class_invalid")
        label = safe_label(name)
        if label is None:
            return self._reject(principal, operation_id, "invalid_source")

        read = self._reader.read(
            name=label, resource_type=resource_type, content=content
        )
        if read.rejection is not None or read.document is None:
            reason = read.rejection.value if read.rejection else "read_error"
            return self._reject(principal, operation_id, reason)
        document = read.document

        # Logical identity: an explicit refresh keeps its source id whatever the
        # new file is called; otherwise the (type, label) pair names the source.
        source_id = target.source_id if target else new_source_id(source_type, label)
        existing = self._repo.get_source(source_id)
        if existing is not None and existing.checksum == document.checksum:
            # Identical source: idempotent, nothing changes (the owner's stored
            # privacy class is kept; changing it is an explicit UPDATE).
            self._record(
                ProfessionalOperation.INGEST,
                principal,
                "allow",
                "unchanged",
                source_id=source_id,
            )
            return OpResult(
                "allow",
                True,
                None,
                None,
                IngestSummary(status="unchanged", source_id=source_id),
                operation_id,
            )
        duplicate = self._repo.find_source_by_checksum(document.checksum)
        if duplicate is not None and duplicate.source_id != source_id:
            self._record(
                ProfessionalOperation.INGEST,
                principal,
                "allow",
                "duplicate",
                source_id=duplicate.source_id,
            )
            return OpResult(
                "allow",
                False,
                "duplicate_source",
                None,
                IngestSummary(status="duplicate", source_id=duplicate.source_id),
                operation_id,
            )

        try:
            extraction = extract(
                document,
                source_type=source_type,
                label=label,
                identity=self._identity,
            )
        except ExtractionError as error:
            return self._reject(principal, operation_id, error.code)

        candidates = CandidateOutcome()
        if use_llm and self._candidates is not None:
            try:
                candidates = self._candidates.propose(
                    segments=document.segments, privacy_class=privacy
                )
            except Exception:
                candidates = CandidateOutcome(status="failed")

        now = self._clock()
        content_fingerprint, line_fingerprints = fingerprints(
            [segment.text for segment in document.segments]
        )
        source = SourceRecord(
            source_id=source_id,
            source_type=source_type,
            label=label,
            checksum=document.checksum,
            version=existing.version + 1 if existing else 1,
            independence_key=independence_key(source_type, source_id),
            content_fingerprint=content_fingerprint,
            line_fingerprints=line_fingerprints,
            privacy_class=privacy,
            resource_type=document.resource_type,
            segment_count=len(document.segments),
            extractor_version=EXTRACTOR_VERSION,
            candidate_extraction=candidates.status if use_llm else "off",
            ingested_at=existing.ingested_at if existing else now,
            refreshed_at=now,
        )
        try:
            batch, created, skipped = self._build_batch(
                source, extraction, candidates, now
            )
        except (ValidationError, ValueError):
            return self._reject(principal, operation_id, "invalid_extraction")

        before_ids = {c.claim_id for c in self._repo.list_claims()}
        affected = self._repo.commit_ingestion(batch)
        conflicts_open = len(self._profile().open_conflicts())
        new_claims = sum(1 for cid in affected if cid not in before_ids)
        summary = IngestSummary(
            status="refreshed" if existing else "ingested",
            source_id=source_id,
            claims_created=new_claims,
            claims_updated=len(affected) - new_claims,
            claims_skipped_rejected=skipped,
            evidence_added=len(batch.evidence),
            conflicts_open=conflicts_open,
            candidate_extraction=source.candidate_extraction,
            candidates_proposed=candidates.proposed,
            candidates_accepted=len(candidates.accepted),
            candidates_rejected=candidates.rejected,
            unmapped_skills=extraction.unmapped_skills,
            claim_ids=tuple(affected[:50]),
        )
        self._record(
            ProfessionalOperation.INGEST,
            principal,
            "allow",
            "ingested" if not existing else "refreshed",
            source_id=source_id,
            claim_ids=tuple(affected),
            counts={
                "claims_created": summary.claims_created,
                "claims_updated": summary.claims_updated,
                "claims_skipped_rejected": skipped,
                "evidence_added": summary.evidence_added,
                "conflicts_open": conflicts_open,
                "candidates_proposed": candidates.proposed,
                "candidates_accepted": len(candidates.accepted),
                "candidates_rejected": candidates.rejected,
                "unmapped_skills": extraction.unmapped_skills,
            },
        )
        _ = created
        return OpResult("allow", True, None, None, summary, operation_id)

    def _build_batch(
        self,
        source: SourceRecord,
        extraction: ExtractionResult,
        candidates: CandidateOutcome,
        now: datetime,
    ) -> tuple[IngestionBatch, int, int]:
        rejected = self._repo.list_rejections()
        claims: dict[str, ProfessionalClaim] = {}
        evidence: dict[str, EvidenceRef] = {}
        draft_ids: dict[tuple[ClaimCategory, str], str] = {}
        skipped = 0

        for draft in extraction.drafts:
            claim_id = new_claim_id(draft.category, draft.key)
            if claim_id in rejected:
                skipped += 1
                continue
            draft_ids[(draft.category, draft.key)] = claim_id
            floor = CATEGORY_SENSITIVITY.get(draft.category, PrivacyClass.PUBLIC)
            claims[claim_id] = ProfessionalClaim(
                claim_id=claim_id,
                category=draft.category,
                canonical_statement=draft.statement,
                key=draft.key,
                attributes=dict(draft.attributes),
                sensitivity=floor,
                created_at=now,
                updated_at=now,
            )
        for draft in extraction.drafts:
            known_id = draft_ids.get((draft.category, draft.key))
            if known_id is None:
                continue
            claim_id = known_id
            for item in draft.evidence:
                observed = dict(item.observed)
                if not set(observed) <= ATTRIBUTE_ALLOWLIST[draft.category]:
                    raise ValueError("attribute not allowed for this category")
                context_id = draft_ids.get(item.context) if item.context else None
                ref = EvidenceRef(
                    evidence_id=new_evidence_id(
                        claim_id,
                        source.source_id,
                        item.nature,
                        item.location,
                        item.reference,
                    ),
                    claim_id=claim_id,
                    source_id=source.source_id,
                    source_type=source.source_type,
                    source_location=item.location,
                    evidence_reference=item.reference,
                    nature=item.nature,
                    observed=observed,
                    context_claim_id=context_id,
                    sensitivity=draft.sensitivity,
                    created_at=now,
                )
                evidence[ref.evidence_id] = ref

        existing_claims = {c.claim_id for c in self._repo.list_claims()}
        for candidate in candidates.accepted:
            claim_id = new_claim_id(candidate.category, candidate.key)
            if claim_id in rejected:
                skipped += 1
                continue
            if claim_id not in claims and claim_id not in existing_claims:
                claims[claim_id] = ProfessionalClaim(
                    claim_id=claim_id,
                    category=candidate.category,
                    canonical_statement=candidate.statement,
                    key=candidate.key,
                    attributes=dict(candidate.attributes),
                    sensitivity=CATEGORY_SENSITIVITY.get(
                        candidate.category, PrivacyClass.PUBLIC
                    ),
                    created_at=now,
                    updated_at=now,
                )
            # A candidate carries NO observed attributes: it can never assert or
            # change a date, a title or a classification.
            ref = EvidenceRef(
                evidence_id=new_evidence_id(
                    claim_id,
                    source.source_id,
                    EvidenceNature.MODEL_CANDIDATE,
                    candidate.location,
                    candidate.reference,
                ),
                claim_id=claim_id,
                source_id=source.source_id,
                source_type=source.source_type,
                source_location=candidate.location,
                evidence_reference=candidate.reference,
                nature=EvidenceNature.MODEL_CANDIDATE,
                observed={},
                context_claim_id=None,
                created_at=now,
            )
            evidence[ref.evidence_id] = ref
        for ref in self._carry_attestations(source, evidence, now):
            evidence[ref.evidence_id] = ref
        batch = IngestionBatch(source, tuple(claims.values()), tuple(evidence.values()))
        return batch, len(claims), skipped

    def _carry_attestations(
        self,
        source: SourceRecord,
        evidence: dict[str, EvidenceRef],
        now: datetime,
    ) -> list[EvidenceRef]:
        """On a refresh, the owner's attestations anchored in the OLD version of
        this source are re-anchored on the new version's evidence for the same
        claim. If the new version no longer says anything about the claim, the
        attestation is not carried and the owner's confirmation lapses."""

        carried: list[EvidenceRef] = []
        for old in self._repo.list_all_evidence():
            if (
                old.source_id != source.source_id
                or old.nature is not EvidenceNature.OWNER_ATTESTATION
            ):
                continue
            basis = _pick_basis(
                e for e in evidence.values() if e.claim_id == old.claim_id
            )
            if basis is not None:
                carried.append(_attestation(basis, now))
        return carried

    # ---------------------------------------------------- owner decisions

    def confirm_claim(self, principal: Principal, claim_id: str) -> OpResult[ClaimView]:
        """The owner vouches for a claim through the trusted review workflow.

        This records ``ClaimReviewState.OWNER_CONFIRMED`` AND an
        ``OWNER_ATTESTATION`` evidence record naming the evidence the owner
        reviewed (its provenance), atomically. It never removes, replaces or
        re-labels any documentary evidence and never changes documentary
        strength: an attested claim with no document behind it is accepted with
        strength NONE."""

        operation_id = uuid4().hex
        allowed, permission, reason, pending = self._authorize(
            principal, PermissionAction.UPDATE, _SCOPE_REVIEW
        )
        if not allowed:
            self._record(ProfessionalOperation.CONFIRM, principal, permission, "denied")
            return OpResult(permission, False, reason, pending, None, operation_id)
        with self._write_lock:
            claim = self._repo.get_claim(claim_id)
            if claim is None:
                return OpResult(
                    permission, False, "claim_not_found", None, None, operation_id
                )
            if claim.key.startswith("unmapped|"):
                # Not in the trusted vocabulary: cannot be promoted by anyone.
                self._record(
                    ProfessionalOperation.CONFIRM,
                    principal,
                    permission,
                    "rejected",
                    claim_ids=(claim_id,),
                    error_category="unknown_skill",
                )
                return OpResult(
                    permission, False, "unknown_skill", None, None, operation_id
                )
            refs = self._repo.list_evidence(claim_id)
            basis = _pick_basis(refs)
            if basis is None:
                return OpResult(
                    permission, False, "no_evidence", None, None, operation_id
                )
            now = self._clock()
            self._repo.confirm(
                ClaimReview(
                    claim_id=claim_id,
                    state=ClaimReviewState.OWNER_CONFIRMED,
                    reviewed_at=now,
                ),
                _attestation(basis, now),
            )
            view = self._profile().view(claim_id)
            self._record(
                ProfessionalOperation.CONFIRM,
                principal,
                permission,
                "ok",
                claim_ids=(claim_id,),
            )
            return OpResult(
                permission, view is not None, None, None, view, operation_id
            )

    def reject_claim(self, principal: Principal, claim_id: str) -> OpResult[bool]:
        operation_id = uuid4().hex
        allowed, permission, reason, pending = self._authorize(
            principal, PermissionAction.UPDATE, _SCOPE_REVIEW
        )
        if not allowed:
            self._record(ProfessionalOperation.REJECT, principal, permission, "denied")
            return OpResult(permission, False, reason, pending, None, operation_id)
        with self._write_lock:
            if self._repo.get_claim(claim_id) is None:
                return OpResult(
                    permission, False, "claim_not_found", None, None, operation_id
                )
            self._repo.add_rejection(claim_id, self._clock())
            self._record(
                ProfessionalOperation.REJECT,
                principal,
                permission,
                "ok",
                claim_ids=(claim_id,),
            )
            return OpResult(permission, True, None, None, True, operation_id)

    def resolve_conflict(
        self, principal: Principal, conflict_id: str, option_id: str
    ) -> OpResult[bool]:
        """Only the owner resolves a conflict. A model can identify one, never
        resolve it."""

        operation_id = uuid4().hex
        allowed, permission, reason, pending = self._authorize(
            principal, PermissionAction.UPDATE, _SCOPE_REVIEW
        )
        if not allowed:
            self._record(
                ProfessionalOperation.RESOLVE_CONFLICT, principal, permission, "denied"
            )
            return OpResult(permission, False, reason, pending, None, operation_id)
        with self._write_lock:
            profile = self._profile()
            conflict = next(
                (c for c in profile.conflicts if c.conflict_id == conflict_id), None
            )
            if conflict is None:
                return OpResult(
                    permission, False, "conflict_not_found", None, None, operation_id
                )
            option = next(
                (o for o in conflict.options if o.option_id == option_id), None
            )
            if option is None:
                return OpResult(
                    permission, False, "option_not_found", None, None, operation_id
                )
            if conflict.kind is ConflictKind.ATTRIBUTE:
                self._repo.put_resolution(
                    Resolution(
                        claim_id=conflict.claim_ids[0],
                        attribute=conflict.attribute,
                        value=option.value,
                        resolved_at=self._clock(),
                    )
                )
            else:
                for other in conflict.claim_ids:
                    if other != option.claim_id:
                        self._repo.add_rejection(other, self._clock())
            self._record(
                ProfessionalOperation.RESOLVE_CONFLICT,
                principal,
                permission,
                "ok",
                claim_ids=conflict.claim_ids,
            )
            return OpResult(permission, True, None, None, True, operation_id)

    def set_source_privacy(
        self, principal: Principal, source_id: str, privacy_class: PrivacyClass
    ) -> OpResult[bool]:
        operation_id = uuid4().hex
        allowed, permission, reason, pending = self._authorize(
            principal, PermissionAction.UPDATE, _SCOPE_REVIEW
        )
        if not allowed:
            self._record(
                ProfessionalOperation.SET_PRIVACY, principal, permission, "denied"
            )
            return OpResult(permission, False, reason, pending, None, operation_id)
        if privacy_class not in _ALLOWED_SOURCE_CLASSES:
            return OpResult(
                permission, False, "privacy_class_invalid", None, None, operation_id
            )
        with self._write_lock:
            source = self._repo.get_source(source_id)
            if source is None:
                return OpResult(
                    permission, False, "source_not_found", None, None, operation_id
                )
            self._repo.update_source(
                source.model_copy(update={"privacy_class": privacy_class})
            )
            self._record(
                ProfessionalOperation.SET_PRIVACY,
                principal,
                permission,
                "ok",
                source_id=source_id,
            )
            return OpResult(permission, True, None, None, True, operation_id)

    def remove_source(
        self,
        principal: Principal,
        source_id: str,
        confirmation_id: str | None = None,
    ) -> OpResult[RemovalSummary]:
        """DELETE (HIGH, always confirmed). Removes ONLY this source's evidence:
        a claim corroborated by another source keeps that source's evidence and
        its strength is recomputed."""

        operation_id = uuid4().hex
        try:
            scope = PermissionScope.from_path(f"profile/{source_id}")
        except ValueError:
            return OpResult("deny", False, "source_not_found", None, None, operation_id)
        allowed, permission, reason, pending = self._authorize(
            principal, PermissionAction.DELETE, scope, confirmation_id
        )
        if not allowed:
            self._record(
                ProfessionalOperation.REMOVE_SOURCE,
                principal,
                permission,
                "denied" if permission == "deny" else "confirmation_required",
                source_id=source_id,
            )
            return OpResult(permission, False, reason, pending, None, operation_id)
        with self._write_lock:
            if self._repo.get_source(source_id) is None:
                return OpResult(
                    permission, False, "source_not_found", None, None, operation_id
                )
            before = {c.claim_id for c in self._repo.list_claims()}
            affected = self._repo.delete_source(source_id)
            after = {c.claim_id for c in self._repo.list_claims()}
            summary = RemovalSummary(source_id, len(affected), len(before - after))
            self._record(
                ProfessionalOperation.REMOVE_SOURCE,
                principal,
                permission,
                "ok",
                source_id=source_id,
                claim_ids=tuple(affected),
                counts={"claims_affected": len(affected)},
            )
            return OpResult(permission, True, None, None, summary, operation_id)


# ------------------------------------------------------------------ helpers


def _pick_basis(refs: Iterable[EvidenceRef]) -> EvidenceRef | None:
    """The evidence an owner attestation rests on: never another attestation."""

    usable = [r for r in refs if r.nature in _BASIS_ORDER]
    if not usable:
        return None
    return min(usable, key=lambda r: (_BASIS_ORDER[r.nature], r.evidence_id))


def _attestation(basis: EvidenceRef, now: datetime) -> EvidenceRef:
    """An OWNER_ATTESTATION anchored on ``basis``: same source and location (its
    provenance), no observed values, never documentary."""

    return EvidenceRef(
        evidence_id=new_evidence_id(
            basis.claim_id,
            basis.source_id,
            EvidenceNature.OWNER_ATTESTATION,
            basis.source_location,
            basis.evidence_id,
        ),
        claim_id=basis.claim_id,
        source_id=basis.source_id,
        source_type=basis.source_type,
        source_location=basis.source_location,
        evidence_reference=basis.evidence_reference,
        nature=EvidenceNature.OWNER_ATTESTATION,
        observed={},
        context_claim_id=basis.context_claim_id,
        sensitivity=basis.sensitivity,
        basis_evidence_id=basis.evidence_id,
        created_at=now,
    )


def _ordered(views: Iterable[ClaimView]) -> tuple[ClaimView, ...]:
    return tuple(
        sorted(
            views,
            key=lambda v: (
                v.category.value,
                _rank(v),
                v.statement,
                v.claim_id,
            ),
        )
    )


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9+#.]+", text.lower()) if len(t) >= 2]


def _search(profile: Profile, query: str) -> tuple[ClaimView, ...]:
    wanted = {m.entry.skill_id for m in find_mentions(query)}
    entry = normalize_skill(query)
    if entry is not None:
        wanted.add(entry.skill_id)
    tokens = set(_tokens(query))
    scored: list[tuple[int, ClaimView]] = []
    for view in profile.views.values():
        score = 0
        if view.attributes.get("skill_id") in wanted:
            score += 10
        haystack = " ".join([view.statement, *view.attributes.values()]).lower()
        score += sum(1 for t in tokens if t in _tokens(haystack))
        if score:
            scored.append((score, view))
    scored.sort(
        key=lambda item: (
            -item[0],
            _rank(item[1]),
            item[1].category.value,
            item[1].claim_id,
        )
    )
    return tuple(view for _, view in scored[:50])


def _publication(view: ClaimView) -> PublicationView:
    a = view.attributes
    position = a.get("owner_author_position")
    return PublicationView(
        claim_id=view.claim_id,
        strength=view.strength,
        review=view.review,
        accepted=view.accepted,
        sensitivity=view.sensitivity,
        title=a.get("title", view.statement),
        authors=_split(a.get("authors")),
        owner_author_position=int(position)
        if position and position.isdigit()
        else None,
        venue=a.get("venue"),
        year=a.get("year"),
        doi=a.get("doi"),
        abstract=a.get("abstract"),
        methods=a.get("methods"),
        models=_split(a.get("models")),
        datasets=a.get("datasets"),
        findings=a.get("findings"),
        limitations=a.get("limitations"),
        topics=_split(a.get("topics")),
        owner_contribution=a.get("owner_contribution"),
        evidence=view.evidence,
        conflicted_attributes=view.conflicted_attributes,
    )


def _education(view: ClaimView) -> EducationView:
    a = view.attributes
    return EducationView(
        claim_id=view.claim_id,
        strength=view.strength,
        review=view.review,
        accepted=view.accepted,
        sensitivity=view.sensitivity,
        institution=a.get("institution"),
        degree=a.get("degree", ""),
        subject=a.get("subject", ""),
        classification=a.get("classification"),
        start=a.get("start"),
        end=a.get("end"),
        modules=_split(a.get("modules")),
        dissertation=a.get("dissertation"),
        evidence=view.evidence,
        conflicted_attributes=view.conflicted_attributes,
    )


__all__ = ["ProfessionalService"]
