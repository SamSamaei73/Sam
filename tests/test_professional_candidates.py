"""Model-assisted candidate extraction is untrusted, and routed by Phase 13.

Every test uses fake providers behind the REAL ModelRouter: no live provider is
called, no network, and only synthetic professional text is ever "sent".
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from sam.models.errors import ProviderFailure
from sam.models.models import (
    Availability,
    FailureCategory,
    PrivacyClass,
    ProviderId,
)
from sam.models.providers.fake import FakeProvider
from sam.professional.candidates import (
    CandidateOutcome,
    RouterCandidateExtractor,
    validate_candidates,
)
from sam.professional.models import (
    ClaimCategory,
    ClaimReviewState,
    EvidenceNature,
    EvidenceStrength,
    SourceLocation,
    SourceType,
)
from sam.professional.reader import SourceSegment
from tests.models_support import make_rig as make_router_rig
from tests.professional_support import (
    CV_TEXT,
    OWNER,
    Rig,
    ingest_cv,
    make_rig,
)

QUOTE = "Led a team of 4 engineers"
ZORB = "Worked with Zorbnetics daily on internal tooling"


class Stub:
    """A CandidateExtractor that replays a fixed, untrusted model reply."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls = 0

    def propose(
        self, *, segments: Sequence[SourceSegment], privacy_class: PrivacyClass
    ) -> CandidateOutcome:
        self.calls += 1
        return validate_candidates(self.payload, segments)


def candidate(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "category": "responsibility",
        "statement": "Mentored and led a small engineering team",
        "quote": QUOTE,
    }
    base.update(overrides)
    return base


def rig_with(payload: Any) -> tuple[Rig, Stub]:
    stub = Stub(payload)
    return make_rig(candidate_extractor=stub), stub


def view(rig: Rig, needle: str):  # type: ignore[no-untyped-def]
    data = rig.service.get_profile(OWNER).data
    assert data is not None
    return next(v for v in data.claims if needle.lower() in v.statement.lower())


# ------------------------------------------------------ untrusted by design


def test_an_unsupported_skill_stays_unverified() -> None:
    rig, _ = rig_with(
        {
            "candidates": [
                {
                    "category": "skill",
                    "skill": "Zorbnetics",
                    "statement": "Skilled in Zorbnetics",
                    "quote": ZORB,
                }
            ]
        }
    )
    text = CV_TEXT + "\nTOOLS\n" + ZORB + "\n"
    result = rig.ingest(text, use_llm=True)
    assert (
        result.ok and result.data is not None and result.data.candidates_accepted == 1
    )
    zorb = view(rig, "Zorbnetics")
    assert zorb.strength is EvidenceStrength.NONE and not zorb.accepted
    assert not zorb.canonical
    assert {e.nature for e in zorb.evidence} == {EvidenceNature.MODEL_CANDIDATE}


def test_a_model_cannot_self_verify_or_smuggle_state() -> None:
    hostile = candidate(
        verification_state="direct",
        state="corroborated",
        evidence_kind="owner_confirmed",
        verified=True,
        start="1990-01",
        end="2030-01",
        permission="allow",
        sensitivity="public",
    )
    rig, _ = rig_with({"candidates": [hostile]})
    result = rig.ingest(CV_TEXT, use_llm=True)
    assert result.ok and result.data is not None
    assert result.data.candidates_accepted == 1
    claim = view(rig, "Mentored and led a small engineering team")
    assert claim.strength is EvidenceStrength.NONE and not claim.accepted
    assert claim.review is ClaimReviewState.UNREVIEWED
    assert claim.sensitivity is not PrivacyClass.PRIVATE  # the model set nothing
    assert {e.nature for e in claim.evidence} == {EvidenceNature.MODEL_CANDIDATE}
    # The smuggled fields were ignored, never applied.
    outcome = validate_candidates({"candidates": [hostile]}, _segments(CV_TEXT))
    assert outcome.ignored_fields == 8 and len(outcome.accepted) == 1
    assert outcome.accepted[0].attributes == {}


