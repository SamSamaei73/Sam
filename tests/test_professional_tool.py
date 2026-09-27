"""The model-facing boundary is READ ONLY, path-free and privacy-preserving."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sam.models.models import PrivacyClass
from sam.professional.models import SourceType
from sam.professional.tool import READ_OPERATIONS, ProfessionalReadTool
from tests.professional_support import (
    OWNER,
    PAPER_TEXT,
    Rig,
    ingest_cv,
    make_rig,
)


def make_tool(rig: Rig) -> ProfessionalReadTool:
    return ProfessionalReadTool(rig.service, OWNER)


def snapshot(rig: Rig) -> tuple[Any, ...]:
    return (
        rig.repo.list_claims(),
        [e.evidence_id for e in rig.repo.list_all_evidence()],
        rig.repo.list_sources(),
        rig.repo.list_rejections(),
        rig.repo.list_resolutions(),
    )


def test_the_tool_exposes_only_read_operations() -> None:
    schema = make_tool(make_rig()).input_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["operation"]["enum"] == list(READ_OPERATIONS)
    for write in (
        "ingest_source",
        "confirm_claim",
        "reject_claim",
        "resolve_conflict",
        "remove_source",
        "set_source_privacy",
        "ingest_local_file",
    ):
        assert write not in READ_OPERATIONS
        assert not hasattr(ProfessionalReadTool, write)


@pytest.mark.parametrize(
    "operation",
    [
        "remove_source",
        "confirm_claim",
        "reject_claim",
        "resolve_conflict",
        "ingest_source",
        "set_source_privacy",
        "delete",
        "verify",
        "__init__",
        "_search",
    ],
)
def test_a_model_cannot_delete_verify_or_change_evidence(operation: str) -> None:
    rig = make_rig()
    ingest_cv(rig)
    before = snapshot(rig)
    tool = make_tool(rig)
    with pytest.raises(ValueError):
        tool.execute({"operation": operation, "source_id": "x", "claim_id": "y"})
    with pytest.raises(ValueError):
        tool.execute({"operation": operation})
    assert snapshot(rig) == before


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"operation": "search", "path": "/etc/passwd"},
        {"operation": "search", "query": "x", "principal": "owner"},
        {"operation": "search", "query": "x", "sql": "select * from claims"},
        {"operation": "search", "query": 5},
        {"operation": "get_skills", "query": ["a"]},
    ],
)
def test_the_tool_takes_no_path_sql_principal_or_non_text_arguments(
    arguments: dict[str, Any],
) -> None:
    tool = make_tool(make_rig())
    with pytest.raises(ValueError):
        tool.execute(arguments)


def test_a_path_in_a_query_is_only_text_and_reads_no_file() -> None:
    rig = make_rig()
    ingest_cv(rig)
    out = make_tool(rig).execute(
        {"operation": "search", "query": "/etc/passwd ../../secret"}
    )
    assert out["ok"] is True and out["items"] == []


def test_reads_return_evidence_backed_claims_with_provenance() -> None:
    rig = make_rig()
    ingest_cv(rig)
    out = make_tool(rig).execute({"operation": "get_skills"})
    assert out["ok"] is True
    python = next(i for i in out["items"] if i["attributes"].get("display") == "Python")
    assert python["evidence_strength"] == "single_source"
    assert python["review_state"] == "unreviewed" and python["accepted"] is True
    assert python["provenance"][0]["source_label"] == "cv.txt"
    assert python["provenance"][0]["source_type"] == "master_cv"
    # Where evidence came from, never the raw evidence text.
    assert "evidence_reference" not in json.dumps(out)
    assert out["max_sensitivity"] == "personal"


def test_private_claims_are_withheld_from_the_model_entirely() -> None:
    rig = make_rig()
    text = "OBJECTIVE\nSalary expectation is 90k and needs visa sponsorship.\nSKILLS\nPython\n"  # noqa: E501
    assert rig.ingest(text, name="notes.txt", privacy=PrivacyClass.PERSONAL).ok
    tool = make_tool(rig)
    # The salary/visa claims are PRIVATE by cue; the skill is only PERSONAL.
    everything = json.dumps(
        [
            tool.execute({"operation": op})
            for op in ("get_skills", "get_projects", "get_education")
        ]
        + [tool.execute({"operation": "search", "query": "salary visa sponsorship"})]
    )
    assert "90k" not in everything and "sponsorship" not in everything
    searched = tool.execute({"operation": "search", "query": "salary expectation visa"})
    assert searched["withheld_private"] >= 1 and searched["items"] == []


def test_a_wholly_private_source_shows_nothing_but_a_count() -> None:
    rig = make_rig()
    ingest_cv(rig, privacy=PrivacyClass.PRIVATE)
    tool = make_tool(rig)
    skills = tool.execute({"operation": "get_skills"})
    assert skills["items"] == [] and skills["withheld_private"] > 0
    education = tool.execute({"operation": "get_education"})
    assert education["items"] == [] and education["withheld_private"] == 1
    timeline = tool.execute({"operation": "get_timeline"})
    assert timeline["entries"] == [] and timeline["total_years"] == 0
    assert "Acme" not in json.dumps(timeline) and "Globex" not in json.dumps(timeline)
    experience = tool.execute({"operation": "experience_for", "skill": "FastAPI"})
    assert (
        experience["verified"] is False and experience["reason"] == "withheld_private"
    )
    gaps = tool.execute({"operation": "get_profile_gaps"})
    assert all(
        "FastAPI" not in g["detail"] and "Acme" not in g["detail"]
        for g in gaps["items"]
    )
    salary = tool.execute(
        {"operation": "assess_claim", "statement": "I earn a salary of 90k"}
    )
    assert all(f["supported"] is None for f in salary["findings"])
    # ... and the requirement matcher does not leak them either.
    match = tool.execute({"operation": "evidence_for", "requirement": "production RAG"})
    assert match["matched"] == [] and match["withheld_private"] > 0


def test_publications_and_education_are_shaped_and_bounded() -> None:
    rig = make_rig()
    ingest_cv(rig)
    assert rig.ingest(
        PAPER_TEXT,
        name="paper.txt",
        source_type=SourceType.PUBLICATION,
        privacy=PrivacyClass.PUBLIC,
    ).ok
    tool = make_tool(rig)
    pubs = tool.execute({"operation": "get_publications"})
    (paper,) = pubs["items"]
    assert paper["owner_author_position"] == 1
    assert paper["owner_contribution_stated"] is False
    assert paper["doi"] == "10.1234/example.2023.001"
    edu = tool.execute({"operation": "get_education"})
    assert edu["items"][0]["degree"] == "MSc"


def test_timeline_experience_and_claim_checks_are_deterministic() -> None:
    rig = make_rig()
    ingest_cv(rig)
    tool = make_tool(rig)
    first = tool.execute({"operation": "get_timeline"})
    assert first == tool.execute({"operation": "get_timeline"})
    assert (first["total_years"], first["total_remainder_months"]) == (7, 7)
    exp = tool.execute({"operation": "experience_for", "skill": "FastAPI"})
    assert (
        exp["verified"] is True and exp["years"] == 3 and exp["remainder_months"] == 4
    )
    claim = tool.execute(
        {"operation": "assess_claim", "statement": "I have 10 years of Python"}
    )
    assert claim["verdict"] == "unsupported"
    gaps = tool.execute({"operation": "get_profile_gaps"})
    assert gaps["ok"] is True and isinstance(gaps["items"], list)


def test_a_denied_principal_gets_a_refusal_not_data() -> None:
    rig = make_rig(grants=False)
    out = make_tool(rig).execute({"operation": "get_skills"})
    assert out == {"ok": False, "reason": "permission_denied"}
