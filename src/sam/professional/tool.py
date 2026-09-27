"""The model-facing boundary to Professional Intelligence: READ ONLY.

A model may ask for professional evidence through this tool. It cannot:

* verify a claim, confirm or reject a candidate, or resolve a conflict;
* modify or delete evidence, remove a source, or change any privacy class;
* ingest a file or any external data, or pass a path, SQL or an expression;
* write the CV, apply for jobs, or send email.

Those operations exist only on ``ProfessionalService`` behind the
PermissionEngine and are driven by the owner's UI, never by this tool: the tool
has no method that reaches them and rejects every operation name not listed.

Privacy: PRIVATE claims (salary, visa, private notes) are WITHHELD from tool
output entirely (only a count is reported). No raw source text is ever
included: not an evidence excerpt (``EvidenceRef.evidence_reference``), not a
paper's abstract or findings, not the contribution sentence naming the owner;
only where a fact came from (source type, label, page, section). The output carries the
highest sensitivity it contains, so a caller that puts it into a prompt can raise
the Phase 13 ``privacy_scope`` accordingly and the router applies its policy.

The principal is bound by trusted code at construction; a model cannot supply one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sam.models.models import PrivacyClass
from sam.permissions.models import Principal
from sam.professional.profile import ClaimView
from sam.professional.service import ProfessionalService

MAX_TEXT = 300
# Aggregates that may reach a model never include PRIVATE evidence.
_CEILING = PrivacyClass.PERSONAL
_PRIVATE_KINDS = frozenset({"salary", "work_authorization", "clearance"})
READ_OPERATIONS = (
    "search",
    "evidence_for",
    "get_skills",
    "get_projects",
    "get_publications",
    "get_research_topics",
    "get_education",
    "get_timeline",
    "get_profile_gaps",
    "experience_for",
    "assess_claim",
)
_ALLOWED_ARGUMENTS = frozenset(
    {"operation", "query", "requirement", "skill", "statement"}
)
# Free-text attributes copied from a document (a paper's abstract or findings,
# the contribution sentence naming the owner, a project description ...). They
# are raw source text, so they never reach a model; structured attributes
# (employer, dates, degree, DOI, skill id ...) do.
RAW_TEXT_ATTRIBUTES = frozenset(
    {
        "abstract",
        "methods",
        "datasets",
        "findings",
        "limitations",
        "owner_contribution",
        "description",
        "outcome",
        "dissertation",
        "value",
    }
)


def _status(view: Any) -> dict[str, Any]:
    """Documentary strength, the owner's review and acceptance: three separate
    facts, never folded into one."""

    return {
        "evidence_strength": view.strength.value,
        "review_state": view.review.value,
        "accepted": view.accepted,
    }


def _claim(view: ClaimView) -> dict[str, Any]:
    return {
        "claim_id": view.claim_id,
        "category": view.category.value,
        "statement": view.statement,
        **_status(view),
        "independent_source_families": view.source_family_count,
        "sensitivity": view.sensitivity.name.lower(),
        "attributes": {
            k: v for k, v in view.attributes.items() if k not in RAW_TEXT_ATTRIBUTES
        },
        "conflicted_attributes": list(view.conflicted_attributes),
        "provenance": [
            {
                "source_type": e.source_type.value,
                "source_label": e.source_label,
                "nature": e.nature.value,
                "page": e.location.page_number,
                "section": e.location.section_title,
            }
            for e in view.evidence[:5]
        ],
    }


def _finding(finding: Any) -> dict[str, Any]:
    if finding.kind.value in _PRIVATE_KINDS:
        # Whether the owner stated a salary/visa/clearance is itself private.
        return {
            "kind": finding.kind.value,
            "supported": None,
            "reason": "private_withheld",
        }
    return {
        "kind": finding.kind.value,
        "supported": finding.supported,
        "reason": finding.reason,
    }


class ProfessionalReadTool:
    name = "professional_evidence"
    description = (
        "Read the owner's evidence-backed professional profile: skills, projects, "
        "publications, education, timeline, and requirement-to-evidence matching. "
        "Read-only; every fact carries its provenance and evidence strength."
    )

    def __init__(self, service: ProfessionalService, principal: Principal) -> None:
        self._service = service
        self._principal = principal

    def input_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": list(READ_OPERATIONS)},
                "query": {"type": "string", "maxLength": MAX_TEXT},
                "requirement": {"type": "string", "maxLength": MAX_TEXT},
                "skill": {"type": "string", "maxLength": 80},
                "statement": {"type": "string", "maxLength": 1_000},
            },
        }

    def execute(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(arguments) - _ALLOWED_ARGUMENTS
        if unknown:
            raise ValueError("unsupported argument")
        operation = arguments.get("operation")
        if operation not in READ_OPERATIONS:
            raise ValueError("unsupported operation")
        for name in _ALLOWED_ARGUMENTS - {"operation"}:
            value = arguments.get(name)
            if value is not None and not isinstance(value, str):
                raise ValueError("arguments must be strings")
        handler = getattr(self, f"_{operation}")
        reply: dict[str, Any] = handler(arguments)
        return reply

    # ------------------------------------------------------------ helpers

    def _reply(self, result: Any, build: Any) -> dict[str, Any]:
        if not result.ok:
            return {"ok": False, "reason": result.reason}
        payload: dict[str, Any] = build(result.data)
        payload["ok"] = True
        return payload

    def _claims(self, views: Any) -> dict[str, Any]:
        visible = [v for v in views if v.sensitivity < PrivacyClass.PRIVATE]
        withheld = len(views) - len(visible)
        top = max((v.sensitivity for v in visible), default=PrivacyClass.PUBLIC)
        return {
            "items": [_claim(v) for v in visible],
            "withheld_private": withheld,
            "max_sensitivity": top.name.lower(),
        }

    # --------------------------------------------------------- operations

    def _search(self, args: Mapping[str, Any]) -> dict[str, Any]:
        result = self._service.search(self._principal, str(args.get("query", "")))
        return self._reply(result, lambda data: self._claims(data))

    def _evidence_for(self, args: Mapping[str, Any]) -> dict[str, Any]:
        result = self._service.evidence_for(
            self._principal, str(args.get("requirement", ""))
        )

        def build(data: Any) -> dict[str, Any]:
            matched = self._claims(list(data.matched_claims))
            return {
                "status": data.status.value,
                "normalized_requirement": data.normalized_requirement,
                "matched": matched["items"],
                "unsupported_aspects": list(data.unsupported_aspects),
                "notes": list(data.notes),
                "withheld_private": matched["withheld_private"],
                "max_sensitivity": matched["max_sensitivity"],
            }

        return self._reply(result, build)

    def _list(self, result: Any) -> dict[str, Any]:
        return self._reply(result, lambda data: self._claims(list(data)))

    def _get_skills(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self._list(self._service.get_skills(self._principal))

    def _get_projects(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self._list(self._service.get_projects(self._principal))

    def _get_research_topics(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self._list(self._service.get_research_topics(self._principal))

    def _get_publications(self, args: Mapping[str, Any]) -> dict[str, Any]:
        result = self._service.get_publications(self._principal)

        def build(data: Any) -> dict[str, Any]:
            visible = [p for p in data if p.sensitivity < PrivacyClass.PRIVATE]
            return {
                "items": [
                    {
                        "claim_id": p.claim_id,
                        **_status(p),
                        "title": p.title,
                        "authors": list(p.authors),
                        "owner_author_position": p.owner_author_position,
                        "venue": p.venue,
                        "year": p.year,
                        "doi": p.doi,
                        "topics": list(p.topics),
                        # Whether a contribution statement names the owner;
                        # never the sentence itself (raw source text).
                        "owner_contribution_stated": bool(p.owner_contribution),
                    }
                    for p in visible
                ],
                "withheld_private": len(data) - len(visible),
                "max_sensitivity": max(
                    (p.sensitivity for p in visible), default=PrivacyClass.PUBLIC
                ).name.lower(),
            }

        return self._reply(result, build)

    def _get_education(self, args: Mapping[str, Any]) -> dict[str, Any]:
        result = self._service.get_education(self._principal)

        def build(data: Any) -> dict[str, Any]:
            visible = [e for e in data if e.sensitivity < PrivacyClass.PRIVATE]
            return {
                "items": [
                    {
                        "claim_id": e.claim_id,
                        **_status(e),
                        "institution": e.institution,
                        "degree": e.degree,
                        "subject": e.subject,
                        "classification": e.classification,
                        "start": e.start,
                        "end": e.end,
                    }
                    for e in visible
                ],
                "withheld_private": len(data) - len(visible),
                "max_sensitivity": max(
                    (e.sensitivity for e in visible), default=PrivacyClass.PUBLIC
                ).name.lower(),
            }

        return self._reply(result, build)

    def _get_timeline(self, args: Mapping[str, Any]) -> dict[str, Any]:
        result = self._service.get_timeline(self._principal, max_sensitivity=_CEILING)

        def build(data: Any) -> dict[str, Any]:
            return {
                "entries": [
                    {
                        "employer": e.employer,
                        "title": e.title,
                        "start": e.start,
                        "end": e.end,
                        "status": e.status.value,
                        "months": e.months,
                    }
                    for e in data.entries
                ],
                "total_years": data.total.years,
                "total_remainder_months": data.total.remainder_months,
                "excluded_conflicted": len(data.excluded_conflicted),
                "excluded_incomplete": len(data.excluded_incomplete),
                "max_sensitivity": "personal",
            }

        return self._reply(result, build)

    def _get_profile_gaps(self, args: Mapping[str, Any]) -> dict[str, Any]:
        result = self._service.get_profile_gaps(
            self._principal, max_sensitivity=_CEILING
        )
        return self._reply(
            result,
            lambda data: {
                "items": [{"kind": g.kind.value, "detail": g.detail} for g in data],
                "max_sensitivity": "personal",
            },
        )

    def _experience_for(self, args: Mapping[str, Any]) -> dict[str, Any]:
        skill = args.get("skill")
        result = self._service.experience_for(
            self._principal, str(skill) if skill else None, max_sensitivity=_CEILING
        )
        return self._reply(
            result,
            lambda d: {
                "subject": d.subject,
                "verified": d.verified,
                "years": d.years,
                "remainder_months": d.remainder_months,
                "conservative_months": d.conservative_months,
                "reason": d.reason,
                "max_sensitivity": "personal",
            },
        )

    def _assess_claim(self, args: Mapping[str, Any]) -> dict[str, Any]:
        result = self._service.assess_claim(
            self._principal, str(args.get("statement", ""))
        )
        return self._reply(
            result,
            lambda d: {
                "verdict": d.verdict.value,
                "findings": [_finding(f) for f in d.findings],
                "max_sensitivity": "personal",
            },
        )


__all__ = ["RAW_TEXT_ATTRIBUTES", "READ_OPERATIONS", "ProfessionalReadTool"]