def test_a_candidate_cannot_alter_dates_or_the_timeline() -> None:
    rig = make_rig(candidate_extractor=Stub(None))
    ingest_cv(rig)
    before = rig.service.get_timeline(OWNER).data
    job_before = view(rig, "Acme")
    assert before is not None

    forged = candidate(
        category="employment",
        statement="Senior Software Engineer at Acme Analytics since 1990",
        quote="Senior Software Engineer, Acme Analytics, Mar 2019 - Jun 2022",
        start="1990-01",
    )
    rig2 = make_rig(candidate_extractor=Stub({"candidates": [forged]}))
    ingest_cv(rig2, use_llm=True)
    after = rig2.service.get_timeline(OWNER).data
    assert after is not None
    assert after.total == before.total
    assert [e.months for e in after.entries] == [e.months for e in before.entries]
    job_after = view(rig2, "Acme")
    assert (
        job_after.attributes.get("start")
        == job_before.attributes.get("start")
        == "2019-03"
    )
    # The forged claim exists only as an UNVERIFIED candidate with NO dates and is
    # therefore not on the timeline at all.
    unverified = [
        v
        for v in _all(rig2)
        if v.category is ClaimCategory.EMPLOYMENT and not v.accepted
    ]
    assert len(unverified) == 1 and "start" not in unverified[0].attributes
    assert all(e.claim_id != unverified[0].claim_id for e in after.entries)


def _all(rig: Rig):  # type: ignore[no-untyped-def]
    data = rig.service.get_profile(OWNER).data
    assert data is not None
    return list(data.claims)


def _segments(text: str) -> list[SourceSegment]:
    return [SourceSegment(0, text, SourceLocation())]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "candidates",
        {"candidates": "x"},
        {"candidates": [None, 3, "x", []]},
        {"candidates": [{"category": "skill"}]},
        {"candidates": [candidate(category="astronaut")]},
        {"candidates": [candidate(statement="x")]},
        {"candidates": [candidate(quote="short")]},
    ],
)
def test_malformed_candidates_are_rejected_not_repaired(payload: Any) -> None:
    outcome = validate_candidates(payload, _segments(CV_TEXT))
    assert outcome.accepted == []


def test_a_candidate_must_quote_the_source_provenance_validation() -> None:
    made_up = candidate(quote="Won the Nobel Prize in Physics in 2019")
    outcome = validate_candidates({"candidates": [made_up]}, _segments(CV_TEXT))
    assert outcome.accepted == [] and outcome.rejected == 1
    real = validate_candidates({"candidates": [candidate()]}, _segments(CV_TEXT))
    (accepted,) = real.accepted
    start, end = accepted.location.character_start, accepted.location.character_end
    assert start is not None and end is not None
    assert CV_TEXT[start:end].lower() == QUOTE.lower()


def test_a_candidate_containing_a_secret_is_rejected() -> None:
    text = "SKILLS\nPython\nkey sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
    bad = candidate(
        statement="Uses a key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        quote="key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )
    assert validate_candidates({"candidates": [bad]}, _segments(text)).accepted == []


def test_a_candidate_never_upgrades_an_existing_claim() -> None:
    rig, _ = rig_with(
        {
            "candidates": [
                {
                    "category": "skill",
                    "skill": "Python",
                    "statement": "Knows Python",
                    "quote": "Python, TypeScript, React",
                }
            ]
        }
    )
    ingest_cv(rig, use_llm=True)
    python = view(rig, "Technology: Python")
    # SINGLE_SOURCE from the explicit CV line; the candidate adds nothing.
    assert python.strength is EvidenceStrength.SINGLE_SOURCE
    assert EvidenceNature.MODEL_CANDIDATE in {e.nature for e in python.evidence}


# ------------------------------------------------------------ owner workflow


def test_owner_can_confirm_a_candidate_through_the_trusted_workflow() -> None:
    rig, _ = rig_with({"candidates": [candidate()]})
    ingest_cv(rig, use_llm=True)
    claim = view(rig, "Mentored and led a small engineering team")
    assert not claim.accepted and claim.review is ClaimReviewState.UNREVIEWED
    confirmed = rig.service.confirm_claim(OWNER, claim.claim_id)
    assert confirmed.ok and confirmed.data is not None
    data = confirmed.data
    # Accepted on the owner's word, NOT on documents: strength stays NONE.
    assert data.accepted and data.review is ClaimReviewState.OWNER_CONFIRMED
    assert data.strength is EvidenceStrength.NONE and data.owner_attested_only
    (attestation,) = [
        e for e in data.evidence if e.nature is EvidenceNature.OWNER_ATTESTATION
    ]
    (basis,) = [e for e in data.evidence if e.nature is EvidenceNature.MODEL_CANDIDATE]
    assert attestation.basis_evidence_id == basis.evidence_id
    # Confirming again is idempotent: one attestation, nothing else changes.
    again = rig.service.confirm_claim(OWNER, claim.claim_id)
    assert again.ok and again.data is not None
    assert again.data.evidence == data.evidence


