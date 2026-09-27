"""True source independence: CORROBORATED needs independent source FAMILIES.

Two versions of one CV, a renamed copy, or the same text under another file name
or source type are one family and never corroborate each other. Families are
decided by deterministic code only (no model, no embedding) and are recomputed
from the current sources on every read.

Synthetic documents only; no provider, no network.
"""

from __future__ import annotations

import inspect
import random
from datetime import UTC, datetime

from sam.models.models import PrivacyClass
from sam.professional import lineage
from sam.professional.lineage import fingerprints, independence_key, source_families
from sam.professional.models import (
    ClaimCategory,
    EvidenceStrength,
    SourceRecord,
    SourceType,
    new_source_id,
)
from sam.professional.profile import ClaimView
from tests.professional_support import (
    CV_TEXT,
    LINKEDIN_AGREEING_CSV,
    OWNER,
    README_TEXT,
    Rig,
    ingest_cv,
    make_rig,
)

NOW = datetime(2026, 9, 21, tzinfo=UTC)
CV_V2 = CV_TEXT.replace("Kubernetes", "Kubernetes, Terraform").replace(
    "Machine learning engineer.", "Machine learning engineer focused on NLP."
)


def _views(rig: Rig) -> list[ClaimView]:
    data = rig.service.get_profile(OWNER).data
    assert data is not None
    return list(data.claims)


def _skill(rig: Rig, name: str) -> ClaimView:
    return next(v for v in _views(rig) if v.statement.endswith(f": {name}"))


def _job(rig: Rig) -> ClaimView:
    return next(
        v
        for v in _views(rig)
        if v.category is ClaimCategory.EMPLOYMENT and "Acme" in v.statement
    )


def _source(
    label: str,
    source_type: SourceType,
    text: str,
) -> SourceRecord:
    source_id = new_source_id(source_type, label)
    content, lines = fingerprints([text])
    return SourceRecord(
        source_id=source_id,
        source_type=source_type,
        label=label,
        checksum=content,
        independence_key=independence_key(source_type, source_id),
        content_fingerprint=content,
        line_fingerprints=lines,
        privacy_class=PrivacyClass.PERSONAL,
        resource_type="txt",
        segment_count=1,
        extractor_version="1",
        ingested_at=NOW,
        refreshed_at=NOW,
    )


# ------------------------------------------------------ same family: no


def test_two_versions_of_the_same_cv_do_not_corroborate() -> None:
    rig = make_rig()
    ingest_cv(rig, name="cv_2023.txt")
    assert rig.ingest(CV_V2, name="cv_2024.txt").ok
    assert len(rig.repo.list_sources()) == 2  # two files ...
    python = _skill(rig, "Python")
    assert python.source_count == 2
    assert python.source_family_count == 1  # ... one family
    assert python.strength is EvidenceStrength.SINGLE_SOURCE
    assert _job(rig).strength is EvidenceStrength.SINGLE_SOURCE


def test_a_renamed_duplicate_is_the_same_family() -> None:
    rig = make_rig()
    ingest_cv(rig, name="cv.txt")
    # The same CV, re-saved under another name and type, with trivial
    # whitespace/case differences (so the raw bytes differ).
    renamed = CV_TEXT.replace("\n", "\r\n").upper()
    assert rig.ingest(
        renamed, name="old_resume.txt", source_type=SourceType.OWNER_DOCUMENT
    ).ok
    python = _skill(rig, "Python")
    assert python.source_count == 2 and python.source_family_count == 1
    assert python.strength is EvidenceStrength.SINGLE_SOURCE


def test_identical_bytes_under_another_file_name_are_refused_as_a_duplicate() -> None:
    rig = make_rig()
    ingest_cv(rig, name="cv.txt")
    dup = rig.ingest(
        CV_TEXT, name="copy_of_cv.txt", source_type=SourceType.OWNER_DOCUMENT
    )
    assert not dup.ok and dup.reason == "duplicate_source"
    assert _skill(rig, "Python").strength is EvidenceStrength.SINGLE_SOURCE


def test_a_lightly_edited_copy_is_the_same_family() -> None:
    rig = make_rig()
    ingest_cv(rig, name="cv.txt")
    edited = CV_TEXT.replace(
        "Led a team of 4 engineers", "Led a team of five engineers"
    )
    assert rig.ingest(
        edited, name="cv_copy.md", source_type=SourceType.PROJECT_DOCUMENTATION
    ).ok
    python = _skill(rig, "Python")
    assert python.source_count == 2  # the copy DOES contribute evidence ...
    assert python.source_family_count == 1  # ... but it is not independent
    assert python.strength is EvidenceStrength.SINGLE_SOURCE


