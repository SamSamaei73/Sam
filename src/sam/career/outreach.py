"""Local outreach drafts. Drafting is never sending.

A draft is built from: a greeting (the contact's evidence-backed name), a line
on why the opportunity or research is relevant (quoting only what the source
states, never the contact's interests), evidence-backed owner background lines,
the owner's own proposed direction or note (claim-checked), and a concise
question. Every line passes the claim policy; a flagged line blocks approval.

Channels: only EMAIL can ever be sent, and only through Sam's controlled e-mail
tool boundary with CAREER SEND plus that tool's own permission (see
``CareerService.send_outreach``). LinkedIn connection notes / messages and
recruiter-platform messages are DRAFT ONLY in Phase 16: Sam has no integration
that sends them and never automates LinkedIn.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sam.career.claims import ClaimChecker
from sam.career.documents import checked_motivation, fact, note, render
from sam.career.evidence import EvidenceClaim
from sam.career.matching import AlignmentReport, FitReport
from sam.career.models import (
    CareerOpportunity,
    Contact,
    DocumentLine,
    FitStatus,
    OutreachChannel,
    OutreachDraft,
    OutreachKind,
    clean_text,
    sha256,
)
from sam.permissions.models import Principal

SENDABLE_CHANNELS = frozenset({OutreachChannel.EMAIL})
LINKEDIN_NOTE_LIMIT = 300


def build_outreach(
    principal: Principal,
    *,
    outreach_id: str,
    kind: OutreachKind,
    channel: OutreachChannel,
    contact: Contact,
    opportunity: CareerOpportunity | None,
    fit: FitReport | None,
    alignment: AlignmentReport | None,
    note_text: str | None,
    claims: Sequence[EvidenceClaim],
    checker: ClaimChecker,
    now: datetime,
) -> OutreachDraft:
    lines: list[DocumentLine] = [note(f"Dear {contact.name},")]
    if opportunity is not None:
        what = (
            "PhD opportunity"
            if kind in (OutreachKind.SUPERVISOR, OutreachKind.PHD_INQUIRY)
            else "role"
        )
        lines.append(
            note(
                f"I am writing about the {what} “{opportunity.title}” "
                f"at {opportunity.organization}."
            )
        )
    topics = contact.research_topics or (
        opportunity.research_topics if opportunity else ()
    )
    if topics and kind in (OutreachKind.SUPERVISOR, OutreachKind.PHD_INQUIRY):
        lines.append(
            note("Your listed research areas include " + ", ".join(topics[:3]) + ".")
        )
    background: list[EvidenceClaim] = []
    if alignment is not None:
        background += [c for t in alignment.topics for c in t.claims]
    if fit is not None:
        background += [
            c
            for f in fit.requirements
            if f.status is FitStatus.SUPPORTED
            for c in f.claims
        ]
    seen: dict[str, EvidenceClaim] = {}
    for claim in background:
        seen.setdefault(claim.claim_id, claim)
    for claim in list(seen.values())[:3]:
        lines.append(fact([claim], f"Relevant background: {render(claim)}."))
    if note_text:
        lines.append(checked_motivation(principal, note_text, claims, checker))
    question = (
        "Would you be open to a short conversation about whether this direction "
        "could fit your group?"
        if kind in (OutreachKind.SUPERVISOR, OutreachKind.PHD_INQUIRY)
        else "Would you be open to a short conversation about the role?"
    )
    lines += [note(question), note("Kind regards,")]
    if channel is OutreachChannel.LINKEDIN_CONNECTION_NOTE:
        lines = _fit_note(lines)
    subject = ""
    if channel is OutreachChannel.EMAIL and opportunity is not None:
        subject = clean_text(f"Enquiry: {opportunity.title}", 200)
    body = "\n".join(line.text for line in lines)
    return OutreachDraft(
        outreach_id=outreach_id,
        kind=kind,
        channel=channel,
        contact_id=contact.contact_id,
        opportunity_id=opportunity.opportunity_id if opportunity else None,
        subject=subject,
        lines=tuple(lines),
        sha256=sha256(f"{channel.value}\n{subject}\n{body}"),
        created_at=now,
    )


def _fit_note(lines: list[DocumentLine]) -> list[DocumentLine]:
    kept: list[DocumentLine] = []
    total = 0
    for line in lines:
        if total + len(line.text) + 1 > LINKEDIN_NOTE_LIMIT:
            break
        kept.append(line)
        total += len(line.text) + 1
    return kept


__all__ = ["LINKEDIN_NOTE_LIMIT", "SENDABLE_CHANNELS", "build_outreach"]