def test_owner_can_reject_a_candidate_and_it_is_not_resurrected() -> None:
    rig, _ = rig_with({"candidates": [candidate()]})
    ingest_cv(rig, use_llm=True)
    claim = view(rig, "Mentored and led a small engineering team")
    assert rig.service.reject_claim(OWNER, claim.claim_id).ok
    assert all("Mentored and led" not in v.statement for v in _all(rig))
    # A refreshed source that proposes the same candidate again does not bring it back.
    result = rig.ingest(CV_TEXT + "\nEXTRA\nnothing\n", use_llm=True)
    assert result.ok and result.data is not None
    assert result.data.claims_skipped_rejected >= 1
    assert all("Mentored and led" not in v.statement for v in _all(rig))
    assert not rig.service.reject_claim(OWNER, claim.claim_id).ok  # already gone


def test_an_unknown_skill_cannot_be_promoted_by_the_model_or_the_owner() -> None:
    rig, _ = rig_with(
        {
            "candidates": [
                {
                    "category": "skill",
                    "skill": "Zorbnetics",
                    "statement": "Skilled in Zorbnetics",
                    "quote": ZORB,
                }
            ]
        }
    )
    assert rig.ingest("TOOLS\n" + ZORB + "\n", use_llm=True).ok
    zorb = view(rig, "Zorbnetics")
    assert zorb.claim_id
    promoted = rig.service.confirm_claim(OWNER, zorb.claim_id)
    assert not promoted.ok and promoted.reason == "unknown_skill"
    after = view(rig, "Zorbnetics")
    assert not after.accepted and after.review is ClaimReviewState.UNREVIEWED


def test_confirming_a_documented_claim_never_erases_its_provenance() -> None:
    rig, _ = rig_with(None)
    ingest_cv(rig)
    python = view(rig, "Technology: Python")
    documentary = {
        e.evidence_id
        for e in python.evidence
        if e.nature is EvidenceNature.EXPLICIT_SOURCE
    }
    assert documentary
    result = rig.service.confirm_claim(OWNER, python.claim_id)
    assert result.ok and result.data is not None
    after = result.data
    # Documentary evidence and strength are untouched by the owner's review.
    assert after.strength is EvidenceStrength.SINGLE_SOURCE
    assert {
        e.evidence_id
        for e in after.evidence
        if e.nature is EvidenceNature.EXPLICIT_SOURCE
    } == documentary
    assert after.review is ClaimReviewState.OWNER_CONFIRMED
    assert rig.service.confirm_claim(OWNER, "c_missing").reason == "claim_not_found"


# ----------------------------------------------- routing through Phase 13


def _reply(*candidates: dict[str, Any]) -> str:
    return json.dumps({"candidates": list(candidates)})


def _extractor(claude_text: str = "{}", **kwargs: Any):  # type: ignore[no-untyped-def]
    router_rig = make_router_rig(
        claude=FakeProvider(ProviderId.CLAUDE_SUBSCRIPTION, text=claude_text, **kwargs)
    )
    return router_rig, RouterCandidateExtractor(router_rig.router)


def test_a_private_source_never_routes_to_gemini_by_default() -> None:
    router_rig, extractor = _extractor(_reply(candidate()))
    rig = make_rig(candidate_extractor=extractor)
    result = rig.ingest(CV_TEXT, privacy=PrivacyClass.PRIVATE, use_llm=True)
    assert result.ok and result.data is not None
    assert result.data.candidate_extraction == "completed"
    assert router_rig.gemini.call_count == 0  # never Gemini Free
    assert router_rig.claude.call_count == 1  # the owner's own subscription
    assert router_rig.claude.calls[0].privacy_class is PrivacyClass.PRIVATE
    assert router_rig.paid_calls() == 0


def test_private_source_with_claude_unavailable_is_blocked_not_sent_to_gemini() -> None:
    router_rig, extractor = _extractor(
        failure=ProviderFailure(FailureCategory.UNAVAILABLE, "down")
    )
    rig = make_rig(candidate_extractor=extractor)
    ingest_cv(rig)
    before = (rig.repo.list_claims(), rig.repo.list_all_evidence())
    result = rig.ingest(
        CV_TEXT + "\nEXTRA\nnothing\n", privacy=PrivacyClass.PRIVATE, use_llm=True
    )
    assert result.ok  # deterministic extraction still succeeds
    assert result.data is not None and result.data.candidate_extraction in (
        "blocked",
        "failed",
    )
    assert router_rig.gemini.call_count == 0
    assert router_rig.paid_calls() == 0
    assert before[0] == rig.repo.list_claims()  # existing claims are undamaged


def test_a_personal_source_is_not_sent_to_gemini_unless_the_owner_allows_it() -> None:
    router_rig, extractor = _extractor(_reply(candidate()))
    rig = make_rig(candidate_extractor=extractor)
    assert rig.ingest(CV_TEXT, privacy=PrivacyClass.PERSONAL, use_llm=True).ok
    assert router_rig.gemini.call_count == 0  # personal_to_free_tier is OFF by default


