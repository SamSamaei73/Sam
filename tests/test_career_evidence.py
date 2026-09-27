"""Evidence: fit analysis, CV tailoring, cover letters, research alignment and the
application claim policy. Every owner fact comes from Phase 14 evidence."""

from __future__ import annotations

from typing import Any

from sam.career.claims import ClaimChecker
from sam.career.documents import render
from sam.career.models import (
    AlignmentStatus,
    ApplicationDocument,
    ContactRole,
    DocumentKind,
    FitStatus,
    OutreachChannel,
    OutreachKind,
    SourceKind,
)
from tests.career_support import OWNER, make_rig, must


def documents(rig: Any, draft: Any) -> dict[DocumentKind, ApplicationDocument]:
    docs = [rig.service.repository.get_document(i) for i in draft.document_ids]
    return {d.kind: d for d in docs if d is not None}


def draft_for(rig: Any, opportunity: Any = None, **kwargs: Any) -> Any:
    opportunity = opportunity or rig.job()
    result = rig.service.create_draft(OWNER, opportunity.opportunity_id, **kwargs)
    assert result.ok and result.data is not None, result.reason
    return result.data


# ------------------------------------------------------------------ fit


def test_fit_analysis_classifies_every_requirement_with_evidence() -> None:
    rig = make_rig()
    job = rig.job()
    report = rig.service.fit(OWNER, job.opportunity_id).data
    assert report is not None
    by = {r.requirement: r for r in report.requirements}
    assert by["Experience with FastAPI"].status is FitStatus.SUPPORTED
    assert by["10 years of Rust experience"].status is FitStatus.NOT_SUPPORTED
    assert by["10 years of Rust experience"].claims == ()
    fastapi = by["Experience with FastAPI"]
    assert fastapi.claims and all(c.evidence_ids for c in fastapi.claims)
    assert not hasattr(report, "score")  # no arbitrary numeric fit score


def test_fit_is_unknown_when_evidence_cannot_be_read() -> None:
    rig = make_rig()
    job = rig.job()
    rig.pro.grants.revoke_grant("p-1", now=rig.clock())  # PROFESSIONAL READ
    report = rig.service.fit(OWNER, job.opportunity_id).data
    assert report is not None
    assert {r.status for r in report.requirements} == {FitStatus.UNKNOWN}


# ------------------------------------------------------------ CV tailoring


def test_an_unsupported_skill_is_never_invented() -> None:
    rig = make_rig()
    draft = draft_for(rig)
    text = "\n".join(d.text for d in documents(rig, draft).values())
    assert "Rust" not in text  # the one requirement with no evidence


def test_cv_tailoring_preserves_provenance_on_every_fact_line() -> None:
    rig = make_rig()
    cv = documents(rig, draft_for(rig))[DocumentKind.CV]
    claims = {c.claim_id: c for c in must(rig.service._evidence.claims(OWNER))}
    facts = [line for line in cv.lines if line.fact]
    assert facts and all(line.evidence for line in facts)
    for line in facts:
        for link in line.evidence:
            assert link.claim_id in claims  # a real, accepted Phase 14 claim
            assert set(link.evidence_ids) == set(claims[link.claim_id].evidence_ids)
    assert cv.unresolved == ()


def test_cv_dates_titles_and_employers_are_copied_exactly() -> None:
    rig = make_rig()
    cv = documents(rig, draft_for(rig))[DocumentKind.CV]
    assert "Senior Software Engineer, Acme Analytics (2019-03 to 2022-06)" in cv.text
    assert "Data Scientist, Globex Health (2022-07 to present)" in cv.text


def test_an_owner_edit_that_changes_a_date_is_flagged_and_blocks_approval() -> None:
    rig = make_rig()
    draft = draft_for(rig)
    cv = documents(rig, draft)[DocumentKind.CV]
    edited = [
        line.text.replace("2019-03", "2016-01") if "Acme" in line.text else line.text
        for line in cv.lines
    ]
    new = rig.service.edit_document(OWNER, cv.document_id, edited).data
    assert new is not None
    (flagged,) = new.unresolved
    assert "2016-01" in flagged.text and flagged.flag_reason == "dated_line_changed"
    assert not rig.service.approve_document(OWNER, new.document_id).ok


