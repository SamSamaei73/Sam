"""Requirement-to-evidence matching.

``match_requirement`` answers "what does the owner's evidence say about this
requirement?" deterministically. It never fabricates evidence to improve the
fit: a skill counts only if an ACCEPTED claim (documentary evidence, or the
owner's attestation) supports it; an inferred relationship or an unreviewed
model candidate is reported as such and does NOT match. A
requirement it cannot recognise at all is UNKNOWN, never guessed.

Recognised aspects: named skills (from the trusted vocabulary), a "production"
qualifier (needs employment evidence, not only project evidence), years of
experience (from the deterministic timeline), degree level, publications and
leadership. Everything else is left unrecognised rather than invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sam.professional.models import ClaimCategory, MatchStatus
from sam.professional.profile import ClaimView, Profile
from sam.professional.timeline import Duration
from sam.professional.vocabulary import find_mentions

_PRODUCTION = re.compile(r"(?i)\b(?:production|in\s+prod|deployed|live\s+systems?)\b")
_YEARS = re.compile(r"(?i)\b(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b")
_DEGREE_LEVEL = re.compile(
    r"(?i)\b(?P<level>ph\.?\s?d\.?|doctorate|master(?:'?s)?|m\.?\s?sc\.?|bachelor(?:'?s)?|"
    r"b\.?\s?sc\.?|degree)\b"
)
_PUBLICATIONS = re.compile(
    r"(?i)\b(?:publications?|published|peer[- ]reviewed|research\s+papers?)\b"
)
_LEADERSHIP = re.compile(r"(?i)\b(?:leadership|lead|led|manage|managed|mentor)\b")

_LEVELS = {
    "phd": "phd",
    "ph.d": "phd",
    "doctorate": "phd",
    "master": "master",
    "masters": "master",
    "msc": "master",
    "m.sc": "master",
    "bachelor": "bachelor",
    "bachelors": "bachelor",
    "bsc": "bachelor",
    "b.sc": "bachelor",
}
_DEGREE_TO_LEVEL = {
    "msc": "master",
    "meng": "master",
    "ma": "master",
    "mba": "master",
    "phd": "phd",
    "bsc": "bachelor",
    "beng": "bachelor",
    "ba": "bachelor",
}


@dataclass(frozen=True)
class RequirementEvidence:
    status: MatchStatus
    normalized_requirement: str
    matched_claims: tuple[ClaimView, ...]
    skills: tuple[ClaimView, ...]
    inferred_skills: tuple[ClaimView, ...]
    projects: tuple[ClaimView, ...]
    employment: tuple[ClaimView, ...]
    research: tuple[ClaimView, ...]
    education: tuple[ClaimView, ...]
    unsupported_aspects: tuple[str, ...]
    notes: tuple[str, ...]


def _dedupe(views: list[ClaimView]) -> tuple[ClaimView, ...]:
    seen: dict[str, ClaimView] = {}
    for view in views:
        seen.setdefault(view.claim_id, view)
    return tuple(sorted(seen.values(), key=lambda v: v.claim_id))


def match_requirement(requirement: str, profile: Profile) -> RequirementEvidence:
    text = requirement.strip()
    labels: list[str] = []
    unsupported: list[str] = []
    notes: list[str] = []
    satisfied = 0
    total = 0
    skills: list[ClaimView] = []
    inferred: list[ClaimView] = []
    projects: list[ClaimView] = []
    employment: list[ClaimView] = []
    research: list[ClaimView] = []
    education: list[ClaimView] = []
    skill_claim_ids: list[str] = []
    has_employment_context = False

    skill_index = {
        (v.attributes.get("skill_id") or ""): v
        for v in profile.by_category(ClaimCategory.SKILL, ClaimCategory.TECHNOLOGY)
    }
    seen_skills: dict[str, object] = {}
    for mention in find_mentions(text):
        entry = mention.entry
        if entry.skill_id in seen_skills:
            continue
        seen_skills[entry.skill_id] = entry
        total += 1
        labels.append(entry.display)
        view = skill_index.get(entry.skill_id)
        if view is None:
            unsupported.append(f"skill: {entry.display} (no evidence)")
        elif view.accepted:
            satisfied += 1
            skills.append(view)
            skill_claim_ids.append(view.claim_id)
            for context in profile.graph.contexts_for(view.claim_id):
                context_view = profile.view(context.claim_id)
                if context_view is None or not context_view.accepted:
                    continue
                if context.category is ClaimCategory.PROJECT:
                    projects.append(context_view)
                elif context.category is ClaimCategory.EMPLOYMENT:
                    employment.append(context_view)
                    has_employment_context = True
                elif context.category is ClaimCategory.PUBLICATION:
                    research.append(context_view)
                elif context.category is ClaimCategory.EDUCATION:
                    education.append(context_view)
        elif view.inferred:
            inferred.append(view)
            unsupported.append(f"skill: {entry.display} (inferred only, not verified)")
        else:
            unsupported.append(f"skill: {entry.display} (unverified candidate)")

    if _PRODUCTION.search(text):
        total += 1
        labels.append("production use")
        if has_employment_context:
            satisfied += 1
        else:
            unsupported.append(
                "production use: no employment evidence (project evidence alone "
                "does not show production use)"
            )

    if years := _YEARS.search(text):
        total += 1
        needed = int(float(years.group(1)) * 12)
        labels.append(f"{years.group(1)}+ years")
        months = profile.experience_months(
            skill_claim_ids[0] if len(skill_claim_ids) == 1 else None
        )
        if months >= needed and months > 0:
            satisfied += 1
        else:
            unsupported.append(
                f"years: needs {years.group(1)}, resolved evidence covers "
                f"{Duration(months).label()}"
            )

    if level := _DEGREE_LEVEL.search(text):
        total += 1
        wanted = _LEVELS.get(level.group("level").lower().replace(" ", ""), "any")
        labels.append(f"degree ({wanted})")
        matches = [
            v
            for v in profile.by_category(ClaimCategory.EDUCATION, accepted_only=True)
            if wanted == "any"
            or _DEGREE_TO_LEVEL.get(v.attributes.get("degree", "").lower()) == wanted
        ]
        if matches:
            satisfied += 1
            education.extend(matches)
        else:
            unsupported.append(f"degree: no verified {wanted} degree in the evidence")

    if _PUBLICATIONS.search(text):
        total += 1
        labels.append("publications")
        pubs = profile.by_category(ClaimCategory.PUBLICATION, accepted_only=True)
        if pubs:
            satisfied += 1
            research.extend(pubs)
        else:
            unsupported.append("publications: none in the evidence")

    if _LEADERSHIP.search(text):
        total += 1
        labels.append("leadership")
        proof = [
            v
            for v in profile.by_category(
                ClaimCategory.RESPONSIBILITY,
                ClaimCategory.ACHIEVEMENT,
                ClaimCategory.EMPLOYMENT,
                accepted_only=True,
            )
            if re.search(
                r"(?i)\b(?:led|lead|leader|manag\w*|mentor\w*|head|director)\b",
                v.statement,
            )
        ]
        if proof:
            satisfied += 1
            employment.extend(
                v for v in proof if v.category is ClaimCategory.EMPLOYMENT
            )
        else:
            unsupported.append("leadership: no evidence that says so")

    if total == 0:
        return RequirementEvidence(
            status=MatchStatus.UNKNOWN,
            normalized_requirement="",
            matched_claims=(),
            skills=(),
            inferred_skills=(),
            projects=(),
            employment=(),
            research=(),
            education=(),
            unsupported_aspects=(),
            notes=("no recognised skill or qualification in the requirement",),
        )

    if satisfied == total:
        status = MatchStatus.MATCHED
    elif satisfied == 0:
        status = MatchStatus.NOT_SUPPORTED
    else:
        status = MatchStatus.PARTIALLY_SUPPORTED
    matched = _dedupe([*skills, *projects, *employment, *research, *education])
    return RequirementEvidence(
        status=status,
        normalized_requirement="; ".join(labels),
        matched_claims=matched,
        skills=_dedupe(skills),
        inferred_skills=_dedupe(inferred),
        projects=_dedupe(projects),
        employment=_dedupe(employment),
        research=_dedupe(research),
        education=_dedupe(education),
        unsupported_aspects=tuple(unsupported),
        notes=tuple(notes),
    )


__all__ = ["RequirementEvidence", "match_requirement"]
