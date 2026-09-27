"""Optional model-assisted candidate extraction. Untrusted end to end.

A model may SUGGEST professional claims from a source's text. Nothing it says is
authority:

* the source text is checked for secrets first, and the provider is called
  through the Phase 13 router with the OWNER-SELECTED privacy class, so a PRIVATE
  source never reaches Gemini Free by default and a SECRET source is never sent;
* the reply is untrusted JSON, validated against a strict schema: unknown keys
  (a date, a verification state, a permission) are ignored and counted, never
  applied;
* every candidate must QUOTE the source, and the quote must appear verbatim in
  the source text (provenance validation), otherwise it is rejected;
* an accepted candidate becomes a claim with ``CANDIDATE`` evidence only, so it
  is UNVERIFIED, whatever the model says. It can never be DIRECT or CORROBORATED,
  cannot change a date, and an unknown skill gets no canonical id and can never
  be promoted;
* a provider refusal or failure changes nothing that is already stored.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sam.agent.models import Message, MessageRole
from sam.models.models import (
    Capability,
    FailureCategory,
    ModelRequest,
    PrivacyClass,
)
from sam.models.router import ModelRouter
from sam.professional.evidence import norm_text
from sam.professional.models import ClaimCategory, SourceLocation, clean_text
from sam.professional.reader import SourceSegment
from sam.professional.safety import contains_secret, is_identifier_line, scrub
from sam.professional.vocabulary import normalize_skill

MAX_CANDIDATES = 50
MAX_PROMPT_CHARS = 60_000

_ALLOWED_KEYS = frozenset({"category", "statement", "skill", "quote"})

_INSTRUCTIONS = (
    "You extract professional facts about the document's owner from the text "
    'below. Reply with JSON only: {"candidates": [{"category": one of '
    f'{[c.value for c in ClaimCategory]}, "statement": short factual sentence, '
    '"skill": skill name if the category is skill or technology, '
    '"quote": an EXACT quote from the text that supports it}]}. '
    "Include only what the text explicitly says. Do not infer, exaggerate, add "
    "dates, or add anything the text does not state."
)


@dataclass(frozen=True)
class Candidate:
    category: ClaimCategory
    key: str
    statement: str
    attributes: dict[str, str]
    location: SourceLocation
    reference: str
    canonical: bool


@dataclass
class CandidateOutcome:
    status: str = "off"  # off | completed | blocked | failed | invalid
    accepted: list[Candidate] = field(default_factory=list)
    proposed: int = 0
    rejected: int = 0
    ignored_fields: int = 0


class CandidateExtractor(Protocol):
    def propose(
        self,
        *,
        segments: Sequence[SourceSegment],
        privacy_class: PrivacyClass,
    ) -> CandidateOutcome: ...


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def find_quote(
    quote: str, segments: Sequence[SourceSegment]
) -> tuple[SourceLocation, str] | None:
    """Locate ``quote`` verbatim (whitespace- and case-insensitive) in a
    segment. Returns its provenance, or ``None`` if the text never says it."""

    tokens = quote.split()
    if len(_squash(quote)) < 8 or not tokens:
        return None
    pattern = re.compile(r"\s+".join(re.escape(t) for t in tokens), re.IGNORECASE)
    for segment in segments:
        match = pattern.search(segment.text)
        if match is not None:
            location = SourceLocation(
                page_number=segment.location.page_number,
                section_title=segment.location.section_title,
                paragraph_index=segment.location.paragraph_index,
                character_start=match.start(),
                character_end=match.end(),
            )
            return location, match.group(0)
    return None


def validate_candidates(
    payload: object, segments: Sequence[SourceSegment]
) -> CandidateOutcome:
    """Turn an UNTRUSTED model reply into accepted candidates. Pure."""

    outcome = CandidateOutcome(status="completed")
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        return CandidateOutcome(status="invalid")
    for raw in payload["candidates"][:MAX_CANDIDATES]:
        outcome.proposed += 1
        if not isinstance(raw, dict):
            outcome.rejected += 1
            continue
        outcome.ignored_fields += len(set(raw) - _ALLOWED_KEYS)
        candidate = _one(raw, segments)
        if candidate is None:
            outcome.rejected += 1
        else:
            outcome.accepted.append(candidate)
    return outcome


def _one(raw: dict[str, object], segments: Sequence[SourceSegment]) -> Candidate | None:
    category_text, statement, quote = (
        raw.get("category"),
        raw.get("statement"),
        raw.get("quote"),
    )
    if not (
        isinstance(category_text, str)
        and isinstance(statement, str)
        and isinstance(quote, str)
    ):
        return None
    try:
        category = ClaimCategory(category_text)
    except ValueError:
        return None
    statement = clean_text(scrub(statement), max_length=300)
    if len(statement) < 5 or contains_secret(statement) or contains_secret(quote):
        return None
    found = find_quote(quote, segments)
    if found is None:
        return None  # provenance validation: the source never says this
    location, reference = found

    attributes: dict[str, str] = {}
    key = norm_text(statement)[:200]
    canonical = True
    if category in (ClaimCategory.SKILL, ClaimCategory.TECHNOLOGY):
        skill = raw.get("skill")
        entry = normalize_skill(skill) if isinstance(skill, str) else None
        if entry is not None:
            category, key = entry.kind, entry.skill_id
            attributes = {"skill_id": entry.skill_id, "display": entry.display}
            label = "Skill" if entry.kind is ClaimCategory.SKILL else "Technology"
            statement = f"{label}: {entry.display}"
        else:
            # Not in the trusted vocabulary: no canonical id, never promotable.
            name = clean_text(
                skill if isinstance(skill, str) else statement, max_length=80
            )
            if len(name) < 2:
                return None
            category, canonical = ClaimCategory.SKILL, False
            key = f"unmapped|{norm_text(name)}"
            statement = f"Skill (unmapped): {name}"
    return Candidate(
        category=category,
        key=key,
        statement=statement,
        attributes=attributes,
        location=location,
        reference=clean_text(scrub(reference), max_length=240),
        canonical=canonical,
    )


class RouterCandidateExtractor:
    """Candidate extraction through the Phase 13 ``ModelRouter``. The router
    applies cost, privacy and provider policy; this class adds nothing to it."""

    def __init__(self, router: ModelRouter) -> None:
        self._router = router

    def propose(
        self,
        *,
        segments: Sequence[SourceSegment],
        privacy_class: PrivacyClass,
    ) -> CandidateOutcome:
        # Identifier lines are DROPPED (not merely scrubbed) before anything is
        # sent: a student number never reaches a provider.
        text = "\n\n".join(
            scrub(
                "\n".join(
                    line
                    for line in segment.text.split("\n")
                    if not is_identifier_line(line)
                )
            )
            for segment in segments
        )[:MAX_PROMPT_CHARS]
        if not text.strip():
            return CandidateOutcome(status="blocked")
        if contains_secret(text):
            # Defence in depth: nothing that looks like a secret is ever sent.
            return CandidateOutcome(status="blocked")
        request = ModelRequest(
            request_id=uuid.uuid4().hex,
            messages=(
                Message(role=MessageRole.SYSTEM, content=_INSTRUCTIONS),
                Message(role=MessageRole.USER, content=text),
            ),
            capabilities=frozenset({Capability.TEXT}),
            privacy_class=privacy_class,
            max_output_tokens=2048,
        )
        try:
            result = self._router.execute(request)
        except Exception:
            return CandidateOutcome(status="failed")
        if result.status is not FailureCategory.SUCCESS or not result.text:
            blocked = result.status in (
                FailureCategory.POLICY_BLOCKED,
                FailureCategory.COST_BLOCKED,
            )
            return CandidateOutcome(status="blocked" if blocked else "failed")
        try:
            payload = json.loads(_strip_fences(result.text))
        except ValueError:
            return CandidateOutcome(status="invalid")
        return validate_candidates(payload, segments)


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", stripped)
    return stripped


__all__ = [
    "Candidate",
    "CandidateExtractor",
    "CandidateOutcome",
    "RouterCandidateExtractor",
    "find_quote",
    "validate_candidates",
]