def test_an_invented_metric_is_flagged() -> None:
    rig = make_rig()
    draft = draft_for(rig)
    cv = documents(rig, draft)[DocumentKind.CV]
    lines = [line.text for line in cv.lines]
    lines.append("Improved model accuracy by 37%")
    lines.append("Reduced manual triage time by 40%")  # this metric IS in evidence
    new = rig.service.edit_document(OWNER, cv.document_id, lines).data
    assert new is not None
    reasons = {line.text: line.flag_reason for line in new.unresolved}
    assert reasons == {"Improved model accuracy by 37%": "metric_not_in_evidence"}


def test_reordering_and_shortening_are_allowed() -> None:
    rig = make_rig()
    cv = documents(rig, draft_for(rig))[DocumentKind.CV]
    lines = [line.text for line in reversed(cv.lines)][:8]
    new = rig.service.edit_document(OWNER, cv.document_id, lines).data
    assert new is not None and new.unresolved == ()
    assert rig.service.approve_document(OWNER, new.document_id).ok


# ------------------------------------------------------------ cover letters


def test_cover_letter_separates_facts_from_motivation() -> None:
    rig = make_rig()
    letter = documents(
        rig, draft_for(rig, motivation="I admire your work on field robots.")
    )[DocumentKind.COVER_LETTER]
    facts = [line for line in letter.lines if line.fact]
    motivation = [line for line in letter.lines if not line.fact]
    assert facts and all(line.evidence for line in facts)
    assert any("field robots" in line.text for line in motivation)
    assert letter.unresolved == ()


def test_a_factual_claim_in_motivation_needs_evidence() -> None:
    rig = make_rig()
    letter = documents(
        rig,
        draft_for(
            rig,
            motivation=(
                "I have 12 years of production Rust experience and I led a team of 40."
            ),
        ),
    )[DocumentKind.COVER_LETTER]
    (flagged,) = letter.unresolved
    assert "12 years" in flagged.text
    supported = documents(
        rig, draft_for(rig, motivation="I led a team of 4 engineers.")
    )[DocumentKind.COVER_LETTER]
    assert supported.unresolved == ()


def test_model_drafted_motivation_is_checked_like_anything_else() -> None:
    class Writer:
        def __init__(self) -> None:
            self.seen: list[Any] = []

        def write(self, opportunity: Any, skill_names: Any) -> list[str]:
            self.seen.append((opportunity.title, tuple(skill_names)))
            return [
                "I love building robots.",
                "I have 15 years of Kubernetes experience.",
            ]

    writer = Writer()
    rig = make_rig(motivation_writer=writer)
    letter = documents(rig, draft_for(rig, use_model=True))[DocumentKind.COVER_LETTER]
    assert [line.text for line in letter.unresolved] == [
        "I have 15 years of Kubernetes experience."
    ]
    # Minimal disclosure: only the role and skill NAMES, never the CV text.
    ((title, skills),) = writer.seen
    assert title == "Senior Machine Learning Engineer"
    assert all(len(s) < 40 for s in skills) and "Acme" not in " ".join(skills)


# ------------------------------------------------------------------ PhD


def test_research_alignment_uses_evidence_and_admits_no_evidence() -> None:
    rig = make_rig()
    phd = rig.phd()
    report = rig.service.alignment(OWNER, phd.opportunity_id).data
    assert report is not None
    by = {t.topic: t for t in report.topics}
    assert by["natural language processing"].status in (
        AlignmentStatus.SUPPORTED_ALIGNMENT,
        AlignmentStatus.PARTIAL_ALIGNMENT,
    )
    assert by["natural language processing"].claims
    assert by["quantum chromodynamics"].status is AlignmentStatus.NO_EVIDENCE
    assert by["quantum chromodynamics"].claims == ()


