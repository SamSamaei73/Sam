"""The desktop bridge routes for the Proactive Agent (Automations).

Owner-only and Guest-refused before any body is used, strict request models,
confirmation for deletion, a notification inbox that never executes anything,
and a content-free activity log. Fakes only; no external action.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.desktop_support import Bridge
from tests.test_desktop_identity import IdentityBridge


def tomorrow() -> str:
    return (datetime.now(UTC) + timedelta(days=1)).date().isoformat()


def create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": "Call the lab",
        "task_type": "recurring",
        "timing_mode": "exact_schedule",
        "action": "reminder",
        "schedule": {
            "timezone": "Europe/London",
            "start_date": tomorrow(),
            "time_of_day": "10:00",
            "frequency": "daily",
        },
        "instruction": "Ask about the sample results.",
    }
    body.update(overrides)
    return body


def overview(b: Bridge) -> dict[str, Any]:
    response = b.get("/proactive/overview")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def create(b: Bridge, **overrides: Any) -> dict[str, Any]:
    response = b.post("/proactive/create", create_body(**overrides))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def run_now(b: Bridge, task_id: str) -> dict[str, Any]:
    """Switch scheduling on (owner route), run once, then switch it off again so
    no background loop outlives the test."""

    switched = b.post("/proactive/scheduler", {"enabled": True}).json()
    assert switched["status"] == "ok" and switched["scheduler_enabled"] is True
    try:
        body: dict[str, Any] = b.post("/proactive/run", {"task_id": task_id}).json()
        assert b.runtime.proactive.scheduler.wait_idle(10)
    finally:
        b.post("/proactive/scheduler", {"enabled": False})
    return body


# ---------------------------------------------------------------- basics


def test_an_empty_overview_is_honest_and_lists_only_trusted_conditions() -> None:
    body = overview(Bridge())
    assert body["status"] == "ok"
    assert body["tasks"] == [] and body["notifications"] == [] and body["history"] == []
    ids = {c["condition_id"] for c in body["conditions"]}
    assert {"deadline_approaching", "professional_open_conflicts"} <= ids
    assert body["limits"]["min_interval_hours"] == 1


def test_create_run_and_read_a_notification() -> None:
    b = Bridge()
    created = create(b)
    assert created["status"] == "ok", created
    task = created["task"]
    assert (
        task["enabled"]
        and task["next_run_at"]
        and task["schedule"]["timezone"] == "Europe/London"
    )
    assert run_now(b, task["task_id"])["status"] == "ok"
    body = overview(b)
    (note,) = body["notifications"]
    assert (
        note["title"] == "Call the lab"
        and note["summary"] == "Ask about the sample results."
    )
    assert note["read"] is False and note["proposed_action"] == "none"
    assert body["history"][0]["trigger"] == "manual"
    marked = b.post(
        "/proactive/notification",
        {"notification_id": note["notification_id"], "action": "read"},
    ).json()
    assert marked["status"] == "ok"
    assert overview(b)["notifications"][0]["read"] is True
    b.post(
        "/proactive/notification",
        {"notification_id": note["notification_id"], "action": "dismiss"},
    )
    assert overview(b)["notifications"] == []


def test_disable_then_enable_and_edit_the_schedule() -> None:
    b = Bridge()
    task_id = create(b)["task"]["task_id"]
    off = b.post("/proactive/update", {"task_id": task_id, "enabled": False}).json()
    assert off["status"] == "ok" and off["task"]["next_run_at"] is None
    on = b.post(
        "/proactive/update",
        {
            "task_id": task_id,
            "enabled": True,
            "schedule": {
                "timezone": "Asia/Tokyo",
                "start_date": tomorrow(),
                "time_of_day": "07:30",
                "frequency": "daily",
            },
        },
    ).json()
    assert on["status"] == "ok" and on["task"]["schedule"]["timezone"] == "Asia/Tokyo"
    assert (
        on["task"]["schedule"]["time_of_day"] == "07:30" and on["task"]["version"] == 3
    )


def test_deleting_a_task_always_needs_a_confirmation() -> None:
    b = Bridge()
    task_id = create(b)["task"]["task_id"]
    first = b.post("/proactive/delete", {"task_id": task_id}).json()
    assert first["status"] == "confirmation_required"
    challenge = first["challenge"]
    assert challenge["risk"] == "high" and challenge["resource"] == "proactive"
    assert len(overview(b)["tasks"]) == 1
    approved = b.post(
        "/confirmations/decide",
        {"confirmation_id": challenge["confirmation_id"], "approved": True},
    ).json()
    assert approved["status"] == "approved"
    done = b.post(
        "/proactive/delete",
        {"task_id": task_id, "confirmation_id": challenge["confirmation_id"]},
    ).json()
    assert done["status"] == "ok"
    assert overview(b)["tasks"] == []
    replay = b.post(
        "/proactive/delete",
        {"task_id": task_id, "confirmation_id": challenge["confirmation_id"]},
    ).json()
    assert replay["status"] != "ok"


def test_invalid_input_is_refused_with_a_safe_message() -> None:
    b = Bridge()
    too_fast = create(
        b,
        task_type="condition_watch",
        timing_mode="condition_watch",
        action="watch",
        condition_id="deadline_approaching",
        condition_params={"date": tomorrow()},
        schedule={
            "timezone": "Europe/London",
            "start_date": tomorrow(),
            "time_of_day": "10:00",
            "frequency": "hourly",
            "interval": 1,
        },
    )
    assert too_fast["status"] == "ok"  # hourly is the minimum and allowed
    bad_tz = create(b, schedule={**create_body()["schedule"], "timezone": "Mars/Base"})
    assert (
        bad_tz["status"] == "rejected" and bad_tz["reason_code"] == "invalid_timezone"
    )
    unknown = create(
        b,
        task_type="condition_watch",
        timing_mode="condition_watch",
        action="watch",
        condition_id="subprocess",
        schedule={**create_body()["schedule"], "frequency": "hourly"},
    )
    assert unknown["reason_code"] == "unknown_condition"
    secret = create(b, instruction="password = hunter2hunter2hunter2")
    assert secret["reason_code"] == "secret_detected"
    assert "hunter2" not in str(secret)
    assert len(overview(b)["tasks"]) == 1


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/proactive/create", create_body(principal="owner")),
        ("/proactive/create", create_body(confirmation_id="c1")),
        ("/proactive/create", create_body(provider="openai_api")),
        ("/proactive/create", create_body(command="rm -rf /")),
        ("/proactive/create", create_body(code="import os")),
        ("/proactive/create", create_body(allow_paid_fallback=True)),
        (
            "/proactive/create",
            create_body(schedule={**create_body()["schedule"], "cron": "* * * * *"}),
        ),
        (
            "/proactive/create",
            create_body(
                schedule={**create_body()["schedule"], "frequency": "minutely"}
            ),
        ),
        ("/proactive/create", create_body(privacy_class="secret")),
        ("/proactive/update", {"task_id": "t1", "status": "active"}),
        ("/proactive/update", {"task_id": "t1", "next_run_at": "2026-01-01T00:00:00Z"}),
        ("/proactive/delete", {"task_id": "../etc"}),
        ("/proactive/run", {"task_id": "t1", "confirmation_id": "c1"}),
        ("/proactive/notification", {"notification_id": "n1", "action": "execute"}),
        ("/proactive/notification", {"notification_id": "n1", "action": "approve"}),
    ],
)
def test_requests_reject_unknown_fields_and_values(
    path: str, body: dict[str, Any]
) -> None:
    b = Bridge()
    response = b.post(path, body)
    assert response.status_code == 422
    assert b.runtime.proactive.repository.list_tasks() == ()


def test_revoking_the_execute_grant_stops_runs() -> None:
    b = Bridge()
    task_id = create(b)["task"]["task_id"]
    grants = b.get("/permissions").json()["grants"]
    execute = next(
        g["grant_id"]
        for g in grants
        if g["resource"] == "proactive" and g["action"] == "execute"
    )
    assert b.post("/permissions/revoke", {"grant_id": execute}).json()["status"] == "ok"
    refused = b.post("/proactive/run", {"task_id": task_id}).json()
    assert refused["status"] == "denied"
    assert overview(b)["notifications"] == []


def test_scheduling_is_off_on_a_fresh_desktop_and_run_now_is_refused() -> None:
    b = Bridge()
    assert overview(b)["scheduler_enabled"] is False
    task_id = create(b)["task"]["task_id"]
    refused = b.post("/proactive/run", {"task_id": task_id}).json()
    assert refused["status"] == "rejected"
    assert refused["reason_code"] == "scheduler_disabled"
    assert overview(b)["history"] == [] and overview(b)["notifications"] == []
    grants_before = b.get("/permissions").json()["grants"]
    on = b.post("/proactive/scheduler", {"enabled": True}).json()
    try:
        assert on["status"] == "ok" and on["scheduler_enabled"] is True
        assert overview(b)["scheduler_enabled"] is True
        assert b.get("/permissions").json()["grants"] == grants_before
    finally:
        off = b.post("/proactive/scheduler", {"enabled": False}).json()
    assert off["scheduler_enabled"] is False
    assert not b.runtime.proactive.scheduler.running
    bad = b.post("/proactive/scheduler", {"enabled": True, "grant": "gmail:send"})
    assert bad.status_code == 422


# ------------------------------------------------------------ Guest Mode


PROACTIVE_CALLS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("get", "/proactive/overview", None),
    ("post", "/proactive/create", create_body()),
    ("post", "/proactive/update", {"task_id": "t1", "enabled": False}),
    ("post", "/proactive/delete", {"task_id": "t1"}),
    ("post", "/proactive/run", {"task_id": "t1"}),
    ("post", "/proactive/notification", {"notification_id": "n1", "action": "read"}),
    ("post", "/proactive/scheduler", {"enabled": True}),
]


@pytest.mark.parametrize(("method", "path", "body"), PROACTIVE_CALLS)
def test_guest_mode_is_refused_on_every_proactive_route(
    method: str, path: str, body: dict[str, Any] | None
) -> None:
    b = IdentityBridge(text="hello")
    task_id = create(b, title="Private: divorce lawyer call")["task"]["task_id"]
    run_now(b, task_id)
    before = overview(b)
    b.enroll()
    assert b.start_guest()["status"] == "ok"
    events_before = len(b.runtime.proactive_audit.events())
    if body is not None and "task_id" in body:
        body = {**body, "task_id": task_id}
    response = b.get(path) if method == "get" else b.post(path, body)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "guest_mode_active"
    assert "divorce" not in response.text and "sample results" not in response.text
    # Refused before the service was reached: nothing audited, nothing changed.
    assert len(b.runtime.proactive_audit.events()) == events_before
    b.post("/voice/guest/end", {})
    after = overview(b)
    assert [t["task_id"] for t in after["tasks"]] == [
        t["task_id"] for t in before["tasks"]
    ]
    assert after["tasks"][0]["enabled"] is True
    assert len(after["notifications"]) == len(before["notifications"])


def test_guest_cannot_create_a_task() -> None:
    b = IdentityBridge(text="hello")
    b.enroll()
    assert b.start_guest()["status"] == "ok"
    denied = b.post("/proactive/create", create_body())
    assert denied.status_code == 403
    assert b.runtime.proactive.repository.list_tasks() == ()
    assert b.runtime.proactive_audit.events() == ()


# ------------------------------------------------- separation and audit


def test_the_activity_log_and_audit_hold_no_task_text() -> None:
    b = Bridge()
    task_id = create(
        b, title="Falcon deal with Dr Okafor", instruction="Flat at 12 Baker Street"
    )["task"]["task_id"]
    run_now(b, task_id)
    activity = b.get("/activity").json()
    dumped = str(activity) + " ".join(
        repr(e) for e in b.runtime.proactive_audit.events()
    )
    for private in ("Falcon", "Okafor", "Baker"):
        assert private not in dumped


def test_a_suggested_action_is_never_executed_by_the_inbox() -> None:
    b = Bridge()
    task_id = create(b, proposed_action="review_in_sam")["task"]["task_id"]
    run_now(b, task_id)
    (note,) = overview(b)["notifications"]
    assert note["proposed_action"] == "review_in_sam"
    # The only notification actions are read and dismiss.
    response = b.post(
        "/proactive/notification",
        {"notification_id": note["notification_id"], "action": "execute"},
    )
    assert response.status_code == 422
