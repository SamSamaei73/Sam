"""Read-only adapters from Career to Professional Intelligence and Knowledge.

This is composition code: it is the ONLY place that knows both sides. Career
sees owner facts only as accepted Phase 14 claims with their evidence ids, and
papers only as Knowledge resources the owner actually ingested. Every call runs
as the principal, so Professional's and Knowledge's own permission checks
apply; a denied or failed read is ``None`` (Career treats it as UNKNOWN). There
is no write path from here into either domain.
"""

from __future__ import annotations

import re

from sam.career.evidence import (
    EvidenceClaim,
    PaperRecord,
    RequirementResult,
    StatementCheck,
)
from sam.career.models import FitStatus
from sam.knowledge.engine import KnowledgeEngine
from sam.knowledge.models import ListResourcesRequest
from sam.permissions.models import Principal
from sam.professional.claims import Verdict
from sam.professional.models import MatchStatus
from sam.professional.profile import ClaimView
from sam.professional.service import ProfessionalService

_STATUS = {
    MatchStatus.MATCHED: FitStatus.SUPPORTED,
    MatchStatus.PARTIALLY_SUPPORTED: FitStatus.PARTIALLY_SUPPORTED,
    MatchStatus.NOT_SUPPORTED: FitStatus.NOT_SUPPORTED,
    MatchStatus.UNKNOWN: FitStatus.UNKNOWN,
}


def _claim(view: ClaimView) -> EvidenceClaim:
    return EvidenceClaim(
        claim_id=view.claim_id,
        category=view.category.value,
        statement=view.statement,
        evidence_ids=tuple(e.evidence_id for e in view.evidence),
        attributes=dict(view.attributes),
        strength=view.strength.value,
    )


class ProfessionalEvidence:
    """``EvidencePort`` over ``ProfessionalService``: accepted claims only."""

    def __init__(self, service: ProfessionalService) -> None:
        self._service = service

    def claims(self, principal: Principal) -> tuple[EvidenceClaim, ...] | None:
        result = self._service.get_profile(principal)
        if not result.ok or result.data is None:
            return None
        return tuple(_claim(v) for v in result.data.claims if v.accepted)

    def requirement(self, principal: Principal, text: str) -> RequirementResult | None:
        result = self._service.evidence_for(principal, text[:300])
        if not result.ok or result.data is None:
            return None
        data = result.data
        return RequirementResult(
            status=_STATUS[data.status],
            claims=tuple(_claim(v) for v in data.matched_claims if v.accepted),
            unsupported=tuple(data.unsupported_aspects),
            notes=tuple(data.notes),
        )

    def check_statement(self, principal: Principal, text: str) -> StatementCheck | None:
        result = self._service.assess_claim(principal, text[:1_000])
        if not result.ok or result.data is None:
            return None
        verdict = result.data.verdict
        return StatementCheck(
            supported=None
            if verdict is Verdict.NOT_CHECKABLE
            else verdict is Verdict.SUPPORTED,
            reasons=tuple(f.reason for f in result.data.findings),
            claim_ids=tuple(i for f in result.data.findings for i in f.claim_ids),
        )


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.casefold()).split())


class KnowledgePapers:
    """``PaperPort`` over Knowledge: a paper counts as read ONLY if a resource
    with exactly that (normalized) title or name was ingested."""

    def __init__(self, engine: KnowledgeEngine, collection_id: str) -> None:
        self._engine = engine
        self._collection = collection_id

    def retrieved(self, principal: Principal, title: str) -> PaperRecord | None:
        wanted = _norm(title)
        if len(wanted) < 3:
            return None
        try:
            result = self._engine.list_resources(
                ListResourcesRequest(
                    principal=principal, collection_id=self._collection
                )
            )
        except Exception:
            return None
        for resource in result.resources:
            names = {
                _norm(resource.metadata.title or ""),
                _norm(resource.name.rsplit(".", 1)[0]),
            }
            if wanted in names:
                return PaperRecord(
                    resource.resource_id, resource.metadata.title or resource.name
                )
        return None


__all__ = ["KnowledgePapers", "ProfessionalEvidence"]
