"""Refreshing a source is one atomic transaction on a LOGICAL source.

The logical ``source_id`` never changes; the checksum and version do. A refresh
validates and extracts the whole new version first; if anything fails the
previous version and all of its evidence are left exactly as they were. If it
succeeds, EVERY old ``EvidenceRef`` of that source is replaced (no ghost
evidence), no other source's evidence is touched, and everything derived
(strength, conflicts, skills, publications, education, gaps) is recomputed.

Synthetic documents only; no provider, no network.
"""

from __future__ import annotations

from typing import Any

import pytest

from sam.professional.gaps import GapKind
from sam.professional.models import (
    ClaimCategory,
    ClaimReviewState,
    EvidenceNature,
    EvidenceStrength,
    SourceType,
)
from sam.professional.profile import ClaimView
from tests.professional_support import (
    CV_TEXT,
    LINKEDIN_AGREEING_CSV,
    LINKEDIN_CSV,
    OWNER,
    PAPER_TEXT,
    STRANGER,
    Rig,
    ingest_cv,
    make_rig,
)

CV_ID_NAME = "cv.txt"


def _views(rig: Rig) -> list[ClaimView]:
    data = rig.service.get_profile(OWNER).data
    assert data is not None
    return list(data.claims)


def _find(rig: Rig, needle: str) -> ClaimView | None:
    return next((v for v in _views(rig) if needle in v.statement), None)


def _state(rig: Rig) -> tuple[Any, ...]:
    repo = rig.repo
    return (
        repo.list_sources(),
        repo.list_claims(),
        repo.list_all_evidence(),
        repo.list_reviews(),
        repo.list_resolutions(),
    )


def _cv_id(rig: Rig) -> str:
    return rig.source_id(CV_ID_NAME, SourceType.MASTER_CV)


def _linkedin(rig: Rig, text: str = LINKEDIN_CSV) -> None:
    result = rig.ingest(
        text,
        name="Positions.csv",
        source_type=SourceType.LINKEDIN_EXPORT,
        resource_type="csv",
    )
    assert result.ok, result.reason


def _refresh(rig: Rig, text: str, **kwargs: Any):  # type: ignore[no-untyped-def]
    kwargs.setdefault("name", CV_ID_NAME)
    kwargs.setdefault("resource_type", "txt")
    return rig.service.refresh_source(
        OWNER, _cv_id(rig), content=text.encode("utf-8"), **kwargs
    )


# ------------------------------------------------------ identity vs version


def test_a_refresh_changes_checksum_and_version_not_the_logical_source() -> None:
    rig = make_rig()
    ingest_cv(rig)
    before = rig.repo.get_source(_cv_id(rig))
    assert before is not None and before.version == 1
    result = _refresh(rig, CV_TEXT.replace("Kubernetes", "Terraform"))
    assert result.ok and result.data is not None
    assert result.data.status == "refreshed" and result.data.source_id == _cv_id(rig)
    after = rig.repo.get_source(_cv_id(rig))
    assert after is not None
    assert after.source_id == before.source_id
    assert after.checksum != before.checksum and after.version == 2
    assert after.ingested_at == before.ingested_at
    assert len(rig.repo.list_sources()) == 1


def test_a_renamed_file_refreshes_the_same_logical_source() -> None:
    rig = make_rig()
    ingest_cv(rig)
    result = _refresh(rig, CV_TEXT + "\n", name="cv_final_v2.txt")
    assert result.ok and result.data is not None
    assert result.data.source_id == _cv_id(rig)
    source = rig.repo.get_source(_cv_id(rig))
    assert source is not None and source.label == "cv_final_v2.txt"
    assert len(rig.repo.list_sources()) == 1


def test_an_identical_refresh_is_idempotent() -> None:
    rig = make_rig()
    ingest_cv(rig)
    before = _state(rig)
    for _ in range(2):
        result = _refresh(rig, CV_TEXT)
        assert result.ok and result.data is not None
        assert result.data.status == "unchanged"
    assert _state(rig) == before


def test_refreshing_an_unknown_source_changes_nothing() -> None:
    rig = make_rig()
    ingest_cv(rig)
    before = _state(rig)
    result = rig.service.refresh_source(
        OWNER, "s_missing", name="cv.txt", resource_type="txt", content=b"x"
    )
    assert not result.ok and result.reason == "source_not_found"
    assert _state(rig) == before


