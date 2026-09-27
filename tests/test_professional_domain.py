"""Professional Intelligence: the pure domain. Vocabulary, dates, timeline
arithmetic, evidence strength and the repository's source-deletion guarantees.
No documents, no provider, no network."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from sam.professional.evidence import compute_strength, is_accepted
from sam.professional.lineage import source_families
from sam.professional.models import (
    ClaimCategory,
    ClaimReviewState,
    EvidenceNature,
    EvidenceRef,
    EvidenceStrength,
    ProfessionalClaim,
    SourceLocation,
    SourceRecord,
    SourceType,
    new_claim_id,
    new_evidence_id,
    new_source_id,
)
from sam.professional.repository import IngestionBatch, InMemoryProfessionalRepository
from sam.professional.timeline import (
    Duration,
    Interval,
    dates_agree,
    find_gaps,
    normalize_date,
    reconcile_dates,
    union_months,
)
from sam.professional.vocabulary import find_mentions, normalize_skill

NOW = datetime(2026, 9, 21, tzinfo=UTC)
TODAY = date(2026, 9, 21)


# --------------------------------------------------------------- vocabulary


@pytest.mark.parametrize(
    ("alias", "skill_id"),
    [
        ("JS", "javascript"),
        ("js", "javascript"),
        ("TS", "typescript"),
        ("ReactJS", "react"),
        ("React.js", "react"),
        ("Fast API", "fastapi"),
        ("fast-api", "fastapi"),
        ("Gen AI", "generative-ai"),
        ("GenAI", "generative-ai"),
        ("LLM", "llm"),
        ("large language models", "llm"),
        ("torch", "pytorch"),
        ("PyTorch", "pytorch"),
        ("k8s", "kubernetes"),
        ("sklearn", "scikit-learn"),
    ],
)
def test_aliases_normalize_to_one_canonical_skill(alias: str, skill_id: str) -> None:
    entry = normalize_skill(alias)
    assert entry is not None and entry.skill_id == skill_id


def test_only_the_trusted_vocabulary_mints_skills() -> None:
    assert normalize_skill("Zorbnetics") is None
    assert normalize_skill("") is None
    assert find_mentions("we love Zorbnetics and quantum widgets") == []


def test_mentions_are_word_bounded_and_longest_match_wins() -> None:
    found = [m.entry.skill_id for m in find_mentions("React Native and React and Java")]
    assert found == ["react-native", "react", "java"]
    # "Java" must not be found inside "JavaScript"
    assert [m.entry.skill_id for m in find_mentions("JavaScript")] == ["javascript"]


def test_acronyms_are_case_sensitive_so_ordinary_words_never_match() -> None:
    assert find_mentions("the rag and the tf and ml") == []
    assert [m.entry.skill_id for m in find_mentions("RAG, TF and ML")] == [
        "rag",
        "tensorflow",
        "machine-learning",
    ]


# -------------------------------------------------------------------- dates


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("Mar 2019", "2019-03"),
        ("March 2019", "2019-03"),
        ("Sept 2019", "2019-09"),
        ("2019-03", "2019-03"),
        ("03/2019", "2019-03"),
        ("2019", "2019"),
        ("Present", "present"),
        ("current", "present"),
    ],
)
def test_dates_canonicalize(raw: str, canonical: str) -> None:
    assert normalize_date(raw) == canonical


@pytest.mark.parametrize("raw", ["", "soon", "13/2019", "Foo 2019", "1800", "2019-13"])
def test_unrecognizable_dates_are_none_never_guessed(raw: str) -> None:
    assert normalize_date(raw) is None


def test_date_agreement_and_reconciliation() -> None:
    assert dates_agree("2019", "2019-03")
    assert not dates_agree("2019-03", "2019-04")
    assert not dates_agree("present", "2022-06")
    assert reconcile_dates(["2019-03", "2019"]) == ("2019-03", False)
    assert reconcile_dates(["2019-03", "2019-04"]) == (None, True)
    assert reconcile_dates([]) == (None, False)


# ------------------------------------------------------------ experience math


def test_duration_is_exact_months_and_never_rounded_up() -> None:
    interval = Interval("2019-03", "2022-06")
    assert interval.months(TODAY) == 40
    duration = Duration(40)
    assert (duration.years, duration.remainder_months) == (3, 4)
    assert Duration(35).years == 2  # 2 years 11 months is never "3 years"


def test_present_end_uses_the_injected_date_not_a_model() -> None:
    assert Interval("2022-07", "present").months(date(2026, 9, 21)) == 51
    assert Interval("2022-07", "present").months(date(2026, 10, 1)) == 52


def test_overlapping_intervals_are_not_double_counted() -> None:
    a = Interval("2019-01", "2019-12")  # 12 months
    b = Interval("2019-07", "2020-06")  # overlaps 6 months
    assert union_months([a, b], TODAY) == 18
    assert union_months([b, a], TODAY) == 18  # order does not matter
    assert union_months([a, a, a], TODAY) == 12


def test_disjoint_intervals_add_and_gaps_are_reported() -> None:
    a = Interval("2019-01", "2019-12")
    b = Interval("2021-01", "2021-12")
    assert union_months([a, b], TODAY) == 24
    assert find_gaps([a, b], TODAY) == [("2020-01", "2020-12", 12)]
    assert find_gaps([a, Interval("2020-03", "2020-12")], TODAY) == []  # 2 months


def test_year_only_dates_use_the_conservative_bound() -> None:
    interval = Interval("2019", "2021")
    assert interval.approximate
    assert interval.months(TODAY, conservative=False) == 36
    assert interval.months(TODAY, conservative=True) == 14  # Dec 2019 .. Jan 2021


def test_an_inverted_interval_counts_nothing() -> None:
    assert union_months([Interval("2022-06", "2019-03")], TODAY) == 0


# ---------------------------------------------------------- evidence strength


def _ref(
    claim_id: str,
    source_id: str,
    source_type: SourceType,
    nature: EvidenceNature,
    *,
    reference: str = "x",
    basis: str | None = None,
) -> EvidenceRef:
    location = SourceLocation(character_start=0, character_end=1)
    return EvidenceRef(
        evidence_id=new_evidence_id(claim_id, source_id, nature, location, reference),
        claim_id=claim_id,
        source_id=source_id,
        source_type=source_type,
        source_location=location,
        evidence_reference=reference,
        nature=nature,
        basis_evidence_id=basis,
        created_at=NOW,
    )


# Each source its own family unless a test says otherwise.
FAMILIES = {"s1": "f1", "s2": "f2", "s3": "f3"}
_EXPLICIT = EvidenceNature.EXPLICIT_SOURCE


def test_strength_is_computed_from_documentary_evidence_only() -> None:
    cv = _ref("c", "s1", SourceType.MASTER_CV, _EXPLICIT)
    li = _ref("c", "s2", SourceType.LINKEDIN_EXPORT, _EXPLICIT)
    assert compute_strength([], FAMILIES) is EvidenceStrength.NONE
    assert compute_strength([cv], FAMILIES) is EvidenceStrength.SINGLE_SOURCE
    assert compute_strength([cv, li], FAMILIES) is EvidenceStrength.CORROBORATED


def test_two_sources_in_one_family_are_not_corroboration() -> None:
    a = _ref("c", "s1", SourceType.MASTER_CV, _EXPLICIT)
    b = _ref("c", "s2", SourceType.OWNER_DOCUMENT, _EXPLICIT)
    same = {"s1": "f1", "s2": "f1"}
    assert compute_strength([a, b], same) is EvidenceStrength.SINGLE_SOURCE


def test_inferred_and_candidate_evidence_never_verify() -> None:
    inferred = _ref("c", "s1", SourceType.GITHUB, EvidenceNature.INFERRED_RELATIONSHIP)
    candidate = _ref("c", "s2", SourceType.MASTER_CV, EvidenceNature.MODEL_CANDIDATE)
    for refs in ([inferred], [candidate], [inferred, candidate]):
        strength = compute_strength(refs, FAMILIES)
        assert strength is EvidenceStrength.NONE
        assert not is_accepted(refs, strength, ClaimReviewState.UNREVIEWED)


def test_owner_attestation_accepts_but_never_adds_documentary_strength() -> None:
    candidate = _ref("c", "s1", SourceType.MASTER_CV, EvidenceNature.MODEL_CANDIDATE)
    attested = _ref(
        "c",
        "s1",
        SourceType.MASTER_CV,
        EvidenceNature.OWNER_ATTESTATION,
        basis=candidate.evidence_id,
    )
    refs = [candidate, attested]
    assert compute_strength(refs, FAMILIES) is EvidenceStrength.NONE
    assert is_accepted(refs, EvidenceStrength.NONE, ClaimReviewState.OWNER_CONFIRMED)
    # An attestation plus ONE document is still one document: SINGLE_SOURCE.
    other = _ref("c", "s2", SourceType.LINKEDIN_EXPORT, _EXPLICIT)
    assert compute_strength([attested, other], FAMILIES) is (
        EvidenceStrength.SINGLE_SOURCE
    )


# --------------------------------------------------------------- repository


def _source(label: str, source_type: SourceType) -> SourceRecord:
    from sam.models.models import PrivacyClass

    return SourceRecord(
        source_id=new_source_id(source_type, label),
        source_type=source_type,
        label=label,
        checksum=(label * 64)[:64].replace(" ", "a"),
        independence_key=f"source:{label}",
        content_fingerprint=(label * 64)[:64],
        privacy_class=PrivacyClass.PERSONAL,
        resource_type="txt",
        segment_count=1,
        extractor_version="1",
        ingested_at=NOW,
        refreshed_at=NOW,
    )


def _claim(key: str) -> ProfessionalClaim:
    return ProfessionalClaim(
        claim_id=new_claim_id(ClaimCategory.SKILL, key),
        category=ClaimCategory.SKILL,
        canonical_statement=f"Skill: {key}",
        key=key,
        attributes={"skill_id": key, "display": key},
        created_at=NOW,
        updated_at=NOW,
    )


def _batch(source: SourceRecord, keys: list[str]) -> IngestionBatch:
    claims = tuple(_claim(k) for k in keys)
    evidence = tuple(
        _ref(c.claim_id, source.source_id, source.source_type, _EXPLICIT)
        for c in claims
    )
    return IngestionBatch(source, claims, evidence)


def test_deleting_a_source_removes_only_its_own_evidence() -> None:
    repo = InMemoryProfessionalRepository()
    cv = _source("cv", SourceType.MASTER_CV)
    li = _source("li", SourceType.LINKEDIN_EXPORT)
    repo.commit_ingestion(_batch(cv, ["python", "docker"]))
    repo.commit_ingestion(_batch(li, ["python"]))
    python_id = new_claim_id(ClaimCategory.SKILL, "python")
    docker_id = new_claim_id(ClaimCategory.SKILL, "docker")
    li_evidence = {
        e.evidence_id
        for e in repo.list_evidence(python_id)
        if e.source_id == li.source_id
    }

    affected = repo.delete_source(cv.source_id)

    # Only SURVIVING claims whose evidence changed are returned.
    assert set(affected) == {python_id}
    # python keeps LinkedIn's evidence; docker had only the CV, so it is gone.
    assert repo.get_claim(python_id) is not None
    assert repo.get_claim(docker_id) is None
    remaining = {e.evidence_id for e in repo.list_evidence(python_id)}
    assert remaining == li_evidence
    families = source_families(repo.list_sources())
    assert (
        compute_strength(repo.list_evidence(python_id), families)
        is EvidenceStrength.SINGLE_SOURCE
    )
    assert (
        repo.get_source(cv.source_id) is None
        and repo.get_source(li.source_id) is not None
    )


def test_a_failed_commit_leaves_the_previous_state_intact() -> None:
    repo = InMemoryProfessionalRepository()
    cv = _source("cv", SourceType.MASTER_CV)
    repo.commit_ingestion(_batch(cv, ["python"]))
    before = (repo.list_claims(), repo.list_all_evidence(), repo.list_sources())

    bad = _batch(cv, ["docker"])
    # evidence that points at a claim outside the batch -> inconsistent
    broken = IngestionBatch(
        bad.source,
        bad.claims,
        (
            *bad.evidence,
            _ref("c_missing", cv.source_id, cv.source_type, _EXPLICIT),
        ),
    )
    with pytest.raises(ValueError):
        repo.commit_ingestion(broken)

    assert (repo.list_claims(), repo.list_all_evidence(), repo.list_sources()) == before


def test_refreshing_a_source_replaces_its_evidence_and_drops_orphans() -> None:
    repo = InMemoryProfessionalRepository()
    cv = _source("cv", SourceType.MASTER_CV)
    repo.commit_ingestion(_batch(cv, ["python", "docker"]))
    repo.commit_ingestion(_batch(cv, ["python"]))  # docker no longer stated
    assert {c.key for c in repo.list_claims()} == {"python"}


def test_rejection_is_a_tombstone_that_removes_the_claim_and_its_evidence() -> None:
    repo = InMemoryProfessionalRepository()
    cv = _source("cv", SourceType.MASTER_CV)
    repo.commit_ingestion(_batch(cv, ["python"]))
    python_id = new_claim_id(ClaimCategory.SKILL, "python")
    repo.add_rejection(python_id)
    assert repo.get_claim(python_id) is None and repo.list_evidence(python_id) == ()
    assert repo.is_rejected(python_id) and python_id in repo.list_rejections()


def test_the_repository_is_in_memory_only() -> None:
    import inspect

    from sam.professional import repository

    source = inspect.getsource(repository)
    for forbidden in ("sqlite", "open(", "pickle", "shelve", "requests", "httpx"):
        assert forbidden not in source
