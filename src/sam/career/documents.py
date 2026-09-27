"""Application documents built from evidence: tailored CV, cover letter, PhD
research statement and proposal outline.

Tailoring may reorder, select, shorten and emphasize; it may never invent. Every
FACT line is RENDERED from accepted Phase 14 claims (dates, employer, title,
degree, venue and metrics copied exactly from the claim) and carries the
claim's evidence ids. Nothing a model writes becomes a fact:

* a cover letter separates FACT lines (rendered from evidence) from MOTIVATION
  lines (the owner's text, a fixed template, or model-drafted interest
  language), and every motivation line still passes the claim policy, so a
  motivation line that slips in an unsupported owner fact is FLAGGED;
* an owner edit is re-validated line by line: an unchanged line keeps its
  evidence; an employment/education line whose dates or title differ from the
  evidence is flagged; any other new line needs evidence (numbers must appear
  in the evidence verbatim) or it is flagged.

A flagged line blocks document approval, so it can never be submitted.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol

from sam.career.claims import ClaimChecker, numbers_in_evidence
from sam.career.evidence import EvidenceClaim, EvidencePort
from sam.career.matching import AlignmentReport, FitReport
from sam.career.models import (
    ApplicationDocument,
    CareerOpportunity,
    DocumentKind,
    DocumentLine,
    FitStatus,
    clean_text,
    sha256,
)
from sam.permissions.models import Principal

HEADERS = frozenset(
    {
        "Summary",
        "Experience",
        "Highlights",
        "Education",
        "Projects",
        "Publications",
        "Skills",
        "Research interests",
        "Relevant prior work",
        "Proposed direction",
        "Research questions",
        "Methods",
        "Why this group",
    }
)
_SKILL = frozenset({"skill", "technology"})
_HIGHLIGHT = frozenset({"achievement", "responsibility"})
OWNER_INPUT = "owner_input_required"


class MotivationWriter(Protocol):
    """Optional model drafting of INTEREST language only (never facts). It is
    given minimal disclosure: the role, the organization and skill names."""

    def write(
        self, opportunity: CareerOpportunity, skill_names: Sequence[str]
    ) -> Sequence[str] | None: ...


def render(claim: EvidenceClaim) -> str:
    """The one canonical text of a claim. Dates and titles are copied exactly."""

    a = claim.attributes
    if claim.category == "employment" and a.get("employer") and a.get("title"):
        start, end = a.get("start", "?"), a.get("end", "?")
        return f"{a['title']}, {a['employer']} ({start} to {end})"
    if claim.category == "education" and a.get("degree"):
        parts = [f"{a['degree']} {a.get('subject', '')}".strip()]
        if a.get("institution"):
            parts.append(a["institution"])
        if a.get("classification"):
            parts.append(a["classification"])
        dates = (
            f" ({a.get('start', '?')} to {a.get('end', '?')})" if a.get("end") else ""
        )
        return ", ".join(parts) + dates
    if claim.category == "publication" and a.get("title"):
        tail = ", ".join(v for v in (a.get("venue"), a.get("year")) if v)
        return f"{a['title']}" + (f" ({tail})" if tail else "")
    if claim.category == "project" and a.get("name"):
        desc = a.get("description")
        return f"{a['name']}: {desc}" if desc else a["name"]
    if claim.category in _HIGHLIGHT and a.get("description"):
        return a["description"]
    if claim.category in _SKILL and a.get("display"):
        return a["display"]
    return claim.statement


def fact(claims: Iterable[EvidenceClaim], text: str) -> DocumentLine:
    links = tuple(c.link for c in claims)
    return DocumentLine(text=clean_text(text, 2_000), fact=True, evidence=links)


def note(text: str) -> DocumentLine:
    return DocumentLine(text=clean_text(text, 2_000), fact=False)


def placeholder(text: str) -> DocumentLine:
    return DocumentLine(text=text, fact=False, flagged=True, flag_reason=OWNER_INPUT)


def _ranked(claims: Iterable[EvidenceClaim], relevant: set[str]) -> list[EvidenceClaim]:
    return sorted(claims, key=lambda c: (c.claim_id not in relevant, c.statement))


def make_document(
    *,
    document_id: str,
    opportunity_id: str,
    kind: DocumentKind,
    version: int,
    lines: Sequence[DocumentLine],
    now: datetime,
) -> ApplicationDocument:
    text = "\n".join(line.text for line in lines)
    return ApplicationDocument(
        document_id=document_id,
        opportunity_id=opportunity_id,
        kind=kind,
        version=version,
        lines=tuple(lines),
        sha256=sha256(f"{kind.value}\n{text}"),
        created_at=now,
    )


def build_cv(claims: Sequence[EvidenceClaim], fit: FitReport) -> list[DocumentLine]:
    relevant = {c.claim_id for c in fit.supported_claims}
    by = {
        cat: [c for c in claims if c.category == cat]
        for cat in {c.category for c in claims}
    }
    skills = _ranked([c for c in claims if c.category in _SKILL], relevant)
    lines: list[DocumentLine] = []
    top = [s for s in skills if s.claim_id in relevant][:6] or skills[:4]
    if top:
        lines += [
            note("Summary"),
            fact(
                top,
                "Evidence-backed experience with "
                + ", ".join(render(s) for s in top)
                + ".",
            ),
        ]
    employment = sorted(
        by.get("employment", []),
        key=lambda c: c.attributes.get("start", ""),
        reverse=True,
    )
    if employment:
        lines.append(note("Experience"))
        lines += [fact([c], render(c)) for c in employment]
    highlights = _ranked([c for c in claims if c.category in _HIGHLIGHT], relevant)[:6]
    if highlights:
        lines.append(note("Highlights"))
        lines += [fact([c], render(c)) for c in highlights]
    for category, header in (
        ("education", "Education"),
        ("project", "Projects"),
        ("publication", "Publications"),
    ):
        items = _ranked(by.get(category, []), relevant)
        if items:
            lines.append(note(header))
            lines += [fact([c], render(c)) for c in items[:5]]
    if skills:
        lines += [note("Skills"), fact(skills, ", ".join(render(s) for s in skills))]
    return lines


def build_letter(
    principal: Principal,
    opportunity: CareerOpportunity,
    fit: FitReport,
    claims: Sequence[EvidenceClaim],
    checker: ClaimChecker,
    motivation: Sequence[str],
) -> list[DocumentLine]:
    lines: list[DocumentLine] = [note("Dear Hiring Manager,")]
    role, org = opportunity.title, opportunity.organization
    intro = f"I am applying for the {role} position at {org}."
    lines.append(note(intro))
    for text in motivation:
        lines.append(checked_motivation(principal, text, claims, checker))
    supported = [
        f
        for f in fit.requirements
        if f.status in (FitStatus.SUPPORTED, FitStatus.PARTIALLY_SUPPORTED) and f.claims
    ][:4]
    for fit_line in supported:
        chosen = list(fit_line.claims[:2])
        rendered = "; ".join(render(c) for c in chosen)
        lines.append(
            fact(
                chosen,
                f"Relevant to “{fit_line.requirement}”, "
                f"my record includes: {rendered}.",
            )
        )
    lines.append(note("Thank you for considering my application."))
    return lines


def checked_motivation(
    principal: Principal,
    text: str,
    claims: Sequence[EvidenceClaim],
    checker: ClaimChecker,
) -> DocumentLine:
    cleaned = clean_text(text, 2_000)
    result = checker.check(principal, cleaned, tuple(claims))
    if result.flagged:
        return DocumentLine(
            text=cleaned, fact=False, flagged=True, flag_reason=result.reason
        )
    return DocumentLine(text=cleaned, fact=False)


def build_research_statement(
    principal: Principal,
    opportunity: CareerOpportunity,
    alignment: AlignmentReport,
    direction: str | None,
    claims: Sequence[EvidenceClaim],
    checker: ClaimChecker,
) -> list[DocumentLine]:
    lines: list[DocumentLine] = [note("Research interests")]
    if direction:
        lines.append(checked_motivation(principal, direction, claims, checker))
    else:
        lines.append(placeholder("[Write your research interests for this programme]"))
    evidence = {c.claim_id: c for t in alignment.topics for c in t.claims}
    lines.append(note("Relevant prior work"))
    if evidence:
        lines += [fact([c], render(c)) for c in evidence.values()][:6]
    else:
        lines.append(
            note("No evidence-backed prior work matches the programme's stated topics.")
        )
    return lines


def build_proposal_outline(
    principal: Principal,
    opportunity: CareerOpportunity,
    alignment: AlignmentReport,
    direction: str | None,
    claims: Sequence[EvidenceClaim],
    checker: ClaimChecker,
) -> list[DocumentLine]:
    lines: list[DocumentLine] = [
        note(f"Working title for {opportunity.title}"),
        note("Proposed direction"),
    ]
    lines.append(
        checked_motivation(principal, direction, claims, checker)
        if direction
        else placeholder("[Describe the direction you want to propose]")
    )
    lines += [
        note("Research questions"),
        placeholder("[List two or three research questions]"),
    ]
    lines += [note("Methods"), placeholder("[Outline the methods you would use]")]
    evidence = {c.claim_id: c for t in alignment.topics for c in t.claims}
    lines.append(note("Relevant prior work"))
    lines += [fact([c], render(c)) for c in evidence.values()][:4] or [
        note("No evidence-backed prior work matches the programme's stated topics.")
    ]
    return lines


def revalidate(
    principal: Principal,
    texts: Sequence[str],
    previous: ApplicationDocument,
    claims: Sequence[EvidenceClaim],
    evidence: EvidencePort,
    checker: ClaimChecker,
) -> list[DocumentLine]:
    """Re-check an owner-edited document line by line (see module docstring)."""

    known = {line.text: line for line in previous.lines}
    anchored = [c for c in claims if c.category in ("employment", "education")]
    out: list[DocumentLine] = []
    for raw in texts:
        text = clean_text(raw, 2_000)
        if not text:
            continue
        if text in known:
            out.append(known[text])
            continue
        if text.rstrip(":") in HEADERS:
            out.append(note(text))
            continue
        anchor = _anchor(text, anchored)
        if anchor is not None:
            if render(anchor) in text:
                out.append(fact([anchor], text))
            else:
                out.append(
                    DocumentLine(
                        text=text,
                        fact=True,
                        flagged=True,
                        flag_reason="dated_line_changed",
                    )
                )
            continue
        if previous.kind in (
            DocumentKind.COVER_LETTER,
            DocumentKind.RESEARCH_STATEMENT,
            DocumentKind.PROPOSAL_OUTLINE,
        ):
            out.append(checked_motivation(principal, text, claims, checker))
            continue
        out.append(_cv_line(principal, text, claims, evidence))
    return out


def _anchor(text: str, claims: Sequence[EvidenceClaim]) -> EvidenceClaim | None:
    lowered = text.casefold()
    for claim in claims:
        a = claim.attributes
        key = (
            a.get("employer")
            if claim.category == "employment"
            else a.get("institution")
        )
        if key and key.casefold() in lowered:
            return claim
    return None


def _cv_line(
    principal: Principal,
    text: str,
    claims: Sequence[EvidenceClaim],
    evidence: EvidencePort,
) -> DocumentLine:
    """A new CV line is a factual line: it needs evidence."""

    if not numbers_in_evidence(text, tuple(claims)):
        return DocumentLine(
            text=text, fact=True, flagged=True, flag_reason="metric_not_in_evidence"
        )
    checked = evidence.check_statement(principal, text)
    if checked is not None and checked.supported is False:
        return DocumentLine(
            text=text, fact=True, flagged=True, flag_reason="unsupported_claim"
        )
    match = evidence.requirement(principal, text)
    if match is None or match.status is not FitStatus.SUPPORTED or not match.claims:
        return DocumentLine(
            text=text, fact=True, flagged=True, flag_reason="unverified_cv_line"
        )
    return fact(match.claims[:3], text)


__all__ = [
    "HEADERS",
    "OWNER_INPUT",
    "MotivationWriter",
    "build_cv",
    "build_letter",
    "build_proposal_outline",
    "build_research_statement",
    "checked_motivation",
    "make_document",
    "render",
    "revalidate",
]