def test_a_refresh_needs_write_permission() -> None:
    rig = make_rig()
    ingest_cv(rig)
    before = _state(rig)
    result = rig.service.refresh_source(
        STRANGER,
        _cv_id(rig),
        name=CV_ID_NAME,
        resource_type="txt",
        content=CV_TEXT.replace("Python", "Rust").encode(),
    )
    assert not result.ok and result.permission == "deny"
    assert _state(rig) == before


# ------------------------------------------------------ no ghost evidence


def test_a_removed_statement_disappears_after_refresh() -> None:
    rig = make_rig()
    ingest_cv(rig)
    assert _find(rig, "Kubernetes") is not None
    old_ids = {e.evidence_id for e in rig.repo.list_all_evidence()}
    assert _refresh(rig, CV_TEXT.replace(", Kubernetes", "")).ok
    assert _find(rig, "Kubernetes") is None
    # No evidence of the OLD version survives unless the new version states it
    # again at the same place.
    new = rig.repo.list_all_evidence()
    assert all("Kubernetes" not in e.evidence_reference for e in new)
    assert {e.evidence_id for e in new} != old_ids


def test_new_evidence_appears_after_refresh() -> None:
    rig = make_rig()
    ingest_cv(rig)
    assert _find(rig, "Terraform") is None
    assert _refresh(rig, CV_TEXT.replace("Kubernetes", "Kubernetes, Terraform")).ok
    terraform = _find(rig, "Terraform")
    assert terraform is not None and terraform.accepted
    assert terraform.strength is EvidenceStrength.SINGLE_SOURCE


def test_every_evidence_record_of_the_source_is_from_the_new_version() -> None:
    rig = make_rig()
    ingest_cv(rig)
    changed = CV_TEXT.replace("SKILLS\n", "SKILLS\nGo, ").replace(
        "- Deployed on AWS using Docker\n", ""
    )
    assert _refresh(rig, changed).ok
    fresh = make_rig()
    assert fresh.ingest(changed).ok
    # Exactly what a first ingest of the new version produces: nothing more.
    assert {e.evidence_id for e in rig.repo.list_all_evidence()} == {
        e.evidence_id for e in fresh.repo.list_all_evidence()
    }
    assert {c.claim_id for c in rig.repo.list_claims()} == {
        c.claim_id for c in fresh.repo.list_claims()
    }


# ---------------------------------------------------- recompute on refresh


def test_changed_dates_recompute_conflicts() -> None:
    rig = make_rig()
    ingest_cv(rig)
    _linkedin(rig, LINKEDIN_AGREEING_CSV)
    assert rig.service.get_conflicts(OWNER).data == ()
    # The CV now says Apr 2019: the sources disagree, a conflict appears ...
    assert _refresh(rig, CV_TEXT.replace("Mar 2019", "Apr 2019")).ok
    conflicts = rig.service.get_conflicts(OWNER).data
    assert conflicts and any(c.attribute == "start" for c in conflicts)
    job = _find(rig, "Acme")
    assert job is not None and "start" in job.conflicted_attributes
    # ... and when the CV agrees again, it is gone.
    assert _refresh(rig, CV_TEXT).ok
    assert rig.service.get_conflicts(OWNER).data == ()


def test_refresh_recomputes_skills_publications_education_and_gaps() -> None:
    rig = make_rig()
    ingest_cv(rig)
    assert rig.ingest(
        PAPER_TEXT, name="paper.txt", source_type=SourceType.PUBLICATION
    ).ok
    pubs = rig.service.get_publications(OWNER).data
    assert pubs and pubs[0].strength is EvidenceStrength.CORROBORATED
    gaps = rig.service.get_profile_gaps(OWNER).data or ()
    assert not any(g.kind is GapKind.PUBLICATION_MISSING_FROM_CV for g in gaps)
    assert rig.service.get_education(OWNER).data

    stripped = CV_TEXT.split("EDUCATION")[0] + "SKILLS\nPython\n"
    assert _refresh(rig, stripped).ok

    pubs = rig.service.get_publications(OWNER).data
    assert pubs and pubs[0].strength is EvidenceStrength.SINGLE_SOURCE
    gaps = rig.service.get_profile_gaps(OWNER).data or ()
    assert any(g.kind is GapKind.PUBLICATION_MISSING_FROM_CV for g in gaps)
    assert rig.service.get_education(OWNER).data == ()
    skills = rig.service.get_skills(OWNER).data or ()
    assert "React" not in {v.attributes.get("display") for v in skills}


