"""Deterministic classification of application questions and safe pre-filling.

Three classes:

* SAFE: owner-approved contact details (name, e-mail, phone, portfolio,
  LinkedIn profile) copied from ``CareerPreferences.contact``; if the owner has
  not set a value, the question stays unresolved;
* EVIDENCE_BACKED: technical-experience answers built only from Phase 14
  evidence (with the evidence links); if there is none, unresolved;
* OWNER_REVIEW_REQUIRED: salary, notice period, relocation, work authorization,
  sponsorship, disability, demographic / equality, criminal history, legal
  attestations, conflicts / non-competes, references, consent / privacy
  declarations, availability commitments, motivation, and anything unrecognised.

Sensitive answers are NEVER inferred or pre-filled, and an answer given for one
application is never reused as authority for another: each draft starts with no
owner answers. Sensitive checks come first, so a question that mentions both a
safe and a sensitive topic ("your e-mail and visa status") is sensitive.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from sam.career.evidence import EvidenceClaim
from sam.career.matching import FitReport
from sam.career.models import (
    AnswerSource,
    ApplicationQuestion,
    CareerPreferences,
    DraftAnswer,
    FitStatus,
    QuestionClass,
    QuestionKind,
    clean_text,
)

_RULES: tuple[tuple[QuestionKind, re.Pattern[str]], ...] = (
    (
        QuestionKind.CRIMINAL_HISTORY,
        re.compile(r"(?i)\b(criminal|convict|offen[cs]e|arrest|dbs|background check)"),
    ),
    (
        QuestionKind.DISABILITY,
        re.compile(
            r"(?i)\b(disab|impairment|reasonable adjustment|accommodation for|"
            r"health condition|neurodiver)"
        ),
    ),
    (
        QuestionKind.DEMOGRAPHIC,
        re.compile(
            r"(?i)\b(gender|sex\b|ethnic|race\b|religio|belief|sexual orientation|"
            r"age\b|date of birth|veteran|pronoun|marital|pregnan|"
            r"caring responsibilit|equal opportunit|diversity monitoring|"
            r"nationality)"
        ),
    ),
    (QuestionKind.SPONSORSHIP, re.compile(r"(?i)\bsponsor")),
    (
        QuestionKind.WORK_AUTHORIZATION,
        re.compile(
            r"(?i)\b(right to work|work authori[sz]|authori[sz]ed to work|"
            r"work permit|visa|citizenship|immigration|eligible to work|"
            r"legally (entitled|able) to work)"
        ),
    ),
    (
        QuestionKind.SALARY,
        re.compile(
            r"(?i)\b(salary|compensation|pay expectation|expected pay|"
            r"rate expectation|day rate|remuneration|current pay)"
        ),
    ),
    (
        QuestionKind.NOTICE_PERIOD,
        re.compile(r"(?i)\b(notice period|when can you start|start date)"),
    ),
    (QuestionKind.RELOCATION, re.compile(r"(?i)\breloca")),
    (
        QuestionKind.LEGAL_ATTESTATION,
        re.compile(
            r"(?i)\b(i (certify|confirm|declare|attest)|true and (complete|"
            r"accurate)|legally binding|declaration|attest)"
        ),
    ),
    (
        QuestionKind.CONFLICT_NONCOMPETE,
        re.compile(
            r"(?i)\b(non-?compete|conflict of interest|restrictive covenant|"
            r"non-?solicit)"
        ),
    ),
    (QuestionKind.REFERENCES, re.compile(r"(?i)\breferee|\breferences?\b")),
    (
        QuestionKind.CONSENT_PRIVACY,
        re.compile(
            r"(?i)\b(consent|privacy (notice|policy)|data protection|gdpr|agree to)"
        ),
    ),
    (
        QuestionKind.AVAILABILITY,
        re.compile(
            r"(?i)\b(availab|full[- ]time|part[- ]time|work weekends|shift|travel)"
        ),
    ),
    (
        QuestionKind.MOTIVATION,
        re.compile(
            r"(?i)\b(why (do you|are you)|motivat|interest(ed)? in|cover letter)"
        ),
    ),
    (
        QuestionKind.TECHNICAL_EXPERIENCE,
        re.compile(
            r"(?i)\b(experience (with|in|using)|describe (a|your)|have you (used|"
            r"worked)|proficien|familiar with|skills? in)"
        ),
    ),
    (QuestionKind.LINKEDIN_PROFILE, re.compile(r"(?i)\blinkedin")),
    (
        QuestionKind.PORTFOLIO,
        re.compile(r"(?i)\b(portfolio|github|website|personal site)"),
    ),
    (QuestionKind.EMAIL, re.compile(r"(?i)\be-?mail")),
    (QuestionKind.PHONE, re.compile(r"(?i)\b(phone|mobile|telephone)")),
    (
        QuestionKind.NAME,
        re.compile(
            r"(?i)\b(full name|first name|last name|surname|your name|legal name)"
        ),
    ),
)
_SAFE = frozenset(
    {
        QuestionKind.NAME,
        QuestionKind.EMAIL,
        QuestionKind.PHONE,
        QuestionKind.PORTFOLIO,
        QuestionKind.LINKEDIN_PROFILE,
    }
)
SENSITIVE = frozenset(
    {
        QuestionKind.SALARY,
        QuestionKind.NOTICE_PERIOD,
        QuestionKind.RELOCATION,
        QuestionKind.WORK_AUTHORIZATION,
        QuestionKind.SPONSORSHIP,
        QuestionKind.DISABILITY,
        QuestionKind.DEMOGRAPHIC,
        QuestionKind.CRIMINAL_HISTORY,
        QuestionKind.LEGAL_ATTESTATION,
        QuestionKind.CONFLICT_NONCOMPETE,
        QuestionKind.REFERENCES,
        QuestionKind.CONSENT_PRIVACY,
        QuestionKind.AVAILABILITY,
    }
)


def classify(text: str) -> tuple[QuestionKind, QuestionClass]:
    for kind, pattern in _RULES:
        if pattern.search(text):
            if kind in _SAFE:
                return kind, QuestionClass.SAFE
            if kind is QuestionKind.TECHNICAL_EXPERIENCE:
                return kind, QuestionClass.EVIDENCE_BACKED
            return kind, QuestionClass.OWNER_REVIEW_REQUIRED
    return QuestionKind.OTHER, QuestionClass.OWNER_REVIEW_REQUIRED


def make_questions(texts: Sequence[str]) -> tuple[ApplicationQuestion, ...]:
    out: list[ApplicationQuestion] = []
    for index, raw in enumerate(texts[:40]):
        text = clean_text(raw, 500)
        if not text:
            continue
        kind, cls = classify(text)
        out.append(
            ApplicationQuestion(
                question_id=f"q{index + 1}", text=text, kind=kind, classification=cls
            )
        )
    return tuple(out)


def _contact_value(kind: QuestionKind, prefs: CareerPreferences) -> str | None:
    c = prefs.contact
    return {
        QuestionKind.NAME: c.name,
        QuestionKind.EMAIL: c.email,
        QuestionKind.PHONE: c.phone,
        QuestionKind.PORTFOLIO: c.portfolio_url,
        QuestionKind.LINKEDIN_PROFILE: c.linkedin_url,
    }.get(kind)


def prefill(
    questions: Sequence[ApplicationQuestion],
    prefs: CareerPreferences,
    fit: FitReport,
) -> tuple[DraftAnswer, ...]:
    """Answers Sam may fill WITHOUT the owner. Sensitive ones: never."""

    from sam.career.documents import render as render_claim  # local: avoid a cycle

    answers: list[DraftAnswer] = []
    supported: list[EvidenceClaim] = [
        c for f in fit.requirements if f.status is FitStatus.SUPPORTED for c in f.claims
    ]
    for q in questions:
        if q.classification is QuestionClass.SAFE:
            value = _contact_value(q.kind, prefs)
            if value:
                answers.append(
                    DraftAnswer(
                        question_id=q.question_id,
                        text=value,
                        source=AnswerSource.OWNER_PROFILE,
                    )
                )
        elif q.classification is QuestionClass.EVIDENCE_BACKED and supported:
            chosen = supported[:3]
            text = (
                "Evidence from my record: "
                + "; ".join(render_claim(c) for c in chosen)
                + "."
            )
            answers.append(
                DraftAnswer(
                    question_id=q.question_id,
                    text=clean_text(text, 2_000),
                    source=AnswerSource.EVIDENCE,
                    evidence=tuple(c.link for c in chosen),
                )
            )
    return tuple(answers)


def unresolved(
    questions: Sequence[ApplicationQuestion], answers: Sequence[DraftAnswer]
) -> tuple[ApplicationQuestion, ...]:
    answered = {a.question_id for a in answers if a.text.strip()}
    return tuple(q for q in questions if q.required and q.question_id not in answered)


__all__ = ["SENSITIVE", "classify", "make_questions", "prefill", "unresolved"]
