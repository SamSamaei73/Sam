"""Privacy of professional evidence.

* raw evidence excerpts never enter an audit event;
* the model-facing tool never returns a raw excerpt or document free text;
* PRIVATE evidence never goes to Gemini Free by default (refresh included);
* SECRET material is never stored;
* a principal without the owner's grants (Guest) sees nothing;
* removed or rejected evidence never resurfaces through a later view.

Synthetic documents, fake providers and stub replies only: no network.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import ValidationError

from sam.models.models import PrivacyClass
from sam.permissions.models import Principal, PrincipalKind
from sam.professional.candidates import CandidateOutcome, validate_candidates
from sam.professional.models import (
    ClaimCategory,
    EvidenceNature,
    ProfessionalClaim,
    SourceType,
)
from sam.professional.reader import SourceSegment
from sam.professional.tool import (
    RAW_TEXT_ATTRIBUTES,
    READ_OPERATIONS,
    ProfessionalReadTool,
)
from tests.professional_support import (
    CV_TEXT,
    NOW,
    OWNER,
    PAPER_TEXT,
    Rig,
    ingest_cv,
    make_rig,
)

# Sentinels that appear ONLY in raw source lines, never in a claim statement.
LINE_SENTINEL = "Quokkaline"
_SKILLS_LINE = "Python, TypeScript, React, FastAPI, PyTorch, Docker, AWS, Kubernetes"
CV_WITH_SENTINEL = CV_TEXT.replace(_SKILLS_LINE, f"{_SKILLS_LINE} ({LINE_SENTINEL})")
ABSTRACT_SENTINEL = "Wombatfold"
PAPER_WITH_SENTINEL = PAPER_TEXT.replace(
    "for health misinformation.", f"for health misinformation ({ABSTRACT_SENTINEL})."
).replace(
    "Riley Placeholder implemented the model.",
    "Jordan Example implemented the Numbatcore model.",
)
GUEST = Principal(kind=PrincipalKind.USER, id="guest")


class Recorder:
    """A candidate extractor that records the privacy class it is given."""

    def __init__(self) -> None:
        self.classes: list[PrivacyClass] = []

    def propose(
        self, *, segments: Sequence[SourceSegment], privacy_class: PrivacyClass
    ) -> CandidateOutcome:
        self.classes.append(privacy_class)
        return validate_candidates({"candidates": []}, segments)


def _all_tool_output(rig: Rig, principal: Principal = OWNER) -> str:
    tool = ProfessionalReadTool(rig.service, principal)
    arguments: dict[str, dict[str, Any]] = {
        "search": {"query": "Python graph convolutional health misinformation"},
        "evidence_for": {"requirement": "Python and FastAPI with publications"},
        "experience_for": {"skill": "Python"},
        "assess_claim": {"statement": "I published a paper on Python"},
    }
    replies = [
        tool.execute({"operation": op, **arguments.get(op, {})})
        for op in READ_OPERATIONS
    ]
    return json.dumps(replies)


def _references(rig: Rig) -> list[str]:
    """Raw excerpts that are NOT also a claim's own statement or structured
    value (a paper's title line is its title; that is not an excerpt leak)."""

    structured = {c.canonical_statement for c in rig.repo.list_claims()} | {
        value for c in rig.repo.list_claims() for value in c.attributes.values()
    }
    return [
        e.evidence_reference
        for e in rig.repo.list_all_evidence()
        if len(e.evidence_reference) >= 20
        and not any(e.evidence_reference in s for s in structured)
    ]


def _attribute_names(value: Any) -> set[str]:
    """Every key of every ``attributes`` mapping anywhere in a tool reply."""

    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "attributes" and isinstance(item, dict):
                found |= set(item)
            found |= _attribute_names(item)
    elif isinstance(value, list):
        for item in value:
            found |= _attribute_names(item)
    return found


def _full_rig() -> Rig:
    rig = make_rig()
    assert rig.ingest(CV_WITH_SENTINEL).ok
    assert rig.ingest(
        PAPER_WITH_SENTINEL,
        name="paper.txt",
        source_type=SourceType.PUBLICATION,
        privacy=PrivacyClass.PUBLIC,
    ).ok
    return rig


# ------------------------------------------------------------------- audit


def test_raw_evidence_excerpts_never_enter_the_audit() -> None:
    rig = _full_rig()
    # Every write path: refresh, confirm, reject, removal.
    assert rig.ingest(CV_WITH_SENTINEL.replace("React, ", "")).ok
    views = rig.service.get_profile(OWNER).data
    assert views is not None
    rig.service.confirm_claim(OWNER, views.claims[0].claim_id)
    rig.service.reject_claim(OWNER, views.claims[-1].claim_id)
    source_id = rig.repo.list_sources()[0].source_id
    first = rig.service.remove_source(OWNER, source_id)
    assert first.confirmation_id
    rig.approve(first.confirmation_id)
    references = _references(rig) + [LINE_SENTINEL, ABSTRACT_SENTINEL, "Numbatcore"]
    dumped = " ".join(repr(e) for e in rig.audit.events())
    assert rig.service.remove_source(OWNER, source_id, first.confirmation_id).ok
    dumped += " ".join(repr(e) for e in rig.audit.events())
    for text in references:
        assert text not in dumped
    for label in ("cv.txt", "paper.txt"):
        assert label not in dumped


# --------------------------------------------------------- model-facing tool


def test_the_tool_never_returns_raw_evidence_excerpts() -> None:
    rig = _full_rig()
    everything = _all_tool_output(rig)
    assert LINE_SENTINEL not in everything
    for reference in _references(rig):
        assert reference not in everything
    assert '"reference"' not in everything and '"evidence_reference"' not in everything


def test_the_tool_never_returns_document_free_text() -> None:
    rig = _full_rig()
    everything = _all_tool_output(rig)
    # The abstract and the contribution sentence are raw paper text.
    assert ABSTRACT_SENTINEL not in everything and "Numbatcore" not in everything
    names = _attribute_names(json.loads(everything))
    assert names and not names & RAW_TEXT_ATTRIBUTES
    assert '"abstract"' not in everything and '"owner_contribution"' not in everything
    # ... while the owner, through the desktop profile, still sees them.
    pubs = rig.service.get_publications(OWNER).data
    assert pubs and pubs[0].owner_contribution is not None
    assert pubs[0].abstract and ABSTRACT_SENTINEL in pubs[0].abstract


def test_the_tool_withholds_private_evidence_and_its_provenance() -> None:
    rig = make_rig()
    ingest_cv(rig, privacy=PrivacyClass.PRIVATE)
    everything = _all_tool_output(rig)
    for private in ("Acme", "Globex", "Example University", "cv.txt"):
        assert private not in everything


# ------------------------------------------------------ Gemini / providers


def test_a_private_source_refresh_keeps_its_private_class_for_the_router() -> None:
    recorder = Recorder()
    rig = make_rig(candidate_extractor=recorder)
    assert rig.ingest(CV_TEXT, privacy=PrivacyClass.PRIVATE, use_llm=True).ok
    source_id = rig.repo.list_sources()[0].source_id
    refreshed = rig.service.refresh_source(
        OWNER,
        source_id,
        name="cv.txt",
        resource_type="txt",
        content=CV_TEXT.replace("Kubernetes", "Terraform").encode(),
        use_llm=True,
    )
    assert refreshed.ok
    # The refresh takes the STORED class: a caller cannot downgrade it, so the
    # Phase 13 router still sees PRIVATE (never Gemini Free by default).
    assert recorder.classes == [PrivacyClass.PRIVATE, PrivacyClass.PRIVATE]


def test_private_candidate_routing_uses_the_phase13_router_policy() -> None:
    # The end-to-end routing proof lives with the candidate tests (real
    # ModelRouter, fake providers); this guards that it is still there.
    from tests import test_professional_candidates as candidates

    assert hasattr(
        candidates, "test_a_private_source_never_routes_to_gemini_by_default"
    )
    assert hasattr(
        candidates,
        "test_private_source_with_claude_unavailable_is_blocked_not_sent_to_gemini",
    )


# ------------------------------------------------------------------ SECRET


def test_secret_class_and_secret_content_are_never_stored() -> None:
    rig = make_rig()
    refused = rig.ingest(CV_TEXT, privacy=PrivacyClass.SECRET)
    assert not refused.ok and refused.reason == "secret_not_ingestible"
    secret = CV_TEXT + "\napi_key = sk-live-A1b2C3d4E5f6G7h8I9j0K1l2M3n4\n"
    leaked = rig.ingest(secret, name="cv2.txt")
    assert not leaked.ok
    assert rig.repo.list_sources() == () and rig.repo.list_all_evidence() == ()


def test_the_models_refuse_secret_sensitivity() -> None:
    with pytest.raises(ValidationError):
        ProfessionalClaim(
            claim_id="c",
            category=ClaimCategory.SKILL,
            canonical_statement="Skill: x",
            key="x",
            sensitivity=PrivacyClass.SECRET,
            created_at=NOW,
            updated_at=NOW,
        )


# ------------------------------------------------------------------- Guest


def test_a_principal_without_the_owners_grants_sees_nothing() -> None:
    rig = _full_rig()
    service = rig.service
    for result in (
        service.get_profile(GUEST),
        service.search(GUEST, "Python"),
        service.evidence_for(GUEST, "Python"),
        service.get_sources(GUEST),
    ):
        assert not result.ok and result.data is None and result.permission == "deny"
    replies = json.loads(_all_tool_output(rig, GUEST))
    assert replies and all(r == {"ok": False, "reason": r["reason"]} for r in replies)
    assert "Acme" not in json.dumps(replies)


# ------------------------------------------------------- no stale views


def test_removed_evidence_never_resurfaces_in_a_later_view() -> None:
    rig = _full_rig()
    before = rig.service.get_profile(OWNER).data
    assert before is not None
    assert LINE_SENTINEL in json.dumps(
        [e.reference for v in before.claims for e in v.evidence]
    )
    cv = rig.source_id("cv.txt", SourceType.MASTER_CV)
    first = rig.service.remove_source(OWNER, cv)
    assert first.confirmation_id
    rig.approve(first.confirmation_id)
    assert rig.service.remove_source(OWNER, cv, first.confirmation_id).ok
    after = rig.service.get_profile(OWNER).data
    assert after is not None
    dumped = repr(after)
    assert LINE_SENTINEL not in dumped and "cv.txt" not in dumped
    assert all(e.source_id != cv for v in after.claims for e in v.evidence)
    assert LINE_SENTINEL not in _all_tool_output(rig)


def test_a_rejected_claim_never_resurfaces_in_a_later_view() -> None:
    rig = _full_rig()
    python = next(
        v
        for v in (rig.service.get_skills(OWNER).data or ())
        if v.attributes.get("skill_id") == "python"
    )
    assert rig.service.reject_claim(OWNER, python.claim_id).ok
    assert rig.ingest(CV_WITH_SENTINEL.replace("React", "React.js")).ok  # refresh
    data = rig.service.get_profile(OWNER).data
    assert data is not None
    assert all(v.claim_id != python.claim_id for v in data.claims)
    assert '"skill_id": "python"' not in _all_tool_output(rig)


def test_a_superseded_version_never_resurfaces_after_refresh() -> None:
    rig = _full_rig()
    assert rig.ingest(CV_WITH_SENTINEL.replace(f" ({LINE_SENTINEL})", "")).ok
    data = rig.service.get_profile(OWNER).data
    assert data is not None
    assert LINE_SENTINEL not in repr(data)
    assert all(
        LINE_SENTINEL not in e.evidence_reference for e in rig.repo.list_all_evidence()
    )
    assert all(
        e.nature is not EvidenceNature.MODEL_CANDIDATE
        for e in rig.repo.list_all_evidence()
    )