def test_phd_drafts_contain_evidence_and_owner_placeholders() -> None:
    rig = make_rig()
    draft = draft_for(rig, rig.phd())
    docs = documents(rig, draft)
    assert {DocumentKind.RESEARCH_STATEMENT, DocumentKind.PROPOSAL_OUTLINE} <= set(docs)
    proposal = docs[DocumentKind.PROPOSAL_OUTLINE]
    assert any(
        line.flag_reason == "owner_input_required" for line in proposal.unresolved
    )
    assert all(line.evidence for line in proposal.lines if line.fact)
    assert draft.state.value == "needs_owner_input"


def test_supervisor_interest_is_never_stated() -> None:
    rig = make_rig()
    checker = ClaimChecker(rig.service._evidence, rig.papers)
    for text in (
        "Your group is looking for a student like me.",
        "You would be interested in my background.",
        "You are recruiting PhD students this year.",
    ):
        assert checker.check(OWNER, text, ()).reason == "third_party_interest_not_known"


def test_a_paper_read_claim_needs_a_paper_sam_actually_ingested() -> None:
    rig = make_rig()
    checker = ClaimChecker(rig.service._evidence, rig.papers)
    text = "I read your paper on Robust Clinical Retrieval with interest."
    assert checker.check(OWNER, text, ()).reason == "paper_not_retrieved"
    rig.papers.titles.add("robust clinical retrieval with interest")
    assert not checker.check(OWNER, text, ()).flagged
    assert (
        checker.check(OWNER, "I read your paper.", ()).reason == "paper_not_retrieved"
    )


def test_an_owner_contribution_is_never_invented() -> None:
    rig = make_rig()
    checker = ClaimChecker(rig.service._evidence, rig.papers)
    claims = must(rig.service._evidence.claims(OWNER))
    result = checker.check(
        OWNER, "In my paper I designed the core graph model.", claims
    )
    assert result.flagged


def test_phd_email_facts_are_evidence_backed() -> None:
    rig = make_rig()
    phd = rig.phd()
    contact = rig.add_contact(
        name="Prof Ada Example",
        role=ContactRole.SUPERVISOR,
        organization="Example University",
        source_kind=SourceKind.UNIVERSITY_PAGE,
        url="https://www.example-university.ac.uk/people/ada",
        quote=(
            "Prof Ada Example, Example University, leads the Health NLP group. "
            "Email: ada.example@example-university.ac.uk"
        ),
        email="ada.example@example-university.ac.uk",
        research_topics=["natural language processing", "health misinformation"],
    )
    draft = rig.service.draft_outreach(
        OWNER,
        contact_id=contact.contact_id,
        kind=OutreachKind.SUPERVISOR,
        channel=OutreachChannel.EMAIL,
        opportunity_id=phd.opportunity_id,
    ).data
    assert draft is not None
    facts = [line for line in draft.lines if line.fact]
    assert facts and all(line.evidence for line in facts)
    assert "interested" not in draft.body.casefold()
    assert draft.unresolved == ()


# ------------------------------------------------------- domain separation


def test_career_never_changes_professional_facts() -> None:
    rig = make_rig()
    before = (
        rig.pro.repo.list_claims(),
        rig.pro.repo.list_all_evidence(),
        rig.pro.repo.list_rejections(),
    )
    reviews_before = {
        c.claim_id: rig.pro.repo.get_review(c.claim_id) for c in before[0]
    }
    draft = draft_for(rig, questions=["Describe your experience with Python"])
    cv = documents(rig, draft)[DocumentKind.CV]
    rig.service.edit_document(
        OWNER, cv.document_id, ["I have 30 years of COBOL experience"]
    )
    after = (
        rig.pro.repo.list_claims(),
        rig.pro.repo.list_all_evidence(),
        rig.pro.repo.list_rejections(),
    )
    assert after == before
    assert {
        c.claim_id: rig.pro.repo.get_review(c.claim_id) for c in after[0]
    } == reviews_before


def test_render_is_the_single_source_of_owner_facts() -> None:
    rig = make_rig()
    claims = must(rig.service._evidence.claims(OWNER))
    employment = [c for c in claims if c.category == "employment"]
    assert all(c.attributes["start"] in render(c) for c in employment)
