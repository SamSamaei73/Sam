"""ProfessionalClaimPolicy: protect first-person professional statements.

Before Sam says "I have 10 years of Python", "I worked at X", "I hold an MSc",
"I led a team", "I published a paper", or anything about salary, visa or
clearance, the statement is checked against the evidence. A protected claim with
no trusted evidence is reported UNSUPPORTED and must not be stated as fact.

Guarantees:

* Years of experience are computed by deterministic timeline code from resolved
  dates, using the CONSERVATIVE bound, never rounded up and never taken from a
  model. A claim of N years is supported only if the evidence covers at least
  N whole years.
* A claim resting only on INFERRED or UNVERIFIED evidence (a model suggestion, a
  dependency manifest, a publication topic) is unsupported.
* "Expert" language needs CORROBORATED (or owner-confirmed) evidence.
* Leadership, authorship position and personal contribution each need evidence
  that says exactly that: authorship alone never implies a contribution.

This policy detects claim TYPES with patterns; it is deliberately conservative
(it flags, it never certifies free-form prose). ``NOT_CHECKABLE`` means no
protected pattern matched, NOT that the statement is verified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from sam.professional.evidence import norm_text
from sam.professional.models import (
    ClaimCategory,
    ClaimReviewState,
    EvidenceStrength,
)
from sam.professional.profile import ClaimView, Profile
from sam.professional.vocabulary import find_mentions


class ProtectedKind(StrEnum):
    YEARS_OF_EXPERIENCE = "years_of_experience"
    EMPLOYER = "employer"
    JOB_TITLE = "job_title"
    EMPLOYMENT_DATES = "employment_dates"
    DEGREE = "degree"
    GRADE = "grade"
    CERTIFICATION = "certification"
    PUBLICATION = "publication"
    AUTHORSHIP_POSITION = "authorship_position"
    CONTRIBUTION = "contribution"
    LEADERSHIP = "leadership"
    QUANTIFIED_ACHIEVEMENT = "quantified_achievement"
    EXPERTISE = "expertise"
    SALARY = "salary"
    WORK_AUTHORIZATION = "work_authorization"
    CLEARANCE = "clearance"


class Verdict(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    NOT_CHECKABLE = "not_checkable"


@dataclass(frozen=True)
class Finding:
    kind: ProtectedKind
    supported: bool
    reason: str
    claim_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaimAssessment:
    verdict: Verdict
    findings: tuple[Finding, ...]

    @property
    def protected_kinds(self) -> tuple[ProtectedKind, ...]:
        return tuple(f.kind for f in self.findings)

    @property
    def requires_private_handling(self) -> bool:
        return any(
            f.kind
            in (
                ProtectedKind.SALARY,
                ProtectedKind.WORK_AUTHORIZATION,
                ProtectedKind.CLEARANCE,
            )
            for f in self.findings
        )


_YEARS = re.compile(r"(?i)\b(?P<n>\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b")
_ORG = re.compile(
    r"\b(?:worked|work|employed|interned|served|joined)\s+(?:at|for|with)\s+"
    r"(?P<org>[A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,3})"
)
_TITLE = re.compile(
    r"(?i)\b(?:as|was|am|been|became)\s+(?:an?|the)?\s*"
    r"(?P<title>(?:[a-z]+\s+){0,3}(?:engineer|developer|scientist|researcher|analyst|"
    r"manager|lead|architect|consultant|intern|lecturer|director|specialist|"
    r"founder|designer|programmer|professor))\b"
)
_DEGREE = re.compile(
    r"(?i)(?<![A-Za-z])(?P<deg>m\.?\s?sc\.?|m\.?\s?eng\.?|mba|ph\.?\s?d\.?|dphil|"
    r"b\.?\s?sc\.?|b\.?\s?eng\.?|master(?:'?s)?|bachelor(?:'?s)?|doctorate)(?![A-Za-z])"
)
_GRADE = re.compile(
    r"(?i)\b(?:first[- ]class|distinction|merit|2:1|2:2|upper second|lower second|"
    r"gpa\s*\d(?:\.\d+)?)\b"
)
_CERT = re.compile(r"(?i)\b(?:certified|certification|certificate)\b")
_PUBLISHED = re.compile(
    r"(?i)\b(?:published|publication|co-?authored|authored|peer[- ]reviewed|my paper)\b"
)
_FIRST_AUTHOR = re.compile(r"(?i)\b(?:first|lead|sole|corresponding)\s+author\b")
_CONTRIBUTION = re.compile(
    r"(?i)\b(?:i\s+(?:contributed|designed|implemented|developed|proposed|wrote)|"
    r"my\s+contribution)\b"
)
_LEAD = re.compile(
    r"(?i)\b(?:i\s+)?(?:led|lead|managed|headed|directed|supervised|mentored|"
    r"team\s+lead|spearheaded)\b"
)
_METRIC = re.compile(r"(?i)\d+(?:[.,]\d+)?\s*(?:%|x\b|×|percent)")
_EXPERT = re.compile(
    r"(?i)\b(?:expert|expertise|mastery|master\s+of|advanced|proficient|"
    r"highly\s+skilled)\b"
)
_SALARY = re.compile(
    r"(?i)\b(?:salary|earn|earning|compensation|expected\s+pay|day\s+rate)\b"
)
_AUTHORIZATION = re.compile(
    r"(?i)\b(?:visa|work\s+permit|right\s+to\s+work|work\s+authori[sz]ation|"
    r"sponsorship|citizen(?:ship)?)\b"
)
_CLEARANCE = re.compile(
    r"(?i)\b(?:security\s+clearance|clearance|sc\s+cleared|dv\s+cleared)\b"
)


def _has_verified(views: list[ClaimView]) -> list[ClaimView]:
    return [v for v in views if v.accepted]


class ProfessionalClaimPolicy:
    """Stateless: every check is a function of a ``Profile`` snapshot."""

    def assess(self, statement: str, profile: Profile) -> ClaimAssessment:
        text = statement.strip()
        findings: list[Finding] = []
        entries = find_mentions(text)
        skill_claims = [
            v
            for m in entries
            for v in profile.by_category(ClaimCategory.SKILL, ClaimCategory.TECHNOLOGY)
            if v.attributes.get("skill_id") == m.entry.skill_id
        ]

        if match := _YEARS.search(text):
            findings.append(self._years(float(match.group("n")), entries, profile))
        if match := _ORG.search(text):
            findings.append(self._employer(match.group("org"), profile))
        if match := _TITLE.search(text):
            findings.append(self._title(match.group("title"), profile))
        if _DEGREE.search(text):
            findings.append(self._degree(text, profile))
        if _GRADE.search(text):
            findings.append(self._grade(text, profile))
        if _CERT.search(text):
            findings.append(self._certification(text, profile))
        if _FIRST_AUTHOR.search(text):
            findings.append(self._authorship_position(profile))
        elif _PUBLISHED.search(text):
            findings.append(self._publication(text, profile))
        if _CONTRIBUTION.search(text):
            findings.append(self._contribution(profile))
        if _LEAD.search(text):
            findings.append(self._leadership(profile))
        if _METRIC.search(text):
            findings.append(self._quantified(text, profile))
        if _EXPERT.search(text):
            findings.append(self._expertise(entries, skill_claims))
        if _SALARY.search(text):
            findings.append(self._private(ProtectedKind.SALARY, _SALARY, profile))
        if _AUTHORIZATION.search(text):
            findings.append(
                self._private(ProtectedKind.WORK_AUTHORIZATION, _AUTHORIZATION, profile)
            )
        if _CLEARANCE.search(text):
            findings.append(self._private(ProtectedKind.CLEARANCE, _CLEARANCE, profile))

        if not findings:
            return ClaimAssessment(Verdict.NOT_CHECKABLE, ())
        verdict = (
            Verdict.SUPPORTED
            if all(f.supported for f in findings)
            else Verdict.UNSUPPORTED
        )
        return ClaimAssessment(verdict, tuple(findings))

    # ---------------------------------------------------------------- years

    @staticmethod
    def _years(number: float, entries: list, profile: Profile) -> Finding:  # type: ignore[type-arg]
        needed = int(number * 12)
        skill_id = entries[0].entry.skill_id if entries else None
        skill_claim = None
        if skill_id is not None:
            skill_claim = next(
                (
                    v
                    for v in profile.by_category(
                        ClaimCategory.SKILL, ClaimCategory.TECHNOLOGY
                    )
                    if v.attributes.get("skill_id") == skill_id
                ),
                None,
            )
            if skill_claim is None or not skill_claim.accepted:
                return Finding(
                    ProtectedKind.YEARS_OF_EXPERIENCE,
                    False,
                    "skill_not_verified",
                )
        months = profile.experience_months(
            skill_claim.claim_id if skill_claim else None, conservative=True
        )
        if months >= needed and months > 0:
            return Finding(
                ProtectedKind.YEARS_OF_EXPERIENCE,
                True,
                "covered_by_resolved_timeline",
                (skill_claim.claim_id,) if skill_claim else (),
            )
        return Finding(
            ProtectedKind.YEARS_OF_EXPERIENCE,
            False,
            "exceeds_resolved_timeline" if months else "no_timeline_evidence",
        )

    # ---------------------------------------------------------- employment

    @staticmethod
    def _employer(org: str, profile: Profile) -> Finding:
        wanted = norm_text(org)
        hits = [
            v.claim_id
            for v in _has_verified(profile.by_category(ClaimCategory.EMPLOYMENT))
            if wanted and wanted in norm_text(v.attributes.get("employer", ""))
        ]
        return Finding(
            ProtectedKind.EMPLOYER,
            bool(hits),
            "employment_evidence" if hits else "employer_not_in_evidence",
            tuple(hits),
        )

    @staticmethod
    def _title(title: str, profile: Profile) -> Finding:
        wanted = norm_text(title)
        hits = [
            v.claim_id
            for v in _has_verified(profile.by_category(ClaimCategory.EMPLOYMENT))
            if wanted and wanted in norm_text(v.attributes.get("title", ""))
        ]
        return Finding(
            ProtectedKind.JOB_TITLE,
            bool(hits),
            "title_evidence" if hits else "title_not_in_evidence",
            tuple(hits),
        )

    # ----------------------------------------------------------- education

    @staticmethod
    def _degree(text: str, profile: Profile) -> Finding:
        lowered = norm_text(text)
        hits = []
        for view in _has_verified(profile.by_category(ClaimCategory.EDUCATION)):
            degree = norm_text(view.attributes.get("degree", ""))
            long_form = {
                "msc": "master",
                "meng": "master",
                "ma": "master",
                "mba": "master",
                "phd": "doctor",
                "bsc": "bachelor",
                "beng": "bachelor",
                "ba": "bachelor",
            }.get(degree, degree)
            if degree and (
                degree in lowered.split() or long_form in lowered or degree in lowered
            ):
                hits.append(view.claim_id)
        return Finding(
            ProtectedKind.DEGREE,
            bool(hits),
            "education_evidence" if hits else "degree_not_in_evidence",
            tuple(hits),
        )

    @staticmethod
    def _grade(text: str, profile: Profile) -> Finding:
        lowered = norm_text(text)
        hits = []
        for view in _has_verified(profile.by_category(ClaimCategory.EDUCATION)):
            classification = norm_text(view.attributes.get("classification", ""))
            if classification and classification in lowered:
                hits.append(view.claim_id)
        return Finding(
            ProtectedKind.GRADE,
            bool(hits),
            "classification_evidence" if hits else "grade_not_in_evidence",
            tuple(hits),
        )

    @staticmethod
    def _certification(text: str, profile: Profile) -> Finding:
        lowered = norm_text(text)
        hits = [
            v.claim_id
            for v in _has_verified(profile.by_category(ClaimCategory.CERTIFICATION))
            if norm_text(v.attributes.get("name", "")) in lowered
        ]
        return Finding(
            ProtectedKind.CERTIFICATION,
            bool(hits),
            "certification_evidence" if hits else "certification_not_in_evidence",
            tuple(hits),
        )

    # -------------------------------------------------------- publications

    @staticmethod
    def _publication(text: str, profile: Profile) -> Finding:
        lowered = norm_text(text)
        candidates = _has_verified(profile.by_category(ClaimCategory.PUBLICATION))
        titled = [
            v for v in candidates if norm_text(v.attributes.get("title", "")) in lowered
        ]
        hits = titled or (candidates if not _quoted(text) else [])
        return Finding(
            ProtectedKind.PUBLICATION,
            bool(hits),
            "publication_evidence" if hits else "publication_not_in_evidence",
            tuple(v.claim_id for v in hits),
        )

    @staticmethod
    def _authorship_position(profile: Profile) -> Finding:
        hits = [
            v.claim_id
            for v in _has_verified(profile.by_category(ClaimCategory.PUBLICATION))
            if v.attributes.get("owner_author_position") == "1"
        ]
        return Finding(
            ProtectedKind.AUTHORSHIP_POSITION,
            bool(hits),
            "author_position_evidence" if hits else "author_position_not_evidenced",
            tuple(hits),
        )

    @staticmethod
    def _contribution(profile: Profile) -> Finding:
        # Authorship alone never implies a specific contribution.
        hits = [
            v.claim_id
            for v in _has_verified(profile.by_category(ClaimCategory.PUBLICATION))
            if v.attributes.get("owner_contribution")
        ]
        hits += [
            v.claim_id
            for v in _has_verified(
                profile.by_category(ClaimCategory.RESPONSIBILITY, ClaimCategory.PROJECT)
            )
        ]
        return Finding(
            ProtectedKind.CONTRIBUTION,
            bool(hits),
            "contribution_evidence" if hits else "contribution_not_evidenced",
            tuple(hits),
        )

    # ----------------------------------------------------------- the rest

    @staticmethod
    def _leadership(profile: Profile) -> Finding:
        hits = [
            v.claim_id
            for v in _has_verified(
                profile.by_category(
                    ClaimCategory.RESPONSIBILITY,
                    ClaimCategory.ACHIEVEMENT,
                    ClaimCategory.EMPLOYMENT,
                )
            )
            if _LEAD.search(v.statement)
            or re.search(r"(?i)\b(?:lead|manager|head|director)\b", v.statement)
        ]
        return Finding(
            ProtectedKind.LEADERSHIP,
            bool(hits),
            "leadership_evidence" if hits else "leadership_not_evidenced",
            tuple(hits),
        )

    @staticmethod
    def _quantified(text: str, profile: Profile) -> Finding:
        numbers = {m.group(0).replace(" ", "").lower() for m in _METRIC.finditer(text)}
        hits = []
        for view in _has_verified(profile.by_category(ClaimCategory.ACHIEVEMENT)):
            haystack = view.statement.replace(" ", "").lower()
            if numbers and all(n in haystack for n in numbers):
                hits.append(view.claim_id)
        return Finding(
            ProtectedKind.QUANTIFIED_ACHIEVEMENT,
            bool(hits),
            "achievement_evidence" if hits else "figure_not_in_evidence",
            tuple(hits),
        )

    @staticmethod
    def _expertise(entries: list, skill_claims: list[ClaimView]) -> Finding:  # type: ignore[type-arg]
        strong = [
            v.claim_id
            for v in skill_claims
            if v.strength is EvidenceStrength.CORROBORATED
            or (v.accepted and v.review is ClaimReviewState.OWNER_CONFIRMED)
        ]
        if strong:
            return Finding(
                ProtectedKind.EXPERTISE, True, "corroborated_skill", tuple(strong)
            )
        return Finding(
            ProtectedKind.EXPERTISE,
            False,
            "needs_corroborated_evidence" if entries else "no_skill_named",
        )

    @staticmethod
    def _private(
        kind: ProtectedKind, pattern: re.Pattern[str], profile: Profile
    ) -> Finding:
        hits = [
            v.claim_id
            for v in _has_verified(profile.by_category(ClaimCategory.CAREER_PREFERENCE))
            if pattern.search(v.statement)
        ]
        return Finding(
            kind,
            bool(hits),
            "owner_stated" if hits else "not_stated_by_owner_sources",
            tuple(hits),
        )


def _quoted(text: str) -> bool:
    return bool(re.search(r"[“\"‘'][^”\"’']{10,}[”\"’']", text))


__all__ = [
    "ClaimAssessment",
    "Finding",
    "ProfessionalClaimPolicy",
    "ProtectedKind",
    "Verdict",
]