def test_a_public_source_follows_normal_routing_including_gemini_fallback() -> None:
    router_rig = make_router_rig(
        claude=FakeProvider(
            ProviderId.CLAUDE_SUBSCRIPTION,
            failure=ProviderFailure(FailureCategory.UNAVAILABLE, "down"),
        ),
        gemini=FakeProvider(ProviderId.GEMINI_FREE, text=_reply(candidate())),
    )
    rig = make_rig(candidate_extractor=RouterCandidateExtractor(router_rig.router))
    result = rig.ingest(CV_TEXT, privacy=PrivacyClass.PUBLIC, use_llm=True)
    assert result.ok and result.data is not None
    assert result.data.candidate_extraction == "completed"
    assert router_rig.gemini.call_count == 1  # a public paper may use the free tier
    assert router_rig.paid_calls() == 0


def test_secret_material_reaches_zero_providers() -> None:
    router_rig, extractor = _extractor(_reply(candidate()))
    rig = make_rig(candidate_extractor=extractor)
    secret_cv = CV_TEXT + "\npassword: hunter2hunter2hunter2\n"
    result = rig.ingest(secret_cv, use_llm=True)
    assert not result.ok and result.reason == "secret_detected"
    assert router_rig.claude.call_count == router_rig.gemini.call_count == 0
    assert router_rig.paid_calls() == 0
    # Defence in depth: even handed straight to the extractor, nothing is sent.
    direct = extractor.propose(
        segments=_segments("SKILLS\nPython\npassword: hunter2hunter2hunter2\n"),
        privacy_class=PrivacyClass.PUBLIC,
    )
    assert direct.status == "blocked"
    assert router_rig.claude.call_count == router_rig.gemini.call_count == 0


def test_identifiers_are_never_in_a_provider_prompt() -> None:
    router_rig, extractor = _extractor(_reply(candidate()))
    rig = make_rig(candidate_extractor=extractor)
    text = CV_TEXT + "\nAccount number 99887766 for billing\n"
    assert rig.ingest(text, use_llm=True).ok
    (sent,) = router_rig.claude.calls
    prompt = " ".join(m.content for m in sent.messages)
    for private in ("A1234567", "99887766", "jordan@example.test", "7700 900123"):
        assert private not in prompt
    assert "Jordan Example" in prompt  # the professional content itself is what is sent


def test_a_provider_refusal_never_damages_existing_evidence() -> None:
    router_rig, extractor = _extractor(failure=FakeProvider.refusal())
    rig = make_rig(candidate_extractor=extractor)
    ingest_cv(rig)
    claims = rig.repo.list_claims()
    evidence = rig.repo.list_all_evidence()
    result = rig.ingest(CV_TEXT + "\nEXTRA\nnothing\n", use_llm=True)
    assert result.ok and result.data is not None
    assert result.data.candidate_extraction in ("blocked", "failed")
    assert result.data.candidates_accepted == 0
    assert {e.evidence_id for e in evidence} <= {
        e.evidence_id for e in rig.repo.list_all_evidence()
    }
    assert {c.claim_id for c in claims} <= {c.claim_id for c in rig.repo.list_claims()}
    assert router_rig.gemini.call_count == 0  # a refusal is not worked around


def test_extraction_is_off_unless_the_owner_asks_for_it() -> None:
    router_rig, extractor = _extractor(_reply(candidate()))
    rig = make_rig(candidate_extractor=extractor)
    result = rig.ingest(CV_TEXT)  # use_llm defaults to False
    assert result.ok and result.data is not None
    assert result.data.candidate_extraction == "off"
    assert router_rig.claude.call_count == router_rig.gemini.call_count == 0


def test_garbled_model_output_is_invalid_and_harmless() -> None:
    _router_rig, extractor = _extractor("this is not json at all")
    rig = make_rig(candidate_extractor=extractor)
    result = rig.ingest(CV_TEXT, use_llm=True)
    assert result.ok and result.data is not None
    assert result.data.candidate_extraction == "invalid"
    assert result.data.candidates_accepted == 0


def test_the_candidate_module_only_reaches_a_model_through_the_router() -> None:
    import inspect

    from sam.professional import candidates

    source = inspect.getsource(candidates)
    for direct in ("httpx", "requests", "anthropic", "subprocess", "urllib", "socket"):
        assert direct not in source
    assert "ModelRouter" in source


def test_source_types_and_states_are_closed_sets() -> None:
    assert {s.value for s in SourceType} == {
        "master_cv",
        "publication",
        "transcript",
        "project_documentation",
        "github",
        "linkedin_export",
        "owner_document",
    }
    assert Availability.AVAILABLE.value == "available"