def test_corroborating_evidence_from_another_source_survives_a_refresh() -> None:
    rig = make_rig()
    ingest_cv(rig)
    _linkedin(rig)
    li_id = rig.source_id("Positions.csv", SourceType.LINKEDIN_EXPORT)
    li_before = [e for e in rig.repo.list_all_evidence() if e.source_id == li_id]
    assert li_before
    # The CV no longer mentions Acme at all.
    changed = CV_TEXT.replace(
        "Senior Software Engineer, Acme Analytics, Mar 2019 - Jun 2022\n", ""
    )
    assert _refresh(rig, changed).ok
    li_after = [e for e in rig.repo.list_all_evidence() if e.source_id == li_id]
    assert li_after == li_before  # byte-identical records, none removed
    job = next(
        v
        for v in _views(rig)
        if v.category is ClaimCategory.EMPLOYMENT and "Acme" in v.statement
    )
    assert {e.source_id for e in job.evidence} == {li_id}
    assert job.strength is EvidenceStrength.SINGLE_SOURCE


# ------------------------------------------------------- failure: rollback


@pytest.mark.parametrize(
    "content",
    [
        b"",  # nothing readable
        b"\xff\xfe\x00garbage" * 3,  # not text
        b"api_key = sk-live-" + b"A1b2C3d4E5f6G7h8I9j0K1l2M3n4" + b"\n",  # a secret
    ],
)
def test_a_failed_refresh_leaves_the_old_state_unchanged(content: bytes) -> None:
    rig = make_rig()
    ingest_cv(rig)
    _linkedin(rig)
    before = _state(rig)
    result = rig.service.refresh_source(
        OWNER, _cv_id(rig), name=CV_ID_NAME, resource_type="txt", content=content
    )
    assert not result.ok
    assert _state(rig) == before


def test_a_failure_inside_the_commit_rolls_everything_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_rig()
    ingest_cv(rig)
    _linkedin(rig)
    before = _state(rig)

    def boom(*_: object) -> set[str]:
        raise RuntimeError("storage failure after the old evidence was removed")

    # Fails AFTER the old evidence was removed and the new inserted.
    monkeypatch.setattr(rig.repo, "_drop_orphans", boom)
    result = _refresh(rig, CV_TEXT.replace(", Kubernetes", ""))
    assert not result.ok and result.reason == "ingestion_failed"
    assert _state(rig) == before
    assert _find(rig, "Kubernetes") is not None


def test_a_failed_extraction_of_a_publication_refresh_changes_nothing() -> None:
    rig = make_rig()
    assert rig.ingest(
        PAPER_TEXT, name="paper.txt", source_type=SourceType.PUBLICATION
    ).ok
    before = _state(rig)
    result = rig.service.refresh_source(
        OWNER,
        rig.source_id("paper.txt", SourceType.PUBLICATION),
        name="paper.txt",
        resource_type="txt",
        content=b"abc",  # no usable title: extraction fails closed
    )
    assert not result.ok
    assert _state(rig) == before


# ----------------------------------------------------- owner attestations


def test_an_attestation_is_carried_to_the_new_version_when_still_stated() -> None:
    from tests.test_professional_provenance import STATEMENT, _rig

    rig = _rig()
    ingest_cv(rig, use_llm=True)
    claim = next(v for v in _views(rig) if STATEMENT in v.statement)
    assert rig.service.confirm_claim(OWNER, claim.claim_id).ok
    assert _refresh(rig, CV_TEXT + "\nEXTRA\nnothing\n", use_llm=True).ok
    after = next(v for v in _views(rig) if STATEMENT in v.statement)
    assert after.accepted and after.review is ClaimReviewState.OWNER_CONFIRMED
    (attestation,) = [
        e for e in after.evidence if e.nature is EvidenceNature.OWNER_ATTESTATION
    ]
    basis_ids = {e.evidence_id for e in after.evidence}
    assert attestation.basis_evidence_id in basis_ids
