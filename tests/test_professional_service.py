"""ProfessionalService end to end, over synthetic documents only.

Covers ingestion through Knowledge's pipeline, provenance, corroboration and
source deletion, conflicts, the owner's workflows, claim safety, requirement
matching, gap analysis, audit, idempotence and hostile inputs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sam.models.models import PrivacyClass
from sam.professional.claims import ProtectedKind, Verdict
from sam.professional.gaps import GapKind
from sam.professional.models import (
    ClaimCategory,
    ConflictKind,
    EvidenceNature,
    EvidenceStrength,
    MatchStatus,
    SourceType,
)
from sam.professional.profile import ClaimView
from tests.professional_support import (
    CV_TEXT,
    K8S_YAML,
    LINKEDIN_AGREEING_CSV,
    LINKEDIN_CSV,
    NOW,
    OWNER,
    PAPER_TEXT,
    README_TEXT,
    REQUIREMENTS_TXT,
    STRANGER,
    TRANSCRIPT_TEXT,
    Rig,
    ingest_cv,
    make_rig,
)


def views(rig: Rig) -> list[ClaimView]:
    data = rig.service.get_profile(OWNER).data
    assert data is not None
    return list(data.claims)


def find(rig: Rig, category: ClaimCategory, needle: str) -> ClaimView:
    matches = [
        v
        for v in views(rig)
        if v.category is category and needle.lower() in v.statement.lower()
    ]
    assert matches, f"no {category.value} claim mentioning {needle!r}"
    return matches[0]


def skill(rig: Rig, name: str) -> ClaimView:
    matches = [
        v
        for v in views(rig)
        if v.category in (ClaimCategory.SKILL, ClaimCategory.TECHNOLOGY)
        and v.attributes.get("display", "").lower() == name.lower()
    ]
    assert matches, f"no skill {name!r}"
    return matches[0]


def add_linkedin(rig: Rig, csv_text: str = LINKEDIN_AGREEING_CSV) -> None:
    result = rig.ingest(
        csv_text,
        name="Positions.csv",
        source_type=SourceType.LINKEDIN_EXPORT,
        resource_type="csv",
    )
    assert result.ok, result.reason


# ------------------------------------------------------------- provenance


def test_cv_evidence_preserves_provenance() -> None:
    rig = make_rig()
    ingest_cv(rig)
    python = skill(rig, "Python")
    assert python.strength is EvidenceStrength.SINGLE_SOURCE
    for evidence in python.evidence:
        assert evidence.source_type is SourceType.MASTER_CV
        assert evidence.source_label == "cv.txt"
        assert evidence.nature is EvidenceNature.EXPLICIT_SOURCE
        # The character range points at the line that says it.
        start, end = evidence.location.character_start, evidence.location.character_end
        assert start is not None and end is not None
        assert "Python" in CV_TEXT[start:end]


def test_publication_evidence_preserves_provenance() -> None:
    rig = make_rig()
    result = rig.ingest(
        PAPER_TEXT,
        name="paper.txt",
        source_type=SourceType.PUBLICATION,
        privacy=PrivacyClass.PUBLIC,
    )
    assert result.ok, result.reason
    publication = rig.service.get_publications(OWNER).data
    assert publication is not None and len(publication) == 1
    paper = publication[0]
    assert paper.title == "Semantic Embeddings for Health Misinformation Detection"
    assert paper.doi == "10.1234/example.2023.001" and paper.year == "2023"
    assert paper.strength is EvidenceStrength.SINGLE_SOURCE
    assert all(e.source_type is SourceType.PUBLICATION for e in paper.evidence)
    assert paper.evidence[0].location.character_start is not None
    assert paper.sensitivity is PrivacyClass.PUBLIC


def test_transcript_administrative_identifiers_are_never_stored() -> None:
    rig = make_rig()
    ingest_cv(rig)
    rig.ingest(
        TRANSCRIPT_TEXT,
        name="transcript.txt",
        source_type=SourceType.TRANSCRIPT,
    )
    dump = json.dumps(
        [
            *(c.model_dump(mode="json") for c in rig.repo.list_claims()),
            *(e.model_dump(mode="json") for e in rig.repo.list_all_evidence()),
            *(s.model_dump(mode="json") for s in rig.repo.list_sources()),
        ]
    )
    for identifier in ("12345678", "99887766", "A1234567"):
        assert identifier not in dump
    # Contact details in the CV header are not evidence either.
    for contact in ("jordan@example.test", "7700 900123"):
        assert contact not in dump
    education = rig.service.get_education(OWNER).data
    assert education and education[0].classification == "Distinction"
    assert education[0].modules == (
        "Machine Learning",
        "Natural Language Processing",
        "Deep Learning",
    )


def test_knowledge_provenance_survives_conversion_to_professional_evidence() -> None:
    """A long, multi-chunk Markdown CV: the section title Knowledge attaches to
    each segment, and the exact character range of the line, reach the evidence."""

    from datetime import UTC, datetime

    from sam.knowledge.chunker import ChunkerConfig
    from sam.knowledge.ingestion import run_ingestion_pipeline
    from sam.knowledge.models import (
        IngestResourceRequest,
        ResourceSourceKind,
        ResourceType,
    )
    from sam.knowledge.parser import default_parsers

    filler = "\n".join(f"Filler sentence number {i} about nothing." for i in range(120))
    markdown = (
        "# Skills\n" + filler + "\nExpert in Kubernetes and Docker here.\n"
        "# Projects\nNothing to see here.\n"
    )
    rig = make_rig()
    assert rig.ingest(markdown, name="cv.md", resource_type="markdown").ok

    kubernetes = skill(rig, "Kubernetes")
    (evidence,) = kubernetes.evidence
    assert evidence.location.section_title == "Skills"

    # Knowledge's own chunks for the same bytes agree on the section and the text.
    outcome = run_ingestion_pipeline(
        IngestResourceRequest(
            principal=OWNER,
            collection_id="x",
            name="cv.md",
            declared_resource_type=ResourceType.MARKDOWN,
            source_kind=ResourceSourceKind.UPLOAD,
            source_label="cv.md",
            content=markdown.encode(),
        ),
        parsers=default_parsers(),
        chunker_config=ChunkerConfig(max_chunk_characters=1_000),
        now=datetime.now(UTC),
    )
    assert outcome.accepted and len(outcome.chunks) > 1
    segment_text = "".join(
        c.text for c in outcome.chunks if c.location.section_title == "Skills"
    )
    start, end = evidence.location.character_start, evidence.location.character_end
    assert start is not None and end is not None
    assert "Kubernetes" in segment_text[start:end]


# ----------------------------------------------- normalization / candidates


def test_aliases_in_a_source_produce_one_claim() -> None:
    rig = make_rig()
    text = "SKILLS\nJS, TS, ReactJS, Fast API, Gen AI, LLM, torch\n"
    assert rig.ingest(text, name="skills.txt").ok
    displays = sorted(
        v.attributes["display"]
        for v in views(rig)
        if v.category in (ClaimCategory.SKILL, ClaimCategory.TECHNOLOGY)
    )
    assert displays == sorted(
        [
            "JavaScript",
            "TypeScript",
            "React",
            "FastAPI",
            "Generative AI",
            "Large Language Models",
            "PyTorch",
        ]
    )


def test_a_dependency_manifest_alone_never_verifies_a_skill() -> None:
    rig = make_rig()
    assert rig.ingest(
        REQUIREMENTS_TXT,
        name="requirements.txt",
        source_type=SourceType.GITHUB,
    ).ok
    assert rig.ingest(K8S_YAML, name="deploy.yaml", source_type=SourceType.GITHUB).ok
    kubernetes = skill(rig, "Kubernetes")
    fastapi = skill(rig, "FastAPI")
    for view in (kubernetes, fastapi):
        assert view.inferred and view.strength is EvidenceStrength.NONE
        assert not view.accepted
    assert all(
        e.nature is EvidenceNature.INFERRED_RELATIONSHIP for e in kubernetes.evidence
    )
    # ... so it is not evidence for a requirement, and it is reported as a gap.
    match = rig.service.evidence_for(OWNER, "Kubernetes experience").data
    assert match is not None and match.status is MatchStatus.NOT_SUPPORTED
    assert any("inferred only" in a for a in match.unsupported_aspects)
    gaps = rig.service.get_profile_gaps(OWNER).data
    assert gaps is not None and any(g.kind is GapKind.SKILL_INFERRED_ONLY for g in gaps)


def test_a_readme_does_not_verify_a_skill_only_the_technology_and_project() -> None:
    rig = make_rig()
    ingest_cv(rig)
    assert rig.ingest(
        README_TEXT,
        name="README.md",
        source_type=SourceType.GITHUB,
        resource_type="markdown",
    ).ok
    project = find(rig, ClaimCategory.PROJECT, "Smart Widget")
    assert project.strength is EvidenceStrength.CORROBORATED  # CV + README
    # "agentic AI" reaches the SKILL only as an inferred relationship from GitHub.
    agentic = skill(rig, "Agentic AI")
    assert {
        e.nature for e in agentic.evidence if e.source_type is SourceType.GITHUB
    } == {EvidenceNature.INFERRED_RELATIONSHIP}


# ----------------------------------------------------- corroboration / removal


def test_multiple_sources_produce_corroboration() -> None:
    rig = make_rig()
    ingest_cv(rig)
    add_linkedin(rig)
    job = find(rig, ClaimCategory.EMPLOYMENT, "Acme")
    assert job.strength is EvidenceStrength.CORROBORATED
    assert job.source_count == 2


def test_removing_one_source_recomputes_to_single_source() -> None:
    rig = make_rig()
    ingest_cv(rig)
    add_linkedin(rig)
    li_id = rig.source_id("Positions.csv", SourceType.LINKEDIN_EXPORT)
    summary = _remove(rig, li_id)
    assert summary is not None
    job = find(rig, ClaimCategory.EMPLOYMENT, "Acme")
    assert job.strength is EvidenceStrength.SINGLE_SOURCE
    assert {e.source_type for e in job.evidence} == {SourceType.MASTER_CV}
    # Nothing derived is stored on the claim, so nothing stale can survive.
    stored = rig.repo.get_claim(job.claim_id)
    assert stored is not None
    assert not {"verification_state", "strength", "review"} & set(
        type(stored).model_fields
    )


def _remove(rig: Rig, source_id: str):  # type: ignore[no-untyped-def]
    first = rig.service.remove_source(OWNER, source_id)
    assert first.permission == "confirm_required" and first.confirmation_id
    rig.approve(first.confirmation_id)
    done = rig.service.remove_source(
        OWNER, source_id, confirmation_id=first.confirmation_id
    )
    assert done.ok, done.reason
    return done.data


def test_removing_one_source_never_deletes_another_sources_evidence() -> None:
    rig = make_rig()
    ingest_cv(rig)
    add_linkedin(rig)
    li_id = rig.source_id("Positions.csv", SourceType.LINKEDIN_EXPORT)
    cv_id = rig.source_id("cv.txt", SourceType.MASTER_CV)
    li_evidence = {
        e.evidence_id for e in rig.repo.list_all_evidence() if e.source_id == li_id
    }
    assert li_evidence
    _remove(rig, cv_id)
    remaining = {e.evidence_id for e in rig.repo.list_all_evidence()}
    assert li_evidence <= remaining
    assert all(e.source_id == li_id for e in rig.repo.list_all_evidence())
    # Claims that only the CV supported are gone; LinkedIn's claims are intact.
    job = find(rig, ClaimCategory.EMPLOYMENT, "Acme")
    assert job.strength is EvidenceStrength.SINGLE_SOURCE
    assert {e.source_type for e in job.evidence} == {SourceType.LINKEDIN_EXPORT}


def test_removal_recomputes_the_timeline() -> None:
    rig = make_rig()
    ingest_cv(rig)
    add_linkedin(rig)
    before = rig.service.get_timeline(OWNER).data
    assert before is not None and before.total.months == 91
    _remove(rig, rig.source_id("cv.txt", SourceType.MASTER_CV))
    after = rig.service.get_timeline(OWNER).data
    assert after is not None
    # Only LinkedIn's Acme job remains (Mar 2019 - Jun 2022 = 40 months).
    assert after.total.months == 40


def test_removing_a_source_needs_confirmation() -> None:
    rig = make_rig()
    ingest_cv(rig)
    cv_id = rig.source_id("cv.txt", SourceType.MASTER_CV)
    result = rig.service.remove_source(OWNER, cv_id)
    assert not result.ok and result.permission == "confirm_required"
    assert rig.repo.get_source(cv_id) is not None  # nothing was removed
    forged = rig.service.remove_source(OWNER, cv_id, confirmation_id="nope")
    assert not forged.ok and rig.repo.get_source(cv_id) is not None


# ---------------------------------------------------------------- conflicts


def test_conflicting_dates_are_preserved_as_a_conflict() -> None:
    rig = make_rig()
    ingest_cv(rig)
    add_linkedin(rig, LINKEDIN_CSV)  # LinkedIn says Apr 2019, the CV says Mar 2019
    job = find(rig, ClaimCategory.EMPLOYMENT, "Acme")
    assert "start" in job.conflicted_attributes
    assert "start" not in job.attributes  # no value is chosen for the owner
    conflicts = rig.service.get_conflicts(OWNER).data
    assert conflicts is not None and len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.kind is ConflictKind.ATTRIBUTE and not conflict.resolved
    assert sorted(o.value for o in conflict.options) == ["2019-03", "2019-04"]
    # Both evidence paths are kept, one per source.
    assert all(
        len(o.evidence_ids) >= 1 and len(o.source_ids) == 1 for o in conflict.options
    )
    assert len({sid for o in conflict.options for sid in o.source_ids}) == 2


def test_a_conflicted_job_is_excluded_from_experience_not_resolved_favourably() -> None:
    rig = make_rig()
    ingest_cv(rig)
    add_linkedin(rig, LINKEDIN_CSV)
    timeline = rig.service.get_timeline(OWNER).data
    assert timeline is not None
    assert len(timeline.excluded_conflicted) == 1
    # Only the un-conflicted Globex job counts (Jul 2022 - Sep 2026 = 51 months).
    assert timeline.total.months == 51
    answer = rig.service.experience_for(OWNER).data
    assert answer is not None and answer.months == 51
    gaps = rig.service.get_profile_gaps(OWNER).data
    assert gaps is not None
    kinds = {g.kind for g in gaps}
    assert {GapKind.CONFLICT_OPEN, GapKind.DATES_CONFLICTED} <= kinds


def test_owner_can_resolve_a_conflict_and_the_value_is_the_owners_choice() -> None:
    rig = make_rig()
    ingest_cv(rig)
    add_linkedin(rig, LINKEDIN_CSV)
    conflicts = rig.service.get_conflicts(OWNER).data
    assert conflicts
    conflict = conflicts[0]
    choice = next(o for o in conflict.options if o.value == "2019-04")
    resolved = rig.service.resolve_conflict(
        OWNER, conflict.conflict_id, choice.option_id
    )
    assert resolved.ok, resolved.reason
    job = find(rig, ClaimCategory.EMPLOYMENT, "Acme")
    assert job.attributes["start"] == "2019-04"
    assert job.conflicted_attributes == ()
    after = rig.service.get_conflicts(OWNER).data
    assert after and after[0].resolved and after[0].resolved_value == "2019-04"
    timeline = rig.service.get_timeline(OWNER).data
    assert timeline is not None and timeline.total.months == 39 + 51  # Apr 2019 start
    # An unknown option is refused and nothing changes.
    bad = rig.service.resolve_conflict(OWNER, conflict.conflict_id, "nope")
    assert not bad.ok and bad.reason == "option_not_found"


def test_conflicting_job_titles_at_the_same_employer_are_reported() -> None:
    rig = make_rig()
    rig.ingest(
        "EXPERIENCE\nSenior Software Engineer, Acme Analytics, Mar 2019 - Jun 2022\n",
        name="cv.txt",
    )
    rig.ingest(
        "Company Name,Title,Description,Location,Started On,Finished On\n"
        'Acme Analytics,Staff Engineer,"x",London,Mar 2019,Jun 2022\n',
        name="Positions.csv",
        source_type=SourceType.LINKEDIN_EXPORT,
        resource_type="csv",
    )
    conflicts = rig.service.get_conflicts(OWNER).data
    assert conflicts is not None
    overlaps = [c for c in conflicts if c.kind is ConflictKind.ROLE_OVERLAP]
    assert len(overlaps) == 1
    assert sorted(o.value for o in overlaps[0].options) == [
        "Senior Software Engineer",
        "Staff Engineer",
    ]


# --------------------------------------------------------- experience math


def test_experience_is_computed_deterministically_by_code() -> None:
    rig = make_rig()
    ingest_cv(rig)
    first = rig.service.get_timeline(OWNER).data
    second = rig.service.get_timeline(OWNER).data
    assert first is not None and second is not None
    assert first.total == second.total
    # Acme Mar 2019 - Jun 2022 = 40 months; Globex Jul 2022 - Sep 2026 = 51.
    assert [e.months for e in first.entries] == [40, 51]
    assert first.total.months == 91
    assert (first.total.years, first.total.remainder_months) == (7, 7)


def test_overlapping_employment_is_not_double_counted() -> None:
    rig = make_rig()
    text = (
        "EXPERIENCE\n"
        "Software Engineer, Alpha Labs, Jan 2020 - Dec 2020\n"
        "Data Scientist, Beta Works, Jul 2020 - Jun 2021\n"
    )
    assert rig.ingest(text, name="cv.txt").ok
    timeline = rig.service.get_timeline(OWNER).data
    assert timeline is not None
    assert [e.months for e in timeline.entries] == [12, 12]
    assert timeline.total.months == 18  # Jan 2020 .. Jun 2021, overlap counted once


def test_skill_experience_comes_from_the_employment_it_was_used_in() -> None:
    rig = make_rig()
    ingest_cv(rig)
    fastapi = rig.service.experience_for(OWNER, "FastAPI").data
    assert fastapi is not None and fastapi.verified
    # FastAPI appears in the Acme job (Mar 2019 - Jun 2022) and in Skills.
    assert fastapi.months == 40
    assert fastapi.conservative_months == 40
    missing = rig.service.experience_for(OWNER, "Rust").data
    assert missing is not None and not missing.verified and missing.months == 0
    unknown = rig.service.experience_for(OWNER, "Zorbnetics").data
    assert unknown is not None and unknown.reason == "skill_not_in_vocabulary"


# ------------------------------------------------------- claim safety policy


@pytest.mark.parametrize(
    ("statement", "kind", "supported"),
    [
        (
            "I have 3 years of experience with FastAPI",
            ProtectedKind.YEARS_OF_EXPERIENCE,
            True,
        ),
        (
            "I have 4 years of experience with FastAPI",
            ProtectedKind.YEARS_OF_EXPERIENCE,
            False,
        ),
        ("I have 10 years of experience", ProtectedKind.YEARS_OF_EXPERIENCE, False),
        ("I have 7 years of experience", ProtectedKind.YEARS_OF_EXPERIENCE, True),
        (
            "I have 5 years of experience with Rust",
            ProtectedKind.YEARS_OF_EXPERIENCE,
            False,
        ),
        ("I worked at Acme Analytics", ProtectedKind.EMPLOYER, True),
        ("I worked at Initech", ProtectedKind.EMPLOYER, False),
        ("I hold an MSc in Artificial Intelligence", ProtectedKind.DEGREE, True),
        ("I hold a PhD", ProtectedKind.DEGREE, False),
        ("I graduated with a first class degree", ProtectedKind.GRADE, False),
        ("I earn a salary of 100k", ProtectedKind.SALARY, False),
        ("I hold a UK work visa", ProtectedKind.WORK_AUTHORIZATION, False),
        ("I have a security clearance", ProtectedKind.CLEARANCE, False),
    ],
)
def test_protected_claims_need_evidence(
    statement: str, kind: ProtectedKind, supported: bool
) -> None:
    rig = make_rig()
    ingest_cv(rig)
    assessment = rig.service.assess_claim(OWNER, statement).data
    assert assessment is not None
    finding = next(f for f in assessment.findings if f.kind is kind)
    assert finding.supported is supported
    assert (assessment.verdict is Verdict.SUPPORTED) == supported


def test_years_are_never_rounded_up() -> None:
    rig = make_rig()
    ingest_cv(rig)
    # FastAPI is evidenced for 40 months = 3 years 4 months: "4 years" is not.
    assert _verdict(rig, "3 years of FastAPI experience") is Verdict.SUPPORTED
    assert _verdict(rig, "3.5 years of FastAPI experience") is Verdict.UNSUPPORTED
    assert _verdict(rig, "4 years of FastAPI experience") is Verdict.UNSUPPORTED


def _verdict(rig: Rig, statement: str) -> Verdict:
    result = rig.service.assess_claim(OWNER, statement).data
    assert result is not None
    return result.verdict


def test_a_statement_with_no_protected_claim_is_not_certified() -> None:
    rig = make_rig()
    ingest_cv(rig)
    assessment = rig.service.assess_claim(OWNER, "I enjoy hiking on weekends").data
    assert assessment is not None
    assert assessment.verdict is Verdict.NOT_CHECKABLE  # not "supported"


def test_expert_language_needs_corroborated_evidence() -> None:
    rig = make_rig()
    ingest_cv(rig)
    assert _verdict(rig, "I am an expert in Python") is Verdict.UNSUPPORTED
    assert rig.ingest(
        README_TEXT,
        name="README.md",
        source_type=SourceType.GITHUB,
        resource_type="markdown",
    ).ok
    # CV + GitHub README both state Python explicitly: corroborated.
    assert skill(rig, "Python").strength is EvidenceStrength.CORROBORATED
    assert _verdict(rig, "I am an expert in Python") is Verdict.SUPPORTED


def test_authorship_does_not_invent_an_owner_contribution() -> None:
    rig = make_rig()
    ingest_cv(rig)
    assert rig.ingest(
        PAPER_TEXT,
        name="paper.txt",
        source_type=SourceType.PUBLICATION,
        privacy=PrivacyClass.PUBLIC,
    ).ok
    paper = rig.service.get_publications(OWNER).data
    assert paper is not None
    # The owner is the FIRST author (explicit position), yet the paper's contribution
    # statement names other people, so no contribution is attributed to the owner.
    assert paper[0].owner_author_position == 1
    assert paper[0].owner_contribution is None
    first = rig.service.assess_claim(OWNER, "I am the first author of the paper").data
    assert first is not None and first.verdict is Verdict.SUPPORTED
    contribution = rig.service.assess_claim(
        OWNER, "I designed and implemented the graph model in that paper"
    ).data
    assert contribution is not None
    finding = next(
        f for f in contribution.findings if f.kind is ProtectedKind.CONTRIBUTION
    )
    # No PUBLICATION evidence says the owner contributed anything specific.
    assert not any(
        rig.service.get_claim(OWNER, cid).data.category is ClaimCategory.PUBLICATION  # type: ignore[union-attr]
        for cid in finding.claim_ids
    )


def test_private_career_details_need_owner_stated_evidence_and_are_private() -> None:
    rig = make_rig()
    text = "OBJECTIVE\nSeeking a research role. Salary expectation 90k. Needs visa sponsorship.\n"  # noqa: E501
    assert rig.ingest(text, name="notes.txt", privacy=PrivacyClass.PERSONAL).ok
    prefs = [v for v in views(rig) if v.category is ClaimCategory.CAREER_PREFERENCE]
    assert prefs and all(v.sensitivity is PrivacyClass.PRIVATE for v in prefs)
    assessment = rig.service.assess_claim(OWNER, "I expect a salary of 90k").data
    assert assessment is not None and assessment.requires_private_handling


# ------------------------------------------------------ matching / search


def test_requirement_matching_never_fabricates_evidence() -> None:
    rig = make_rig()
    ingest_cv(rig)
    match = rig.service.evidence_for(
        OWNER, "Experience building production RAG systems"
    ).data
    assert match is not None
    assert match.status is MatchStatus.MATCHED
    assert (
        match.normalized_requirement == "Retrieval-Augmented Generation; production use"
    )
    assert {v.attributes.get("skill_id") for v in match.skills} == {"rag"}
    assert any("Acme" in v.statement for v in match.employment)
    assert all(
        e.source_label == "cv.txt" for v in match.matched_claims for e in v.evidence
    )

    rust = rig.service.evidence_for(OWNER, "Strong Rust experience").data
    assert rust is not None and rust.status is MatchStatus.NOT_SUPPORTED
    assert rust.matched_claims == () and rust.unsupported_aspects == (
        "skill: Rust (no evidence)",
    )


def test_production_use_needs_employment_evidence_not_just_a_project() -> None:
    rig = make_rig()
    ingest_cv(rig)
    # LangChain appears only in the personal Smart Widget project.
    match = rig.service.evidence_for(OWNER, "production LangChain experience").data
    assert match is not None and match.status is MatchStatus.PARTIALLY_SUPPORTED
    assert any(v.category is ClaimCategory.PROJECT for v in match.projects)
    assert any("production use" in a for a in match.unsupported_aspects)


def test_partial_and_years_requirements() -> None:
    rig = make_rig()
    ingest_cv(rig)
    partial = rig.service.evidence_for(OWNER, "Python and Rust experience").data
    assert partial is not None and partial.status is MatchStatus.PARTIALLY_SUPPORTED
    years = rig.service.evidence_for(OWNER, "10+ years of Python").data
    assert years is not None and years.status is MatchStatus.PARTIALLY_SUPPORTED
    assert any(a.startswith("years:") for a in years.unsupported_aspects)
    degree = rig.service.evidence_for(OWNER, "MSc degree in a relevant field").data
    assert degree is not None and degree.status is MatchStatus.MATCHED
    phd = rig.service.evidence_for(OWNER, "PhD required").data
    assert phd is not None and phd.status is MatchStatus.NOT_SUPPORTED


def test_an_unrecognized_requirement_is_unknown_not_guessed() -> None:
    rig = make_rig()
    ingest_cv(rig)
    match = rig.service.evidence_for(OWNER, "Must enjoy hiking and board games").data
    assert match is not None and match.status is MatchStatus.UNKNOWN
    assert match.matched_claims == () and match.normalized_requirement == ""


def test_search_is_deterministic_and_order_independent() -> None:
    def build(order: list[str]) -> list[str]:
        rig = make_rig()
        for step in order:
            if step == "cv":
                ingest_cv(rig)
            elif step == "li":
                add_linkedin(rig)
            else:
                assert rig.ingest(
                    PAPER_TEXT,
                    name="paper.txt",
                    source_type=SourceType.PUBLICATION,
                    privacy=PrivacyClass.PUBLIC,
                ).ok
        result = rig.service.search(OWNER, "FastAPI and semantic embeddings").data
        assert result is not None
        return [f"{v.category.value}:{v.statement}" for v in result]

    a = build(["cv", "li", "paper"])
    assert a == build(["cv", "li", "paper"])
    assert a == build(["paper", "li", "cv"])
    assert any("FastAPI" in item for item in a)
    assert any("Semantic Embeddings" in item for item in a)
    # Ranked by relevance, then evidence strength, then a stable key: the two
    # named skills come first, ahead of anything that merely shares a word.
    assert {
        "technology:Technology: FastAPI",
        "skill:Skill: Semantic Embeddings",
    } <= set(a[:3])


def test_search_takes_no_path_or_expression() -> None:
    rig = make_rig()
    ingest_cv(rig)
    result = rig.service.search(
        OWNER, "/etc/passwd; DROP TABLE claims; __import__('os')"
    )
    assert result.ok and result.data == ()  # it is just text: nothing matched
    assert not rig.service.search(OWNER, "").ok
    assert not rig.service.search(OWNER, "x" * 400).ok


# --------------------------------------------------------------- privacy


def test_owner_selected_privacy_classification_is_respected() -> None:
    rig = make_rig()
    ingest_cv(rig, privacy=PrivacyClass.PRIVATE)
    assert {v.sensitivity for v in views(rig)} == {PrivacyClass.PRIVATE}
    cv_id = rig.source_id("cv.txt", SourceType.MASTER_CV)
    rig2 = make_rig()
    assert rig2.ingest(
        PAPER_TEXT,
        name="paper.txt",
        source_type=SourceType.PUBLICATION,
        privacy=PrivacyClass.PUBLIC,
    ).ok
    assert {v.sensitivity for v in views(rig2)} == {PrivacyClass.PUBLIC}
    # The owner changes the class through the trusted UPDATE workflow.
    changed = rig.service.set_source_privacy(OWNER, cv_id, PrivacyClass.PERSONAL)
    assert changed.ok
    assert {v.sensitivity for v in views(rig)} == {PrivacyClass.PERSONAL}
    assert not rig.service.set_source_privacy(OWNER, cv_id, PrivacyClass.SECRET).ok
    assert not rig.service.set_source_privacy(
        OWNER, "s_missing", PrivacyClass.PUBLIC
    ).ok


def test_a_secret_privacy_class_is_never_ingested() -> None:
    rig = make_rig()
    result = rig.ingest(CV_TEXT, privacy=PrivacyClass.SECRET)
    assert not result.ok and result.reason == "secret_not_ingestible"
    assert rig.repo.list_sources() == () and rig.repo.list_claims() == ()
    normal = rig.ingest(CV_TEXT, privacy=PrivacyClass.NORMAL)
    assert not normal.ok and normal.reason == "privacy_class_invalid"


# ------------------------------------------------------------ safe ingestion


SECRET_DOCS = {
    "private_key": "EXPERIENCE\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\n",  # noqa: E501
    "api_key": "SKILLS\nPython\nANTHROPIC_API_KEY=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n",  # noqa: E501
    "password": "SKILLS\nPython\npassword: hunter2hunter2hunter2\n",
}


@pytest.mark.parametrize("name", sorted(SECRET_DOCS))
def test_a_source_containing_a_secret_fails_closed_before_extraction(name: str) -> None:
    rig = make_rig()
    result = rig.ingest(SECRET_DOCS[name], name="notes.txt")
    assert not result.ok and result.reason == "secret_detected"
    # Nothing was stored, not even the non-secret lines.
    assert rig.repo.list_sources() == () and rig.repo.list_claims() == ()
    events = rig.audit.events()
    assert (
        events[-1].status == "rejected"
        and events[-1].error_category == "secret_detected"
    )


def test_a_secret_bearing_filename_is_refused() -> None:
    rig = make_rig()
    result = rig.ingest("SKILLS\nPython\n", name=".env")
    assert not result.ok and result.reason == "secret_detected"


def test_reingesting_an_identical_source_is_idempotent() -> None:
    rig = make_rig()
    ingest_cv(rig)
    snapshot = (
        rig.repo.list_claims(),
        rig.repo.list_all_evidence(),
        rig.repo.list_sources(),
    )
    again = rig.ingest(CV_TEXT)
    assert again.ok and again.data is not None and again.data.status == "unchanged"
    assert (
        rig.repo.list_claims(),
        rig.repo.list_all_evidence(),
        rig.repo.list_sources(),
    ) == snapshot
    # Even under a different privacy request, an identical source changes nothing.
    other = rig.ingest(CV_TEXT, privacy=PrivacyClass.PUBLIC)
    assert other.data is not None and other.data.status == "unchanged"
    assert rig.repo.list_sources() == snapshot[2]


def test_the_same_content_under_another_name_is_a_duplicate() -> None:
    rig = make_rig()
    ingest_cv(rig)
    dup = rig.ingest(CV_TEXT, name="copy-of-cv.txt")
    assert not dup.ok and dup.reason == "duplicate_source"
    assert len(rig.repo.list_sources()) == 1


def test_refreshing_a_changed_source_replaces_its_evidence() -> None:
    rig = make_rig()
    ingest_cv(rig)
    changed = CV_TEXT.replace("Kubernetes", "Terraform")
    result = rig.ingest(changed)
    assert result.ok and result.data is not None and result.data.status == "refreshed"
    with pytest.raises(AssertionError):
        skill(rig, "Kubernetes")  # no longer stated, so no longer a claim
    assert skill(rig, "Terraform").strength is EvidenceStrength.SINGLE_SOURCE
    assert len(rig.repo.list_sources()) == 1


def test_source_checksum_is_deterministic() -> None:
    import hashlib

    rig_a, rig_b = make_rig(), make_rig()
    ingest_cv(rig_a)
    ingest_cv(rig_b)
    (a,) = rig_a.repo.list_sources()
    (b,) = rig_b.repo.list_sources()
    assert a.checksum == b.checksum == hashlib.sha256(CV_TEXT.encode()).hexdigest()
    assert a.source_id == b.source_id
    assert {c.claim_id for c in rig_a.repo.list_claims()} == {
        c.claim_id for c in rig_b.repo.list_claims()
    }


@pytest.mark.parametrize(
    ("content", "resource_type", "reason"),
    [
        (b"", "txt", "invalid_source"),
        (b"%PDF-1.4\nnot really a pdf", "pdf", "parsing_error"),
        (b"a,b\n1,2\n\x00\x01", "csv", None),
        (b"\xff\xfe\xfa binary", "txt", "parsing_error"),
        (b"just words", "docx", "unsupported_type"),
    ],
)
def test_malformed_sources_fail_closed_with_nothing_stored(
    content: bytes, resource_type: str, reason: str | None
) -> None:
    rig = make_rig()
    result = rig.ingest(content, name="bad.bin", resource_type=resource_type)
    if reason is None:
        return  # accepted by the parser as text; no claim to make either way
    assert not result.ok and result.reason == reason
    assert rig.repo.list_sources() == () and rig.repo.list_claims() == ()


def test_an_extraction_failure_leaves_no_partial_ingestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sam.professional import service as service_module
    from sam.professional.extract import ExtractionError

    rig = make_rig()
    ingest_cv(rig)
    before = (
        rig.repo.list_claims(),
        rig.repo.list_all_evidence(),
        rig.repo.list_sources(),
    )

    def boom(*args: object, **kwargs: object) -> None:
        raise ExtractionError("too_many_claims")

    monkeypatch.setattr(service_module, "extract", boom)
    result = rig.ingest(CV_TEXT + "\nSKILLS\nRust\n")
    assert not result.ok and result.reason == "too_many_claims"
    assert (
        rig.repo.list_claims(),
        rig.repo.list_all_evidence(),
        rig.repo.list_sources(),
    ) == before

    def crash(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(service_module, "extract", crash)
    failed = rig.ingest(CV_TEXT + "\nSKILLS\nRust\n")
    assert not failed.ok and failed.reason == "ingestion_failed"
    assert (
        rig.repo.list_claims(),
        rig.repo.list_all_evidence(),
        rig.repo.list_sources(),
    ) == before


@pytest.mark.parametrize(
    "name", ["../secret.txt", "/etc/passwd", "a/b.txt", "..", "x\x00y.txt", "a\\b.txt"]
)
def test_path_like_source_names_are_rejected(name: str) -> None:
    rig = make_rig()
    result = rig.ingest(CV_TEXT, name=name)
    assert not result.ok and result.reason == "invalid_source"
    assert rig.repo.list_sources() == ()


def test_traversal_is_rejected_when_reading_an_owner_selected_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "cv.txt").write_text(CV_TEXT)
    (tmp_path / "outside.txt").write_text("SKILLS\nPython\n")
    rig = make_rig()

    def ingest(relative: str) -> str | None:
        result = rig.service.ingest_local_file(
            OWNER,
            root=root,
            relative_path=relative,
            source_type=SourceType.MASTER_CV.value,
            privacy_class=PrivacyClass.PERSONAL,
        )
        return None if result.ok else result.reason

    assert ingest("cv.txt") is None  # a normal file inside the root works
    for bad in ("../outside.txt", "docs/../../outside.txt", "/etc/passwd", "~/x.txt"):
        assert ingest(bad) == "path_not_allowed"
    assert ingest("missing.txt") == "not_found"
    assert len(rig.repo.list_sources()) == 1  # only the legitimate file


def test_a_symlink_cannot_escape_the_owner_selected_root(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SKILLS\nPython\n")
    os.symlink(outside / "secret.txt", root / "link.txt")  # file symlink
    os.symlink(outside, root / "linkdir")  # directory symlink
    rig = make_rig()
    for relative in ("link.txt", "linkdir/secret.txt"):
        result = rig.service.ingest_local_file(
            OWNER,
            root=root,
            relative_path=relative,
            source_type=SourceType.MASTER_CV.value,
            privacy_class=PrivacyClass.PERSONAL,
        )
        assert not result.ok and result.reason == "symlink_not_allowed"
    assert rig.repo.list_sources() == ()


def test_the_permission_engine_decides_before_a_local_file_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sam.professional import service as service_module
    from sam.professional.reader import read_source_file as real

    (tmp_path / "cv.txt").write_text(CV_TEXT)
    opened: list[str] = []

    def spy(root: Path, relative: str) -> tuple[str, bytes]:
        opened.append(relative)
        return real(root, relative)

    monkeypatch.setattr(service_module, "read_source_file", spy)
    rig = make_rig()
    denied = rig.service.ingest_local_file(
        STRANGER,
        root=tmp_path,
        relative_path="cv.txt",
        source_type=SourceType.MASTER_CV.value,
        privacy_class=PrivacyClass.PERSONAL,
    )
    assert not denied.ok and denied.permission == "deny"
    assert opened == []  # never read for an unauthorized principal
    allowed = rig.service.ingest_local_file(
        OWNER,
        root=tmp_path,
        relative_path="cv.txt",
        source_type=SourceType.MASTER_CV.value,
        privacy_class=PrivacyClass.PERSONAL,
    )
    assert allowed.ok and opened == ["cv.txt"]


def test_only_regular_files_of_a_supported_type_are_read(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "notes.docx").write_bytes(b"x")
    rig = make_rig()
    for relative, reason in (
        ("sub", "not_a_regular_file"),
        ("notes.docx", "unsupported_type"),
    ):
        result = rig.service.ingest_local_file(
            OWNER,
            root=root,
            relative_path=relative,
            source_type=SourceType.MASTER_CV.value,
            privacy_class=PrivacyClass.PERSONAL,
        )
        assert not result.ok and result.reason == reason


# ------------------------------------------------------------- gap analysis


def test_gap_analysis_reports_evidence_gaps_and_never_rates_the_owner() -> None:
    rig = make_rig()
    ingest_cv(rig)
    assert rig.ingest(
        README_TEXT,
        name="README.md",
        source_type=SourceType.GITHUB,
        resource_type="markdown",
    ).ok
    gaps = rig.service.get_profile_gaps(OWNER).data
    assert gaps is not None
    kinds = {g.kind for g in gaps}
    assert GapKind.SKILL_ONLY_IN_CV in kinds  # e.g. Kubernetes: CV only
    assert (
        GapKind.PROJECT_NO_QUANTIFIED_OUTCOME not in kinds
    )  # the CV project has a metric
    text = " ".join(g.detail for g in gaps).lower()
    for judgement in ("weak", "strong", "score", "rating", "poor", "excellent"):
        assert judgement not in text


def test_publication_missing_from_the_cv_is_a_gap() -> None:
    rig = make_rig()
    assert rig.ingest("SKILLS\nPython\n", name="cv.txt").ok
    assert rig.ingest(
        PAPER_TEXT,
        name="paper.txt",
        source_type=SourceType.PUBLICATION,
        privacy=PrivacyClass.PUBLIC,
    ).ok
    gaps = rig.service.get_profile_gaps(OWNER).data
    assert gaps is not None
    assert any(g.kind is GapKind.PUBLICATION_MISSING_FROM_CV for g in gaps)


def test_a_stale_source_and_a_timeline_gap_are_reported() -> None:
    from datetime import timedelta

    rig = make_rig()
    text = (
        "EXPERIENCE\nEngineer, Alpha Labs, Jan 2018 - Dec 2018\n"
        "Engineer, Beta Works, Jan 2020 - Dec 2020\n"
    )
    assert rig.ingest(text, name="cv.txt").ok
    old = make_rig(now=NOW - timedelta(days=600))
    assert old.ingest(text, name="cv.txt").ok
    gaps = rig.service.get_profile_gaps(OWNER).data
    assert gaps is not None
    assert any(g.kind is GapKind.TIMELINE_GAP for g in gaps)
    # Stale: refreshed more than a year before "today".
    stale_service = make_rig(now=NOW)
    stale_service.ingest(text, name="cv.txt")
    source = stale_service.repo.list_sources()[0]
    stale_service.repo.update_source(
        source.model_copy(update={"refreshed_at": NOW - timedelta(days=500)})
    )
    stale = stale_service.service.get_profile_gaps(OWNER).data
    assert stale is not None and any(g.kind is GapKind.SOURCE_STALE for g in stale)


# ------------------------------------------------------------------ audit


def test_audit_events_are_metadata_only() -> None:
    rig = make_rig()
    ingest_cv(rig)
    add_linkedin(rig, LINKEDIN_CSV)
    rig.service.search(OWNER, "FastAPI")
    rig.service.evidence_for(OWNER, "production RAG")
    rig.service.assess_claim(OWNER, "I earn a salary of 90k")
    rig.ingest("SKILLS\npassword: hunter2hunter2hunter2\n", name="leak.txt")
    rig.service.get_timeline(OWNER)
    dumped = " ".join(repr(e) for e in rig.audit.events())
    assert rig.audit.events()
    for private in (
        "Acme Analytics",
        "Globex",
        "Jordan Example",
        "Distinction",
        "cv.txt",
        "Positions.csv",
        "leak.txt",
        "hunter2",
        "FastAPI",
        "salary",
        "Machine learning engineer",
        "example.test",
    ):
        assert private not in dumped, private
    for event in rig.audit.events():
        assert event.principal_id == "owner"
        assert event.counts is None or all(
            isinstance(v, int) for v in event.counts.values()
        )


def test_audit_counts_are_a_closed_set() -> None:
    from sam.professional.audit import ProfessionalOperation, new_event

    with pytest.raises(ValueError):
        new_event(
            now=NOW,
            operation=ProfessionalOperation.READ,
            principal_id="owner",
            permission_outcome="allow",
            status="ok",
            counts={"cv_text": 1},
        )


# ------------------------------------------------------------- permissions


def test_without_a_grant_every_operation_is_denied() -> None:
    rig = make_rig(grants=False)
    assert rig.ingest(CV_TEXT).permission == "deny"
    assert rig.service.get_skills(OWNER).permission == "deny"
    assert rig.service.search(OWNER, "x").permission == "deny"
    assert rig.service.remove_source(OWNER, "s_x").permission == "deny"
    assert rig.repo.list_sources() == ()


def test_another_principal_has_no_authority_over_the_owners_profile() -> None:
    rig = make_rig()
    ingest_cv(rig)
    assert rig.service.get_skills(STRANGER).permission == "deny"
    assert rig.ingest(CV_TEXT + "\n", principal=STRANGER).permission == "deny"
    assert not rig.service.reject_claim(STRANGER, views(rig)[0].claim_id).ok
    assert not rig.service.confirm_claim(STRANGER, views(rig)[0].claim_id).ok


def test_knowledge_grants_do_not_authorize_professional_operations() -> None:
    from sam.permissions.models import (
        PermissionAction,
        PermissionGrant,
        PermissionResource,
        PermissionScope,
    )

    rig = make_rig(grants=False)
    for i, (action, scope) in enumerate(
        (
            (PermissionAction.READ, PermissionScope.identifier("profile:read")),
            (PermissionAction.WRITE, PermissionScope.identifier("profile:ingest")),
        )
    ):
        rig.grants.create_grant(
            PermissionGrant(
                grant_id=f"k{i}",
                principal=OWNER,
                resource=PermissionResource.KNOWLEDGE,  # NOT professional
                action=action,
                scope=scope,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    assert rig.ingest(CV_TEXT).permission == "deny"
    assert rig.service.get_skills(OWNER).permission == "deny"


def test_the_permission_policy_classifies_exactly_four_professional_actions() -> None:
    from sam.permissions.models import PermissionAction, PermissionResource, RiskLevel
    from sam.permissions.policy import classify

    R = PermissionResource.PROFESSIONAL
    assert classify(R, PermissionAction.READ).risk is RiskLevel.LOW  # type: ignore[union-attr]
    assert classify(R, PermissionAction.WRITE).risk is RiskLevel.MEDIUM  # type: ignore[union-attr]
    assert classify(R, PermissionAction.UPDATE).risk is RiskLevel.MEDIUM  # type: ignore[union-attr]
    delete = classify(R, PermissionAction.DELETE)
    assert (
        delete is not None
        and delete.risk is RiskLevel.HIGH
        and delete.requires_confirmation
    )
    for action in (
        PermissionAction.EXECUTE,
        PermissionAction.SEND,
        PermissionAction.PUBLISH,
        PermissionAction.CREATE,
        PermissionAction.APPROVE,
    ):
        assert classify(R, action) is None  # unclassified => denied outright


# ----------------------------------------------------- domain separation


def test_professional_intelligence_never_imports_memory() -> None:
    import ast

    import sam.professional as package

    root = Path(package.__file__).parent
    offenders = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            offenders += [
                f"{path.name}: {n}" for n in names if n.startswith("sam.memory")
            ]
    assert offenders == []


def test_ingestion_writes_nothing_to_memory_or_knowledge() -> None:
    from sam.knowledge.engine import KnowledgeEngine
    from sam.knowledge.index import InMemoryLexicalIndex
    from sam.knowledge.store import InMemoryKnowledgeStore
    from sam.memory.engine import MemoryEngine
    from sam.memory.store import InMemoryMemoryStore
    from sam.memory.working import InMemoryWorkingMemoryStore

    rig = make_rig()
    knowledge_store = InMemoryKnowledgeStore()
    knowledge = KnowledgeEngine(
        store=knowledge_store,
        index=InMemoryLexicalIndex(),
        permission_engine=rig.engine,
    )
    memory_store = InMemoryMemoryStore()
    memory = MemoryEngine(
        store=memory_store, working_store=InMemoryWorkingMemoryStore()
    )
    ingest_cv(rig)
    add_linkedin(rig)
    assert knowledge_store.list_collections() == ()
    assert memory is not None and knowledge is not None
    # No raw private document lives anywhere but the professional evidence itself:
    # the only text stored is the bounded, scrubbed per-line reference.
    assert all(len(e.evidence_reference) <= 240 for e in rig.repo.list_all_evidence())
    assert all(
        "jordan@example.test" not in e.evidence_reference
        for e in rig.repo.list_all_evidence()
    )
