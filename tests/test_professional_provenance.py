"""Evidence is not owner review: the two are independent concepts.

``EvidenceNature`` says what a piece of evidence IS, ``EvidenceStrength`` how much
DOCUMENTARY support a claim has, ``ClaimReviewState`` what the owner decided.
The owner's confirmation creates an ``OWNER_ATTESTATION`` that names the evidence
the owner reviewed; it never erases, replaces or counts as documentary evidence,
and no accepted claim ever rests on zero trusted evidence.

Synthetic documents and a stub model reply only: no provider, no network.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from sam.models.models import PrivacyClass
from sam.professional.candidates import CandidateOutcome, validate_candidates
from sam.professional.evidence import is_accepted
from sam.professional.models import (
    TRUSTED_NATURES,
    ClaimReview,
    ClaimReviewState,
    EvidenceNature,
    EvidenceRef,
    EvidenceStrength,
    SourceLocation,
    SourceType,
)
from sam.professional.profile import ClaimView
from sam.professional.reader import SourceSegment
from tests.professional_support import (
    CV_TEXT,
    NOW,
    OWNER,
    Rig,
    ingest_cv,
    make_rig,
)

QUOTE = "Led a team of 4 engineers"
STATEMENT = "Mentored and led a small engineering team"
NOTES = "Career notes\nLed a team of 4 engineers at a previous employer.\n"


class Stub:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def propose(
        self, *, segments: Sequence[SourceSegment], privacy_class: PrivacyClass
    ) -> CandidateOutcome:
        return validate_candidates(self.payload, segments)


def _rig() -> Rig:
    reply = {
        "candidates": [
            {"category": "responsibility", "statement": STATEMENT, "quote": QUOTE}
        ]
    }
    return make_rig(candidate_extractor=Stub(reply))


def _views(rig: Rig) -> list[ClaimView]:
    data = rig.service.get_profile(OWNER).data
    assert data is not None
    return list(data.claims)


def _view(rig: Rig, needle: str) -> ClaimView:
    return next(v for v in _views(rig) if needle.lower() in v.statement.lower())


def _remove(rig: Rig, source_id: str) -> None:
    first = rig.service.remove_source(OWNER, source_id)
    assert first.confirmation_id
    rig.approve(first.confirmation_id)
    assert rig.service.remove_source(OWNER, source_id, first.confirmation_id).ok


def _assert_no_accepted_claim_without_trusted_evidence(rig: Rig) -> None:
    for view in _views(rig):
        if view.accepted:
            assert any(e.nature in TRUSTED_NATURES for e in view.evidence), view
            if view.strength is EvidenceStrength.NONE:
                assert view.review is ClaimReviewState.OWNER_CONFIRMED
                assert any(
                    e.nature is EvidenceNature.OWNER_ATTESTATION and e.basis_evidence_id
                    for e in view.evidence
                )


# ------------------------------------------------------------ the concepts


def test_nature_strength_and_review_are_three_separate_vocabularies() -> None:
    natures = {n.value for n in EvidenceNature}
    strengths = {s.value for s in EvidenceStrength}
    reviews = {r.value for r in ClaimReviewState}
    assert natures == {
        "explicit_source",
        "inferred_relationship",
        "owner_attestation",
        "model_candidate",
    }
    assert strengths == {"none", "single_source", "corroborated"}
    assert reviews == {"unreviewed", "owner_confirmed", "owner_rejected"}
    # Owner confirmation is a REVIEW state; it is not a strength.
    assert "owner_confirmed" not in strengths and "direct" not in strengths


def test_documentary_evidence_exists_without_any_owner_review() -> None:
    rig = make_rig()
    ingest_cv(rig)
    python = _view(rig, "Technology: Python")
    assert python.review is ClaimReviewState.UNREVIEWED
    assert python.strength is EvidenceStrength.SINGLE_SOURCE and python.accepted
    assert {e.nature for e in python.evidence} == {EvidenceNature.EXPLICIT_SOURCE}
    assert rig.repo.list_reviews() == ()


def test_a_model_candidate_stays_untrusted_until_the_owner_reviews_it() -> None:
    rig = _rig()
    ingest_cv(rig, use_llm=True)
    claim = _view(rig, STATEMENT)
    assert claim.candidate and not claim.accepted
    assert claim.strength is EvidenceStrength.NONE
    assert claim.review is ClaimReviewState.UNREVIEWED
    # Not an accepted fact anywhere: not in requirement matching, not verified.
    policy = rig.service.assess_claim(OWNER, "I led a team of engineers").data
    assert policy is not None and claim.claim_id not in {
        c for f in policy.findings for c in f.claim_ids
    }


# ----------------------------------------------------------- attestation


def test_owner_confirmation_creates_attestation_with_provenance() -> None:
    rig = _rig()
    ingest_cv(rig, use_llm=True)
    before = _view(rig, STATEMENT)
    (candidate,) = before.evidence
    confirmed = rig.service.confirm_claim(OWNER, before.claim_id)
    assert confirmed.ok and confirmed.data is not None
    after = confirmed.data
    attestations = [
        e for e in after.evidence if e.nature is EvidenceNature.OWNER_ATTESTATION
    ]
    assert len(attestations) == 1
    (attestation,) = attestations
    # It names what the owner reviewed, and keeps that evidence's provenance.
    assert attestation.basis_evidence_id == candidate.evidence_id
    assert attestation.source_id == candidate.source_id
    assert attestation.location == candidate.location
    # The candidate itself is kept, unchanged: still a model candidate.
    assert candidate in after.evidence
    assert after.accepted and after.strength is EvidenceStrength.NONE
    review = rig.repo.get_review(after.claim_id)
    assert review is not None and review.state is ClaimReviewState.OWNER_CONFIRMED


def test_owner_attestation_never_counts_as_documentary_support() -> None:
    rig = _rig()
    ingest_cv(rig, use_llm=True)
    claim = _view(rig, STATEMENT)
    assert rig.service.confirm_claim(OWNER, claim.claim_id).ok
    # A second source proposes the same statement: still only candidates plus
    # one attestation, so documentary strength is NONE, never corroborated.
    assert rig.ingest(
        NOTES, name="notes.txt", source_type=SourceType.OWNER_DOCUMENT, use_llm=True
    ).ok
    after = _view(rig, STATEMENT)
    assert after.strength is EvidenceStrength.NONE
    assert after.source_family_count == 0
    assert after.accepted and after.owner_attested_only


def test_a_review_with_zero_evidence_accepts_nothing() -> None:
    for review in ClaimReviewState:
        assert not is_accepted([], EvidenceStrength.NONE, review)


def test_a_candidate_plus_confirmed_review_without_attestation_is_not_accepted() -> (
    None
):
    location = SourceLocation(character_start=0, character_end=5)
    candidate = EvidenceRef(
        evidence_id="e_candidate",
        claim_id="c",
        source_id="s",
        source_type=SourceType.MASTER_CV,
        source_location=location,
        nature=EvidenceNature.MODEL_CANDIDATE,
        created_at=NOW,
    )
    assert not is_accepted(
        [candidate], EvidenceStrength.NONE, ClaimReviewState.OWNER_CONFIRMED
    )


def test_a_rejected_claim_is_never_accepted_even_with_documents() -> None:
    assert not is_accepted(
        [], EvidenceStrength.CORROBORATED, ClaimReviewState.OWNER_REJECTED
    )


def test_the_repository_refuses_a_confirmation_without_attestation() -> None:
    rig = _rig()
    ingest_cv(rig, use_llm=True)
    claim = _view(rig, STATEMENT)
    (candidate,) = rig.repo.list_evidence(claim.claim_id)
    review = ClaimReview(
        claim_id=claim.claim_id,
        state=ClaimReviewState.OWNER_CONFIRMED,
        reviewed_at=NOW,
    )
    # Passing the CANDIDATE as if it were the attestation is refused ...
    with pytest.raises(ValueError):
        rig.repo.confirm(review, candidate)
    # ... and nothing was recorded: the claim is still unreviewed.
    assert rig.repo.get_review(claim.claim_id) is None
    assert not _view(rig, STATEMENT).accepted


@pytest.mark.parametrize(
    "fields",
    [
        {"nature": EvidenceNature.OWNER_ATTESTATION},  # no basis
        {"nature": EvidenceNature.EXPLICIT_SOURCE, "basis_evidence_id": "e_x"},
        {
            "nature": EvidenceNature.OWNER_ATTESTATION,
            "basis_evidence_id": "e_x",
            "observed": {"start": "2019-03"},
        },
        {"nature": EvidenceNature.MODEL_CANDIDATE, "observed": {"end": "2030-01"}},
        {"nature": EvidenceNature.EXPLICIT_SOURCE, "sensitivity": PrivacyClass.SECRET},
    ],
)
def test_evidence_shape_is_validated(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EvidenceRef(
            evidence_id="e",
            claim_id="c",
            source_id="s",
            source_type=SourceType.MASTER_CV,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            **fields,
        )


# ------------------------------------------------------- lifecycle safety


def test_removing_the_attested_source_lapses_the_confirmation() -> None:
    rig = _rig()
    ingest_cv(rig, use_llm=True)
    assert rig.ingest(
        NOTES, name="notes.txt", source_type=SourceType.OWNER_DOCUMENT, use_llm=True
    ).ok
    claim = _view(rig, STATEMENT)
    confirmed = rig.service.confirm_claim(OWNER, claim.claim_id)
    assert confirmed.ok and confirmed.data is not None
    (attestation,) = [
        e
        for e in confirmed.data.evidence
        if e.nature is EvidenceNature.OWNER_ATTESTATION
    ]
    _remove(rig, attestation.source_id)
    # The claim survives on the other source's candidate, but the owner's
    # confirmation rested on the removed evidence: it lapses, nothing is
    # accepted on zero trusted evidence.
    after = _view(rig, STATEMENT)
    assert not after.accepted and after.review is ClaimReviewState.UNREVIEWED
    assert all(e.nature is EvidenceNature.MODEL_CANDIDATE for e in after.evidence)
    _assert_no_accepted_claim_without_trusted_evidence(rig)


def test_no_accepted_claim_ever_rests_on_zero_trusted_evidence() -> None:
    rig = _rig()
    ingest_cv(rig, use_llm=True)
    _assert_no_accepted_claim_without_trusted_evidence(rig)
    claim = _view(rig, STATEMENT)
    assert rig.service.confirm_claim(OWNER, claim.claim_id).ok
    _assert_no_accepted_claim_without_trusted_evidence(rig)
    # Refresh WITHOUT the confirmed line: the attestation cannot be carried.
    changed = CV_TEXT.replace("- Led a team of 4 engineers\n", "")
    assert rig.ingest(changed, use_llm=True).ok
    _assert_no_accepted_claim_without_trusted_evidence(rig)
    assert all(STATEMENT not in v.statement for v in _views(rig) if v.accepted)


def test_rejection_is_a_review_state_that_keeps_the_claim_out() -> None:
    rig = _rig()
    ingest_cv(rig, use_llm=True)
    claim = _view(rig, STATEMENT)
    assert rig.service.reject_claim(OWNER, claim.claim_id).ok
    review = rig.repo.get_review(claim.claim_id)
    assert review is not None and review.state is ClaimReviewState.OWNER_REJECTED
    assert rig.repo.is_rejected(claim.claim_id)
    assert all(v.claim_id != claim.claim_id for v in _views(rig))


def test_a_stranger_cannot_confirm_or_reject() -> None:
    from tests.professional_support import STRANGER

    rig = _rig()
    ingest_cv(rig, use_llm=True)
    claim = _view(rig, STATEMENT)
    for op in (rig.service.confirm_claim, rig.service.reject_claim):
        result = op(STRANGER, claim.claim_id)
        assert not result.ok and result.permission == "deny"
    assert rig.repo.list_reviews() == ()
