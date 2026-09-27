"""Opportunities: explicit-only facts, deterministic dedup, canonical sources,
domain safety and untrusted listing text. Synthetic fixtures only."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from sam.career.dedup import same_opportunity
from sam.career.models import (
    OpportunityStatus,
    OpportunityType,
    SourceKind,
    Sponsorship,
    WorkMode,
)
from sam.career.opportunities import ListingRejected, normalize_listing, parse_date
from sam.career.sources import (
    UnsafeURL,
    priority,
    registrable_domain,
    safe_host,
    same_host,
)
from tests.career_support import (
    APPLY_URL,
    INJECTION,
    JOB_LINKEDIN,
    JOB_OFFICIAL,
    JOB_SILENT,
    OWNER,
    PHD_LISTING,
    PHD_NO_FUNDING,
    make_rig,
)


def test_explicit_facts_are_extracted_and_nothing_else() -> None:
    rig = make_rig(with_cv=False)
    job = rig.job()
    assert (job.title, job.organization, job.location) == (
        "Senior Machine Learning Engineer",
        "Nimbus Robotics",
        "London, UK",
    )
    assert job.work_mode is WorkMode.HYBRID
    assert job.compensation == "GBP 80,000 - 95,000"
    assert job.deadline == date(2026, 10, 30)
    assert job.application_url == APPLY_URL
    assert job.sponsorship is Sponsorship.UNKNOWN  # the listing is silent
    assert [r.text for r in job.requirements if not r.preferred] == [
        "Production experience with Python",
        "Experience with FastAPI",
        "Kubernetes in production",
        "10 years of Rust experience",
    ]
    assert [r.text for r in job.requirements if r.preferred] == [
        "Published research in NLP"
    ]
    assert job.status is OpportunityStatus.ACTIVE


def test_unknown_values_stay_unknown_and_are_never_invented() -> None:
    rig = make_rig(with_cv=False)
    job = rig.job(JOB_SILENT, url="https://quiet.example/jobs/1")
    assert job.compensation is None and job.deadline is None
    assert job.work_mode is WorkMode.UNKNOWN
    assert job.sponsorship is Sponsorship.UNKNOWN
    assert job.application_url is None and job.funding is None


def test_phd_funding_is_only_what_the_page_states() -> None:
    rig = make_rig(with_cv=False)
    phd = rig.phd()
    assert phd.type is OpportunityType.PHD
    assert phd.funding and "stipend of GBP 19,237" in phd.funding
    assert phd.deadline == date(2026, 11, 15)
    assert "health misinformation" in phd.research_topics
    vague = rig.phd(PHD_NO_FUNDING)
    assert vague.funding is None  # "funding may be available" is not funding


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We can offer visa sponsorship for this role.", Sponsorship.AVAILABLE),
        ("Visa sponsorship is available.", Sponsorship.AVAILABLE),
        ("We are unable to offer visa sponsorship.", Sponsorship.NOT_AVAILABLE),
        ("Sponsorship is not available.", Sponsorship.NOT_AVAILABLE),
        ("Great benefits and a friendly team.", Sponsorship.UNKNOWN),
        (
            "Visa sponsorship is available. We cannot sponsor visas.",
            Sponsorship.UNKNOWN,
        ),
    ],
)
def test_sponsorship_is_never_inferred(text: str, expected: Sponsorship) -> None:
    rig = make_rig(with_cv=False)
    job = rig.job(JOB_SILENT + "\n" + text, url="https://quiet.example/jobs/2")
    assert job.sponsorship is expected


def test_deadlines_are_parsed_deterministically_or_left_unknown() -> None:
    assert parse_date("Closing date: 30 October 2026") == date(2026, 10, 30)
    assert parse_date("October 30, 2026") == date(2026, 10, 30)
    assert parse_date("2026-10-30") == date(2026, 10, 30)
    assert parse_date("2026-10-30 or 2026-11-02") is None  # ambiguous
    assert parse_date("end of the month") is None
    assert parse_date("2026-02-30") is None


def test_a_passed_deadline_or_a_closed_notice_closes_the_opportunity() -> None:
    rig = make_rig(with_cv=False)
    past = rig.job(
        JOB_SILENT + "\nClosing date: 2026-01-01\n", url="https://quiet.example/jobs/3"
    )
    assert past.status is OpportunityStatus.CLOSED
    closed = rig.job(
        JOB_SILENT.replace("Data Engineer", "Analyst")
        + "\nThis position has closed.\n",
        url="https://quiet.example/jobs/4",
    )
    assert closed.status is OpportunityStatus.CLOSED


def test_the_same_job_on_several_sites_is_deduplicated_deterministically() -> None:
    rig = make_rig(with_cv=False)
    linkedin = rig.job(
        JOB_LINKEDIN,
        kind=SourceKind.LINKEDIN,
        url="https://www.linkedin.com/jobs/view/123",
    )
    official = rig.job()
    assert official.opportunity_id == linkedin.opportunity_id
    (stored,) = rig.service.repository.list_opportunities()
    assert {s.kind for s in stored.sources} == {
        SourceKind.LINKEDIN,
        SourceKind.OFFICIAL_CAREER_PAGE,
    }
    # The official page becomes canonical and its facts win.
    assert stored.canonical_source is SourceKind.OFFICIAL_CAREER_PAGE
    assert stored.application_url == APPLY_URL and stored.compensation is not None
    # A later third-party sighting never overwrites the official facts.
    rig.job(
        JOB_LINKEDIN, kind=SourceKind.INDEED, url="https://uk.indeed.com/viewjob?jk=9"
    )
    (again,) = rig.service.repository.list_opportunities()
    assert again.canonical_source is SourceKind.OFFICIAL_CAREER_PAGE
    assert again.compensation == "GBP 80,000 - 95,000" and len(again.sources) == 3


def test_dedup_uses_external_id_and_description_but_not_a_model() -> None:
    now = datetime(2026, 9, 21, tzinfo=UTC)
    rig = make_rig(with_cv=False)
    a = normalize_listing(rig.listing(JOB_OFFICIAL, external_id="ML-123"), now)
    renamed = JOB_OFFICIAL.replace(
        "Senior Machine Learning Engineer", "ML Engineer III"
    )
    b = normalize_listing(
        rig.listing(
            renamed,
            external_id="ML-123",
            url="https://careers.nimbus-robotics.example/jobs/other",
        ),
        now,
    )
    assert same_opportunity(a, b)  # same external id at the same domain
    other = normalize_listing(
        rig.listing(JOB_SILENT, url="https://quiet.example/jobs/9"), now
    )
    assert not same_opportunity(a, other)
    phd = normalize_listing(
        rig.listing(
            PHD_LISTING,
            kind=SourceKind.UNIVERSITY_PAGE,
            url="https://www.example-university.ac.uk/x",
            opportunity_type=OpportunityType.PHD,
        ),
        now,
    )
    assert not same_opportunity(a, phd)


def test_source_priority_prefers_official_sources() -> None:
    job_order = [
        SourceKind.OFFICIAL_CAREER_PAGE,
        SourceKind.OFFICIAL_ATS,
        SourceKind.LINKEDIN,
        SourceKind.INDEED,
        SourceKind.GLASSDOOR,
    ]
    assert [priority(k, OpportunityType.JOB) for k in job_order] == [0, 1, 2, 3, 4]
    phd_order = [
        SourceKind.UNIVERSITY_PAGE,
        SourceKind.FUNDING_PAGE,
        SourceKind.SUPERVISOR_PAGE,
        SourceKind.ACADEMIC_SOURCE,
    ]
    assert [priority(k, OpportunityType.PHD) for k in phd_order] == [0, 1, 2, 3]


def test_only_an_official_source_can_define_where_to_apply() -> None:
    rig = make_rig(with_cv=False)
    board = rig.job(
        JOB_SILENT + "\nApply: https://evil-apply.example/steal\n",
        kind=SourceKind.OTHER,
        url="https://jobs-aggregator.example/1",
    )
    assert board.application_url is None


@pytest.mark.parametrize(
    "url",
    [
        "http://careers.nimbus-robotics.example/apply",
        "https://careers.nimbus-robotics.example@evil.example/apply",
        "https://xn--nmbus-robotics-8kb.example/apply",
        "https://192.168.0.10/apply",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://",
    ],
)
def test_unsafe_urls_are_refused(url: str) -> None:
    with pytest.raises(UnsafeURL):
        safe_host(url)


def test_lookalike_and_subdomain_tricks_are_not_the_same_host() -> None:
    assert not same_host(
        "https://careers.nimbus-robotics.example.evil.io/apply", APPLY_URL
    )
    assert not same_host("https://careers-nimbus-robotics.example/apply", APPLY_URL)
    assert same_host("https://careers.nimbus-robotics.example/apply/other", APPLY_URL)
    assert (
        registrable_domain("www.example-university.ac.uk") == "example-university.ac.uk"
    )


def test_listing_text_is_inert_data_and_cannot_direct_sam() -> None:
    rig = make_rig()
    before = len(rig.pro.permission_audit.list_events())  # the fixture's CV ingest
    job = rig.job(INJECTION, kind=SourceKind.OTHER, url="https://evil.example/jobs/1")
    assert job.application_url is None
    assert rig.service.repository.list_drafts() == ()  # nothing was triggered
    draft = rig.service.create_draft(OWNER, job.opportunity_id).data
    assert draft is not None and draft.state.value != "submitted"
    assert rig.submitter.packages == [] and rig.email.sent == []
    events = rig.pro.permission_audit.list_events()[before:]
    requested = {(e.resource.value, e.action.value) for e in events}
    assert requested <= {("career", "create"), ("professional", "read")}


def test_an_unusable_listing_is_rejected() -> None:
    rig = make_rig(with_cv=False)
    for text in ("", "just some words\nwith no labels"):
        result = rig.service.import_listing(OWNER, rig.listing(text))
        assert not result.ok
    with pytest.raises(ListingRejected):
        normalize_listing(
            rig.listing(JOB_OFFICIAL, url="http://insecure.example/x"),
            datetime.now(UTC),
        )


def test_a_secret_in_listing_text_is_never_stored() -> None:
    rig = make_rig(with_cv=False)
    key = "sk-" + "ant-" + "api03-" + "Z" * 40
    job = rig.job(
        JOB_SILENT + f"\nAPI key: {key}\n", url="https://quiet.example/jobs/5"
    )
    assert key not in job.description and "ZZZZZZ" not in job.model_dump_json()