def test_source_type_alone_cannot_create_corroboration() -> None:
    # Same content, three different source types: still one family.
    records = [
        _source("cv.txt", SourceType.MASTER_CV, CV_TEXT),
        _source("cv.md", SourceType.OWNER_DOCUMENT, CV_TEXT),
        _source("cv.pdf", SourceType.PROJECT_DOCUMENTATION, CV_TEXT),
    ]
    families = source_families(records)
    assert len(set(families.values())) == 1


def test_every_master_cv_is_one_logical_document_whatever_its_content() -> None:
    a = _source("a.txt", SourceType.MASTER_CV, "Alpha resume text line one here")
    b = _source("b.txt", SourceType.MASTER_CV, "Completely different content now")
    families = source_families([a, b])
    assert families[a.source_id] == families[b.source_id]


# -------------------------------------------------- independent families


def test_two_independent_families_corroborate() -> None:
    rig = make_rig()
    ingest_cv(rig)
    assert rig.ingest(
        LINKEDIN_AGREEING_CSV,
        name="Positions.csv",
        source_type=SourceType.LINKEDIN_EXPORT,
        resource_type="csv",
    ).ok
    job = _job(rig)
    assert job.source_family_count == 2
    assert job.strength is EvidenceStrength.CORROBORATED
    assert rig.ingest(
        README_TEXT,
        name="README.md",
        source_type=SourceType.GITHUB,
        resource_type="markdown",
    ).ok
    assert _skill(rig, "Python").strength is EvidenceStrength.CORROBORATED


def test_deleting_one_independent_source_recomputes_to_single_source() -> None:
    rig = make_rig()
    ingest_cv(rig)
    assert rig.ingest(
        README_TEXT,
        name="README.md",
        source_type=SourceType.GITHUB,
        resource_type="markdown",
    ).ok
    assert _skill(rig, "Python").strength is EvidenceStrength.CORROBORATED
    readme = rig.source_id("README.md", SourceType.GITHUB)
    first = rig.service.remove_source(OWNER, readme)
    assert first.confirmation_id
    rig.approve(first.confirmation_id)
    assert rig.service.remove_source(OWNER, readme, first.confirmation_id).ok
    python = _skill(rig, "Python")
    assert python.strength is EvidenceStrength.SINGLE_SOURCE
    assert python.source_family_count == 1


def test_families_are_recomputed_when_a_linking_copy_is_removed() -> None:
    first = [f"First document statement number {i} about the work" for i in range(6)]
    second = [f"Second document statement number {i} about a project" for i in range(6)]
    a = _source("a.txt", SourceType.OWNER_DOCUMENT, "\n".join(first))
    c = _source("c.txt", SourceType.PROJECT_DOCUMENTATION, "\n".join(second))
    # b copies BOTH a and c, so it links them into one family ...
    b = _source("b.txt", SourceType.OWNER_DOCUMENT, "\n".join(first + second))
    linked = source_families([a, b, c])
    assert len(set(linked.values())) == 1
    # ... and once b is removed, a and c are independent again.
    unlinked = source_families([a, c])
    assert unlinked[a.source_id] != unlinked[c.source_id]


def test_families_do_not_depend_on_ingestion_order() -> None:
    records = [
        _source("cv.txt", SourceType.MASTER_CV, CV_TEXT),
        _source("cv2.txt", SourceType.MASTER_CV, CV_V2),
        _source("copy.md", SourceType.OWNER_DOCUMENT, CV_TEXT.upper()),
        _source("readme.md", SourceType.GITHUB, README_TEXT),
    ]
    expected = source_families(records)
    for seed in range(5):
        shuffled = list(records)
        random.Random(seed).shuffle(shuffled)
        assert source_families(shuffled) == expected


def test_independence_is_decided_by_code_never_a_model() -> None:
    source = inspect.getsource(lineage)
    imports = [line for line in source.splitlines() if "import" in line]
    assert imports and all("sam.models" not in line for line in imports)
    for forbidden in ("router", "provider", "ModelRequest", "numpy", "vector"):
        assert forbidden not in source


def test_the_source_view_reports_family_and_version() -> None:
    rig = make_rig()
    ingest_cv(rig, name="cv_2023.txt")
    assert rig.ingest(CV_V2, name="cv_2024.txt").ok
    data = rig.service.get_sources(OWNER).data
    assert data is not None and len(data) == 2
    assert len({s.source_family_id for s in data}) == 1
    assert {s.version for s in data} == {1}
