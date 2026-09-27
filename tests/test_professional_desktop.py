"""The desktop bridge routes for Professional Intelligence.

Owner-only, Guest-refused before any body is processed, confirmation for removal,
strict request models, and no leakage into Memory, Knowledge or the activity log.
Synthetic documents and fake providers only.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.desktop_support import HEADERS, Bridge, b64
from tests.professional_support import (
    CV_TEXT,
    LINKEDIN_AGREEING_CSV,
    PAPER_TEXT,
    TRANSCRIPT_TEXT,
)
from tests.test_desktop_identity import IdentityBridge


def ingest_body(
    text: str = CV_TEXT,
    *,
    name: str = "cv.txt",
    source_type: str = "master_cv",
    privacy: str = "personal",
    resource_type: str = "txt",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "source_type": source_type,
        "privacy_class": privacy,
        "resource_type": resource_type,
        "content_base64": b64(text.encode()),
        **extra,
    }


def profile(b: Bridge) -> dict[str, Any]:
    response = b.get("/professional/profile")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def stable(body: dict[str, Any]) -> dict[str, Any]:
    """The profile without the per-request reference id."""

    return {k: v for k, v in body.items() if k != "reference_id"}


def ingest(b: Bridge, **kwargs: Any) -> dict[str, Any]:
    response = b.post("/professional/ingest", ingest_body(**kwargs))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --------------------------------------------------------------- basics


def test_an_empty_profile_is_honest_not_prepopulated() -> None:
    b = Bridge()
    body = profile(b)
    assert body["status"] == "ok"
    assert body["claims"] == [] and body["sources"] == [] and body["counts"] == {}
    assert body["experience"]["total_months"] == 0


def test_ingest_then_the_profile_shows_provenance_and_strength() -> None:
    b = Bridge()
    result = ingest(b)
    assert result["status"] == "ok" and result["ingest_status"] == "ingested"
    assert result["claims_created"] > 10 and result["candidate_extraction"] == "off"
    body = profile(b)
    assert body["counts"]["employment"] == 2 and body["counts"]["education"] == 1
    python = next(c for c in body["claims"] if c["statement"] == "Technology: Python")
    assert python["strength"] == "single_source" and python["sensitivity"] == "personal"
    assert python["review"] == "unreviewed" and python["accepted"] is True
    evidence = python["evidence"][0]
    assert (
        evidence["source_label"] == "cv.txt" and evidence["source_type"] == "master_cv"
    )
    assert evidence["character_start"] is not None
    (source,) = body["sources"]
    assert source["privacy_class"] == "personal" and source["freshness"] == "fresh"
    assert source["accepted_claims"] > 10
    assert (
        body["experience"]["total_years"] == 7
        and body["experience"]["remainder_months"] == 7
    )


def test_corroboration_and_conflicts_show_in_the_profile() -> None:
    b = Bridge()
    ingest(b)
    ingest(
        b,
        text=LINKEDIN_AGREEING_CSV.replace("Mar 2019", "Apr 2019"),
        name="Positions.csv",
        source_type="linkedin_export",
        resource_type="csv",
    )
    body = profile(b)
    assert len(body["conflicts"]) == 1 and body["conflicts"][0]["attribute"] == "start"
    acme = next(c for c in body["claims"] if "Acme" in c["statement"])
    assert acme["strength"] == "corroborated" and acme["conflicted_attributes"] == [
        "start"
    ]
    assert body["experience"]["excluded_conflicted"] == 1
    # The owner resolves it through the review route.
    conflict = body["conflicts"][0]
    option = next(o for o in conflict["options"] if o["value"] == "2019-04")
    done = b.post(
        "/professional/review",
        {
            "action": "resolve",
            "conflict_id": conflict["conflict_id"],
            "option_id": option["option_id"],
        },
    ).json()
    assert done["status"] == "ok"
    after = profile(b)
    assert after["conflicts"][0]["resolved"] is True
    assert after["experience"]["excluded_conflicted"] == 0


def test_publications_and_education_have_first_class_views() -> None:
    b = Bridge()
    ingest(b)
    ingest(
        b,
        text=PAPER_TEXT,
        name="paper.txt",
        source_type="publication",
        privacy="public",
    )
    ingest(b, text=TRANSCRIPT_TEXT, name="transcript.txt", source_type="transcript")
    body = profile(b)
    (paper,) = body["publications"]
    assert paper["title"].startswith("Semantic Embeddings") and paper["doi"]
    assert paper["sensitivity"] == "personal"  # the CV that also lists it is PERSONAL
    assert paper["owner_contribution"] is None
    (education,) = body["education"]
    assert education["degree"] == "MSc" and education["classification"] == "Distinction"
    assert "12345678" not in str(body) and "A1234567" not in str(body)


def test_query_search_and_evidence_for() -> None:
    b = Bridge()
    ingest(b)
    found = b.post("/professional/query", {"mode": "search", "text": "FastAPI"}).json()
    assert found["status"] == "ok" and found["claims"]
    assert found["claims"][0]["evidence"]
    matched = b.post(
        "/professional/query",
        {"mode": "evidence_for", "text": "production RAG systems"},
    ).json()
    assert matched["requirement"]["status"] == "matched"
    assert matched["requirement"]["skills"][0]["attributes"]["skill_id"] == "rag"
    missing = b.post(
        "/professional/query", {"mode": "evidence_for", "text": "Rust experience"}
    ).json()
    assert missing["requirement"]["status"] == "not_supported"
    assert missing["requirement"]["unsupported_aspects"]
    unknown = b.post(
        "/professional/query", {"mode": "evidence_for", "text": "enjoys hiking"}
    ).json()
    assert unknown["requirement"]["status"] == "unknown"


# ------------------------------------------------------------ request safety


@pytest.mark.parametrize(
    "extra",
    [
        {"principal": "owner"},
        {"verification_state": "direct"},
        {"path": "/etc/passwd"},
        {"permission": "allow"},
        {"privacy_class_override": "public"},
    ],
)
def test_requests_reject_unknown_fields_such_as_principal_or_verification(
    extra: dict[str, Any],
) -> None:
    b = Bridge()
    response = b.post("/professional/ingest", ingest_body(**extra))
    assert response.status_code == 422
    assert profile(b)["sources"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("privacy_class", "secret"),
        ("privacy_class", "normal"),
        ("source_type", "arbitrary_file"),
        ("resource_type", "docx"),
        ("name", ""),
    ],
)
def test_request_values_are_closed_sets(field: str, value: str) -> None:
    b = Bridge()
    body = ingest_body()
    body[field] = value
    assert b.post("/professional/ingest", body).status_code == 422


def test_bad_encoding_and_secrets_are_rejected_with_nothing_stored() -> None:
    b = Bridge()
    bad = b.post(
        "/professional/ingest", {**ingest_body(), "content_base64": "%%%not-base64"}
    ).json()
    assert bad["status"] == "rejected" and bad["reason_code"] == "invalid_encoding"
    secret = ingest(
        b, text="SKILLS\nPython\npassword: hunter2hunter2hunter2\n", name="n.txt"
    )
    assert secret["status"] == "rejected" and secret["reason_code"] == "secret_detected"
    assert "hunter2" not in str(secret)
    assert profile(b)["sources"] == []


def test_a_path_like_name_is_rejected() -> None:
    b = Bridge()
    result = ingest(b, name="../../etc/passwd")
    assert result["status"] == "rejected" and result["reason_code"] == "invalid_source"


def test_a_duplicate_is_reported_and_idempotent() -> None:
    b = Bridge()
    ingest(b)
    again = ingest(b)
    assert again["status"] == "ok" and again["ingest_status"] == "unchanged"
    copy = ingest(b, name="copy.txt")
    assert copy["status"] == "rejected" and copy["reason_code"] == "duplicate_source"
    assert len(profile(b)["sources"]) == 1


def test_candidate_extraction_without_a_model_router_is_off_not_an_error() -> None:
    b = Bridge()  # no model router configured
    result = ingest(b, use_candidates=True)
    assert result["status"] == "ok" and result["candidate_extraction"] == "off"


# ------------------------------------------------------ removal needs a confirmation


def test_removing_a_source_asks_for_confirmation_then_succeeds() -> None:
    b = Bridge()
    ingest(b)
    source_id = profile(b)["sources"][0]["source_id"]
    first = b.post("/professional/remove", {"source_id": source_id}).json()
    assert first["status"] == "confirmation_required"
    challenge = first["challenge"]
    assert challenge["resource"] == "professional" and challenge["action"] == "delete"
    assert challenge["risk"] == "high"
    assert len(profile(b)["sources"]) == 1  # nothing removed yet
    approved = b.post(
        "/confirmations/decide",
        {"confirmation_id": challenge["confirmation_id"], "approved": True},
    ).json()
    assert approved["status"] == "approved"
    done = b.post(
        "/professional/remove",
        {"source_id": source_id, "confirmation_id": challenge["confirmation_id"]},
    ).json()
    assert done["status"] == "ok"
    body = profile(b)
    assert body["sources"] == [] and body["claims"] == []
    # A confirmation is one-time: it cannot remove anything again.
    replay = b.post(
        "/professional/remove",
        {"source_id": source_id, "confirmation_id": challenge["confirmation_id"]},
    ).json()
    assert replay["status"] != "ok"


def test_a_denied_removal_removes_nothing() -> None:
    b = Bridge()
    ingest(b)
    source_id = profile(b)["sources"][0]["source_id"]
    challenge = b.post("/professional/remove", {"source_id": source_id}).json()[
        "challenge"
    ]
    b.post(
        "/confirmations/decide",
        {"confirmation_id": challenge["confirmation_id"], "approved": False},
    )
    denied = b.post(
        "/professional/remove",
        {"source_id": source_id, "confirmation_id": challenge["confirmation_id"]},
    ).json()
    assert denied["status"] in ("denied", "failed", "rejected")
    assert len(profile(b)["sources"]) == 1


def test_removal_preserves_another_sources_corroborating_evidence() -> None:
    b = Bridge()
    ingest(b)
    ingest(
        b,
        text=LINKEDIN_AGREEING_CSV,
        name="Positions.csv",
        source_type="linkedin_export",
        resource_type="csv",
    )
    sources = {s["label"]: s["source_id"] for s in profile(b)["sources"]}
    challenge = b.post("/professional/remove", {"source_id": sources["cv.txt"]}).json()[
        "challenge"
    ]
    b.post(
        "/confirmations/decide",
        {"confirmation_id": challenge["confirmation_id"], "approved": True},
    )
    assert (
        b.post(
            "/professional/remove",
            {
                "source_id": sources["cv.txt"],
                "confirmation_id": challenge["confirmation_id"],
            },
        ).json()["status"]
        == "ok"
    )
    body = profile(b)
    acme = next(c for c in body["claims"] if "Acme" in c["statement"])
    assert acme["strength"] == "single_source"
    assert {e["source_type"] for e in acme["evidence"]} == {"linkedin_export"}


def test_removed_or_rejected_evidence_never_reappears_in_the_profile() -> None:
    b = Bridge()
    sentinel = "Pangolinwire"
    ingest(b, text=CV_TEXT.replace("Deployed on AWS", f"Deployed on AWS {sentinel}"))
    ingest(
        b,
        text=LINKEDIN_AGREEING_CSV,
        name="Positions.csv",
        source_type="linkedin_export",
        resource_type="csv",
    )
    body = profile(b)
    assert sentinel in str(body)
    python = next(c for c in body["claims"] if c["statement"].endswith(": Python"))
    rejected = b.post(
        "/professional/review", {"action": "reject", "claim_id": python["claim_id"]}
    ).json()
    assert rejected["status"] == "ok"
    cv = next(s["source_id"] for s in body["sources"] if s["label"] == "cv.txt")
    challenge = b.post("/professional/remove", {"source_id": cv}).json()["challenge"]
    b.post(
        "/confirmations/decide",
        {"confirmation_id": challenge["confirmation_id"], "approved": True},
    )
    done = b.post(
        "/professional/remove",
        {"source_id": cv, "confirmation_id": challenge["confirmation_id"]},
    ).json()
    assert done["status"] == "ok"
    after = str(profile(b))
    assert sentinel not in after and "cv.txt" not in after and cv not in after
    assert python["claim_id"] not in after
    searched = b.post("/professional/query", {"mode": "search", "text": "AWS"}).json()
    assert sentinel not in str(searched) and cv not in str(searched)


# ------------------------------------------------------------- Guest Mode

PROFESSIONAL_CALLS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("get", "/professional/profile", None),
    ("post", "/professional/ingest", ingest_body()),
    ("post", "/professional/review", {"action": "reject", "claim_id": "c_x"}),
    ("post", "/professional/review", {"action": "confirm", "claim_id": "c_x"}),
    (
        "post",
        "/professional/review",
        {"action": "set_privacy", "source_id": "s_x", "privacy_class": "public"},
    ),
    ("post", "/professional/remove", {"source_id": "s_x"}),
    ("post", "/professional/query", {"mode": "search", "text": "Python"}),
]


@pytest.mark.parametrize(("method", "path", "body"), PROFESSIONAL_CALLS)
def test_guest_mode_is_refused_on_every_professional_route(
    method: str, path: str, body: dict[str, Any] | None
) -> None:
    b = IdentityBridge(text="hello")
    ingest(b)  # the owner's profile exists before Guest Mode starts
    before = stable(profile(b))
    b.enroll()
    assert b.start_guest()["status"] == "ok"
    response = b.get(path) if method == "get" else b.post(path, body)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "guest_mode_active"
    # ... and the private profile is neither readable nor changed by the guest:
    assert "Acme" not in response.text and "Jordan" not in response.text
    b.post("/voice/guest/end", {})
    assert stable(profile(b)) == before


def test_guest_cannot_ingest_a_cv_or_change_the_profile() -> None:
    b = IdentityBridge(text="hello")
    b.enroll()
    assert b.start_guest()["status"] == "ok"
    denied = b.post("/professional/ingest", ingest_body())
    assert denied.status_code == 403
    # Refused before the service was even reached: no audit event, no source.
    assert b.runtime.professional_audit.events() == ()
    assert b.runtime.professional.get_sources(b.runtime.principal).data == ()


def test_the_owner_route_needs_the_bridge_token() -> None:
    b = Bridge()
    response = b.client.get("/desktop/v1/professional/profile")
    assert response.status_code in (401, 403)
    wrong = b.client.get(
        "/desktop/v1/professional/profile", headers={"x-sam-desktop-token": "x" * 40}
    )
    assert wrong.status_code in (401, 403)
    assert HEADERS  # the correct header is what the other tests use


# ------------------------------------------------- separation and audit


def test_professional_data_never_reaches_memory_or_knowledge() -> None:
    b = Bridge()
    ingest(b)
    ingest(
        b,
        text=PAPER_TEXT,
        name="paper.txt",
        source_type="publication",
        privacy="public",
    )
    memory_search = b.post("/memory/search", {"text": "Acme"}).json()
    assert memory_search["items"] == [] and memory_search["working"] == []
    assert b.post("/memory/search", {}).json()["items"] == []
    knowledge = b.get("/knowledge/resources").json()
    assert knowledge["resources"] == []
    query = b.post("/knowledge/query", {"query": "Acme Analytics FastAPI"}).json()
    assert query["hits"] == []


def test_the_activity_log_and_audit_hold_no_document_content() -> None:
    b = Bridge()
    ingest(b, name="My Private CV 2026.txt")
    b.post("/professional/query", {"mode": "search", "text": "salary of Acme"})
    activity = b.get("/activity").json()
    dumped = str(activity) + " ".join(
        repr(e) for e in b.runtime.professional_audit.events()
    )
    for private in (
        "Acme",
        "Jordan",
        "My Private CV",
        "Distinction",
        "FastAPI",
        "salary",
    ):
        assert private not in dumped, private
    assert any("Professional" in item["label"] for item in activity["items"])


def test_the_owner_has_exactly_the_four_professional_grants() -> None:
    b = Bridge()
    grants = b.runtime.grants.list_grants(b.runtime.principal)
    professional = sorted(
        (g.action.value, g.scope.as_text())
        for g in grants
        if g.resource.value == "professional"
    )
    assert professional == [
        ("delete", "profile"),
        ("read", "profile:read"),
        ("update", "profile:review"),
        ("write", "profile:ingest"),
    ]
    # No grant permits EXECUTE/SEND/CREATE on the resource (they are unclassified).
    assert all(
        g.action.value in ("read", "write", "update", "delete")
        for g in grants
        if g.resource.value == "professional"
    )
