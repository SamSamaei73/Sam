"""Career desktop routes: owner-only (Guest refused before the body is used),
strict request models, review-first behaviour and content-free activity.
Synthetic fixtures; no submission adapter or e-mail tool is configured."""

from __future__ import annotations

from typing import Any

import pytest

from tests.career_support import JOB_OFFICIAL, OFFICIAL_URL
from tests.desktop_support import Bridge
from tests.test_desktop_identity import IdentityBridge


def import_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "action": "import",
        "text": JOB_OFFICIAL,
        "url": OFFICIAL_URL,
        "source_kind": "official_career_page",
        "opportunity_type": "job",
    }
    body.update(overrides)
    return body


def overview(b: Bridge) -> dict[str, Any]:
    response = b.get("/career/overview")
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()
    return data


def test_an_empty_overview_is_honest_and_review_first() -> None:
    body = overview(Bridge())
    assert body["status"] == "ok"
    assert body["opportunities"] == [] and body["applications"] == []
    assert body["submission_available"] is False and body["sending_available"] is False


def test_import_fit_and_draft_through_the_bridge() -> None:
    b = Bridge()
    imported = b.post("/career/opportunity", import_body()).json()
    assert imported["status"] == "ok"
    opportunity_id = imported["item_id"]
    (item,) = overview(b)["opportunities"]
    assert (
        item["compensation"] == "GBP 80,000 - 95,000"
        and item["sponsorship"] == "unknown"
    )
    assert item["has_official_application_url"] is True
    fit = b.post("/career/fit", {"opportunity_id": opportunity_id}).json()
    assert fit["status"] == "ok" and len(fit["requirements"]) == 5
    drafted = b.post(
        "/career/draft",
        {
            "action": "create",
            "opportunity_id": opportunity_id,
            "questions": ["What are your salary expectations?"],
        },
    ).json()
    assert drafted["status"] == "ok" and drafted["state"] == "needs_owner_input"
    (application,) = overview(b)["applications"]
    (question,) = application["questions"]
    assert (
        question["classification"] == "owner_review_required"
        and not question["answered"]
    )
    queue = overview(b)["review_queue"]
    assert any(i["kind"] == "needs_answer" for i in queue)


def test_submission_and_sending_fail_closed_in_the_desktop_runtime() -> None:
    b = Bridge()
    grants = b.get("/permissions").json()["grants"]
    career = {(g["action"]) for g in grants if g["resource"] == "career"}
    assert career == {"read", "create", "update", "delete"}  # no SUBMIT / SEND
    opportunity_id = b.post("/career/opportunity", import_body()).json()["item_id"]
    draft_id = b.post(
        "/career/draft", {"action": "create", "opportunity_id": opportunity_id}
    ).json()["item_id"]
    submitted = b.post("/career/submit", {"draft_id": draft_id}).json()
    assert submitted["status"] != "ok"
    assert overview(b)["applications"][0]["state"] != "submitted"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/career/opportunity", import_body(principal="owner")),
        ("/career/opportunity", import_body(path="/Users/owner/cv.pdf")),
        ("/career/draft", {"action": "mark_submitted", "draft_id": "ap_1"}),
        (
            "/career/draft",
            {"action": "create", "opportunity_id": "op_1", "state": "submitted"},
        ),
        (
            "/career/draft",
            {
                "action": "create",
                "opportunity_id": "op_1",
                "file_path": "~/.ssh/id_rsa",
            },
        ),
        ("/career/submit", {"draft_id": "ap_1", "force": True}),
        ("/career/submit", {"draft_id": "ap_1", "url": "https://evil.example/apply"}),
        ("/career/submit", {"draft_id": "ap_1", "destination": "https://evil.example"}),
        ("/career/submit", {"draft_id": "ap_1", "attempt_state": "submitted"}),
        ("/career/send", {"outreach_id": "or_1", "recipient": "x@evil.example"}),
        ("/career/send", {"outreach_id": "or_1", "to": "boss@example.test"}),
        ("/career/outreach", {"action": "send", "outreach_id": "or_1"}),
        (
            "/career/contact",
            {
                "name": "A B",
                "role": "ceo",
                "organization": "X",
                "source_kind": "other",
                "url": "https://x.example",
                "quote": "A B at X",
            },
        ),
        ("/career/preferences", {"permissions": ["career:submit"]}),
        ("/career/fit", {"opportunity_id": "../../etc"}),
    ],
)
def test_requests_reject_unknown_fields_and_values(
    path: str, body: dict[str, Any]
) -> None:
    b = Bridge()
    assert b.post(path, body).status_code == 422
    assert b.runtime.career.repository.list_opportunities() == ()


CAREER_CALLS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("get", "/career/overview", None),
    ("post", "/career/opportunity", import_body()),
    ("post", "/career/fit", {"opportunity_id": "op_1"}),
    ("post", "/career/draft", {"action": "create", "opportunity_id": "op_1"}),
    ("post", "/career/submit", {"draft_id": "ap_1"}),
    (
        "post",
        "/career/contact",
        {
            "name": "A B",
            "role": "recruiter",
            "organization": "X",
            "source_kind": "other",
            "url": "https://x.example",
            "quote": "A B at X",
        },
    ),
    ("post", "/career/outreach", {"action": "approve", "outreach_id": "or_1"}),
    ("post", "/career/send", {"outreach_id": "or_1"}),
    ("post", "/career/preferences", {"salary_preference": "GBP 1"}),
]


@pytest.mark.parametrize(("method", "path", "body"), CAREER_CALLS)
def test_guest_mode_is_refused_on_every_career_route(
    method: str, path: str, body: dict[str, Any] | None
) -> None:
    b = IdentityBridge(text="hello")
    opportunity_id = b.post("/career/opportunity", import_body()).json()["item_id"]
    b.post(
        "/career/draft",
        {
            "action": "create",
            "opportunity_id": opportunity_id,
            "questions": ["Salary expectations?"],
        },
    )
    before = overview(b)
    b.enroll()
    assert b.start_guest()["status"] == "ok"
    events = len(b.runtime.career_audit.events())
    response = b.get(path) if method == "get" else b.post(path, body)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "guest_mode_active"
    assert "Nimbus" not in response.text and "Salary" not in response.text
    assert len(b.runtime.career_audit.events()) == events  # refused before the service
    b.post("/voice/guest/end", {})
    after = overview(b)
    assert len(after["opportunities"]) == len(before["opportunities"])
    assert [a["version"] for a in after["applications"]] == [
        a["version"] for a in before["applications"]
    ]


def test_the_activity_log_holds_no_application_content() -> None:
    b = Bridge()
    opportunity_id = b.post("/career/opportunity", import_body()).json()["item_id"]
    draft_id = b.post(
        "/career/draft",
        {
            "action": "create",
            "opportunity_id": opportunity_id,
            "questions": ["Salary expectations?"],
        },
    ).json()["item_id"]
    b.post(
        "/career/draft",
        {
            "action": "answer",
            "draft_id": draft_id,
            "question_id": "q1",
            "text": "GBP 97,531",
        },
    )
    b.post(
        "/career/preferences",
        {"salary_preference": "GBP 97,531", "needs_sponsorship": True},
    )
    activity = b.get("/activity").text
    audit = " ".join(repr(e) for e in b.runtime.career_audit.events())
    for private in ("97,531", "Nimbus", "Acme", "retrieval systems"):
        assert private not in activity and private not in audit
