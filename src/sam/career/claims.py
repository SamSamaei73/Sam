"""The application claim policy: every factual first-person claim in a CV, cover
letter, answer, outreach message, research statement or proposal must be backed
by Phase 14 evidence, or it is FLAGGED for the owner (and a flagged line blocks
approval and submission).

A sentence is factual when it states something about the owner's history or
status ("I have 5 years ...", "I led ...", "I deployed ...", "I published ...",
"I improved ... by 30%", "I graduated with ...", "I have work authorization ...").
Such a sentence is checked through the ``EvidencePort``:

* the Professional claim policy says UNSUPPORTED -> flagged;
* nothing checkable and no evidence-backed match for the sentence -> flagged.

Additionally, deterministically and regardless of evidence:

* "I read your paper ..." needs a paper Sam actually ingested (``PaperPort``);
* statements about a supervisor's or recruiter's interest are never made;
* a number or metric must appear verbatim in the owner's evidence.

Motivation and interest language that states no owner fact passes unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sam.career.evidence import EvidenceClaim, EvidencePort, PaperPort
from sam.career.models import FitStatus
from sam.permissions.models import Principal

_FACTUAL = re.compile(
    r"(?i)\b(?:i|i've|i'm|we)\s+(?:have\s+|had\s+|was\s+|am\s+|also\s+)?"
    r"(?:led|lead|managed|deployed|built|shipped|launched|improved|increased|reduced|"
    r"published|graduated|hold|earned|received|won|completed|worked|designed|implemented|"
    r"developed|authored|co-?authored|contributed|delivered|architected|mentored|supervised|"
    r"founded|created|trained|taught|presented|obtained)\b"
    r"|\b(?:i\s+have|i've\s+got|with)\s+(?:over\s+|more\s+than\s+|nearly\s+)?\d+\+?\s*(?:years?|yrs?)\b"
    r"|\bi\s+have\s+(?:production|professional|commercial|industry|hands-on)\s+experience\b"
    r"|\bi\s+(?:am|'m)\s+(?:a|an|the)\s+[a-z ]*(?:engineer|developer|scientist|"
    r"researcher|"
    r"lead|manager|architect|graduate|student|author)\b"
    r"|\bi\s+(?:have|hold)\s+(?:a|an)\s+[a-z. ]*(?:degree|msc|bsc|phd|master|bachelor|"
    r"certification|certificate|distinction)\b"
    r"|\bi\s+(?:have|hold)\s+(?:the\s+)?(?:right\s+to\s+work|work\s+authori[sz]ation|"
    r"(?:a\s+)?(?:work\s+)?visa|security\s+clearance)\b"
    r"|\bmy\s+(?:paper|publication|thesis|dissertation|degree|contribution)\b"
)
_READ_PAPER = re.compile(
    r"(?i)\bi\s+(?:have\s+|recently\s+)?read\s+(?:your|the|their)\s+(?:recent\s+)?"
    r"(?:paper|article|publication|work)\b(?:\s+(?:on|about|titled|called)\s+"
    r"[\"“']?(?P<title>[^\"”'.]{3,160}))?"
)
_THIRD_PARTY_INTEREST = re.compile(
    r"(?i)\b(?:you|your\s+(?:group|lab|team))\s+(?:are|is|would\s+be|will\s+be)\s+"
    r"(?:interested|keen|looking\s+for|recruiting|excited)\b"
    r"|\b(?:you|he|she|they)\s+(?:want|wants|need|needs)\s+(?:a|an|someone)\b"
)
_PAPER = re.compile(r"(?i)\b(?:paper|publication|article|thesis|preprint)\b")
_CONTRIBUTION_CLAIM = re.compile(
    r"(?i)\b(?:i\s+(?:designed|proposed|implemented|developed|wrote|built|led|"
    r"contributed|devised|created|introduced|ran)|my\s+(?:own\s+)?contribution)\b"
)
_AUTHOR_POSITION = re.compile(
    r"(?i)\bas\s+(?:the\s+)?(?:first|lead|sole|corresponding)\s+author\b"
)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?\s*(?:%|x\b|×|percent|k\b|m\b)?")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class LineCheck:
    flagged: bool
    reason: str | None = None


def is_factual(text: str) -> bool:
    return bool(_FACTUAL.search(text))


def _numbers(text: str) -> set[str]:
    return {m.group(0).strip().replace(" ", "") for m in _NUMBER.finditer(text)}


def numbers_in_evidence(text: str, claims: tuple[EvidenceClaim, ...]) -> bool:
    """Every number/metric in ``text`` appears in some accepted claim."""

    wanted = _numbers(text)
    if not wanted:
        return True
    corpus = " ".join(
        [c.statement for c in claims]
        + [v for c in claims for v in c.attributes.values()]
    )
    have = _numbers(corpus)
    return wanted <= have


class ClaimChecker:
    def __init__(self, evidence: EvidencePort, papers: PaperPort) -> None:
        self._evidence = evidence
        self._papers = papers

    def check(
        self,
        principal: Principal,
        text: str,
        claims: tuple[EvidenceClaim, ...],
    ) -> LineCheck:
        for sentence in _SENTENCE.split(text.strip()) or [text]:
            result = self._sentence(principal, sentence, claims)
            if result.flagged:
                return result
        return LineCheck(False)

    def _sentence(
        self,
        principal: Principal,
        sentence: str,
        claims: tuple[EvidenceClaim, ...],
    ) -> LineCheck:
        if _THIRD_PARTY_INTEREST.search(sentence):
            return LineCheck(True, "third_party_interest_not_known")
        read = _READ_PAPER.search(sentence)
        if read:
            title = (read.group("title") or "").strip()
            if not title or self._papers.retrieved(principal, title) is None:
                return LineCheck(True, "paper_not_retrieved")
        if _PAPER.search(sentence):
            # Stricter than the general policy: a contribution to a SPECIFIC
            # paper needs the paper's own recorded owner contribution / position.
            if _AUTHOR_POSITION.search(sentence) and not any(
                c.category == "publication"
                and c.attributes.get("owner_author_position") == "1"
                for c in claims
            ):
                return LineCheck(True, "author_position_not_evidenced")
            if _CONTRIBUTION_CLAIM.search(sentence) and not any(
                c.category == "publication" and c.attributes.get("owner_contribution")
                for c in claims
            ):
                return LineCheck(True, "publication_contribution_not_evidenced")
        if not is_factual(sentence):
            return LineCheck(False)
        if not numbers_in_evidence(sentence, claims):
            return LineCheck(True, "metric_not_in_evidence")
        checked = self._evidence.check_statement(principal, sentence)
        if checked is None:
            return LineCheck(True, "evidence_unavailable")
        if checked.supported is False:
            return LineCheck(True, "unsupported_claim")
        if checked.supported is True:
            return LineCheck(False)
        match = self._evidence.requirement(principal, sentence)
        if match is None or match.status is not FitStatus.SUPPORTED:
            return LineCheck(True, "no_evidence_for_claim")
        return LineCheck(False)


__all__ = ["ClaimChecker", "LineCheck", "is_factual", "numbers_in_evidence"]
