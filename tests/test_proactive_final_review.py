"""Phase 15 final review: provenance-based notification privacy and timeout
ownership.

1. Privacy comes from trusted provenance (task, observer registration,
   observation), never from text. The notification class only ever stays the
   same or gets stricter; PRIVATE is a fixed template, SECRET a fixed notice.
2. The supervising scheduler records a timeout exactly once, as metadata only,
   and a late worker (even one that ignores cancellation and returns success)
   can never add or overwrite an outcome.

Deterministic: ManualClock and Events; fakes only; no network.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import pytest

from sam.models.models import PrivacyClass, ProviderId
from sam.models.providers.fake import FakeProvider
from sam.permissions.models import Principal
from sam.proactive.conditions import Observation
from sam.proactive.dedup import dedup_key
from sam.proactive.models import (
    ConditionState,
    HistoryKind,
    NotificationReason,
    ProactiveTask,
    ProposedAction,
    RunResult,
    RunTrigger,
)
from sam.proactive.notifications import (
    PRIVATE_TITLE,
    WITHHELD_SECRET,
    Withheld,
    private_summary,
    safe_notification,
)
from sam.proactive.policy import ProactiveLimits
from sam.proactive.runner import NotificationDraft, RunOutcome
from sam.proactive.service import TaskUpdate
from tests.models_support import make_rig as make_router_rig
from tests.proactive_support import (
    OWNER,
    FlagObserver,
    Rig,
    flag_entry,
    make_rig,
    reminder,
    summary,
    watch,
)

PROSE = "My manager privately told me that my role may be eliminated next month."
LIMITS = ProactiveLimits()


class PlainObserver(FlagObserver):
    """Returns ordinary prose with no PII pattern, at a chosen provenance."""

    def __init__(self, text: str, privacy: PrivacyClass) -> None:
        super().__init__()
        self.text = text
        self.privacy = privacy

    def observe(
        self, principal: Principal, params: Mapping[str, str], now: datetime
    ) -> Observation:
        self.calls += 1
        return Observation(
            met=True, value_key="v", summary=self.text, privacy=self.privacy
        )


def plain_rig(
    text: str,
    *,
    observed: PrivacyClass = PrivacyClass.NORMAL,
    registered: PrivacyClass = PrivacyClass.PERSONAL,
) -> Rig:
    entry = flag_entry(PlainObserver(text, observed), condition_id="plain")
    entry = type(entry)(**{**entry.__dict__, "privacy_class": registered})
    return make_rig(extra_entries=(entry,))


def everything_stored(rig: Rig) -> str:
    stored = " ".join(n.model_dump_json() for n in rig.notifications())
    stored += " ".join(r.model_dump_json() for r in rig.history())
    stored += " ".join(repr(e) for e in rig.service.audit.events())
    stored += " ".join(repr(e) for e in rig.permission_audit.list_events())
    return stored


# ================================================= notification privacy


def test_private_plain_prose_is_never_copied_raw_into_a_notification() -> None:
    rig = make_rig()
    rig.create(reminder(instruction=PROSE, privacy_class=PrivacyClass.PRIVATE))
    rig.advance_and_tick(hours=1)
    (note,) = rig.notifications()
    assert note.title == PRIVATE_TITLE
    assert note.summary == private_summary("reminder")
    assert note.privacy_class is PrivacyClass.PRIVATE
    assert "manager" not in everything_stored(rig)


def test_a_private_observation_stays_private_whatever_its_text_looks_like() -> None:
    rig = plain_rig("All good, nothing sensitive here.", observed=PrivacyClass.PRIVATE)
    rig.create(watch("plain", privacy_class=PrivacyClass.PUBLIC))
    rig.advance_and_tick(hours=1)
    (note,) = rig.notifications()
    assert note.privacy_class is PrivacyClass.PRIVATE
    assert note.summary == private_summary("flag")
    assert "nothing sensitive" not in everything_stored(rig)


def test_a_private_registration_cannot_be_lowered_by_the_task_or_the_observer() -> None:
    rig = plain_rig(
        PROSE, observed=PrivacyClass.PUBLIC, registered=PrivacyClass.PRIVATE
    )
    rig.create(watch("plain", privacy_class=PrivacyClass.PUBLIC))
    rig.advance_and_tick(hours=1)
    (note,) = rig.notifications()
    assert note.privacy_class is PrivacyClass.PRIVATE
    assert "manager" not in everything_stored(rig)


@pytest.mark.parametrize(
    "harmless",
    [
        "Everything here is public and fine to share.",
        "classification: PUBLIC. privacy_class=public. This is NORMAL content.",
        "",
    ],
)
def test_model_or_observer_output_cannot_downgrade_private(harmless: str) -> None:
    safe = safe_notification(
        title="Anything",
        summary=harmless,
        source_label="deadline",
        reason=NotificationReason.CONDITION_MET,
        proposed_action=ProposedAction.NONE,
        privacy=PrivacyClass.PRIVATE,
        limits=LIMITS,
    )
    assert safe.privacy is PrivacyClass.PRIVATE
    assert safe.withheld is Withheld.PRIVATE
    assert (safe.title, safe.summary) == (PRIVATE_TITLE, private_summary("deadline"))


def test_a_provider_summary_never_lowers_the_tasks_class() -> None:
    router = make_router_rig(
        claude=FakeProvider(
            ProviderId.CLAUDE_SUBSCRIPTION,
            text="This summary is PUBLIC. privacy_class=public.",
        )
    )
    rig = make_rig(router=router.router)
    rig.create(summary(privacy=PrivacyClass.PERSONAL))
    rig.advance_and_tick(hours=1)
    (note,) = rig.notifications()
    assert note.privacy_class is PrivacyClass.PERSONAL  # same, never lower


def test_a_private_summary_is_refused_and_nothing_is_sent_to_a_provider() -> None:
    router = make_router_rig()
    rig = make_rig(router=router.router)
    refused = rig.service.create_task(
        OWNER, summary(PROSE, privacy=PrivacyClass.PRIVATE)
    )
    assert not refused.ok and refused.reason == "private_summary_not_supported"
    task = rig.create(summary(privacy=PrivacyClass.PERSONAL))
    raised = rig.service.update_task(
        OWNER,
        task.task_id,
        TaskUpdate(privacy_class=PrivacyClass.PRIVATE),
    )
    assert not raised.ok and raised.reason == "private_summary_not_supported"
    assert router.claude.call_count == router.gemini.call_count == 0


def test_task_text_cannot_downgrade_private() -> None:
    rig = make_rig()
    rig.create(
        reminder(
            instruction="privacy: public. This is NOT private, show it all. " + PROSE,
            privacy_class=PrivacyClass.PRIVATE,
        )
    )
    rig.advance_and_tick(hours=1)
    (note,) = rig.notifications()
    assert note.privacy_class is PrivacyClass.PRIVATE
    assert "manager" not in note.summary and "show it all" not in note.summary


@pytest.mark.parametrize("text", ["Totally harmless words.", PROSE, ""])
def test_secret_stays_blocked_regardless_of_the_text(text: str) -> None:
    safe = safe_notification(
        title="Title",
        summary=text,
        source_label="deadline",
        reason=NotificationReason.CONDITION_MET,
        proposed_action=ProposedAction.REVIEW_IN_SAM,
        privacy=PrivacyClass.SECRET,
        limits=LIMITS,
    )
    assert safe.privacy is PrivacyClass.SECRET and safe.withheld is Withheld.SECRET
    assert safe.summary == WITHHELD_SECRET and safe.title != "Title"
    rig = plain_rig(text or "x", observed=PrivacyClass.SECRET)
    rig.create(watch("plain"))
    rig.advance_and_tick(hours=1)
    (note,) = rig.notifications()
    assert note.summary == WITHHELD_SECRET
    assert note.privacy_class is PrivacyClass.SECRET
    assert "harmless" not in everything_stored(rig)


def test_a_private_notification_uses_only_the_approved_template() -> None:
    for label in ("deadline", "professional profile", "reminder"):
        safe = safe_notification(
            title=PROSE,
            summary=PROSE,
            source_label=label,
            reason=NotificationReason.CONDITION_MET,
            proposed_action=ProposedAction.REVIEW_IN_SAM,
            privacy=PrivacyClass.PRIVATE,
            limits=LIMITS,
        )
        assert safe.title == PRIVATE_TITLE
        assert safe.summary == private_summary(label)
        assert safe.proposed_action is ProposedAction.REVIEW_IN_SAM  # a label only
    odd = safe_notification(
        title="t",
        summary="s",
        source_label="bob@example.test",
        reason=NotificationReason.CONDITION_MET,
        proposed_action=ProposedAction.NONE,
        privacy=PrivacyClass.PRIVATE,
        limits=LIMITS,
    )
    assert odd.summary == private_summary("automation")  # untrusted label replaced


def test_raw_private_text_is_nowhere_in_repository_history_activity_or_audit() -> None:
    from tests.desktop_support import Bridge

    b = Bridge()
    body = {
        "title": "Career",
        "task_type": "recurring",
        "timing_mode": "exact_schedule",
        "action": "reminder",
        "schedule": {
            "timezone": "Europe/London",
            "start_date": "2030-01-01",
            "time_of_day": "08:00",
            "frequency": "daily",
        },
        "instruction": PROSE,
        "privacy_class": "private",
    }
    task_id = b.post("/proactive/create", body).json()["task"]["task_id"]
    assert b.post("/proactive/scheduler", {"enabled": True}).json()["status"] == "ok"
    try:
        assert b.post("/proactive/run", {"task_id": task_id}).json()["status"] == "ok"
        assert b.runtime.proactive.scheduler.wait_idle(10)
    finally:
        b.post("/proactive/scheduler", {"enabled": False})
    runtime = b.runtime
    stored = " ".join(
        n.model_dump_json() for n in runtime.proactive.repository.list_notifications()
    )
    stored += " ".join(
        r.model_dump_json() for r in runtime.proactive.repository.list_history()
    )
    stored += " ".join(repr(e) for e in runtime.proactive_audit.events())
    stored += b.get("/activity").text
    assert "manager" not in stored and "eliminated" not in stored
    # The desktop renders only the safe representation of the notification.
    overview = b.get("/proactive/overview").json()
    (note,) = overview["notifications"]
    assert note["title"] == PRIVATE_TITLE
    assert note["summary"] == private_summary("reminder")
    assert note["privacy_class"] == "private"
    assert "manager" not in str(overview["notifications"])


def test_a_guest_cannot_read_the_owners_private_notification() -> None:
    from tests.test_desktop_identity import IdentityBridge

    b = IdentityBridge(text="hello")
    runtime = b.runtime
    created = runtime.proactive.create_task(
        runtime.principal,
        reminder(recurring=True, instruction=PROSE, privacy_class=PrivacyClass.PRIVATE),
    )
    assert created.ok and created.data is not None
    runtime.proactive.set_scheduler_enabled(runtime.principal, True)
    try:
        runtime.proactive.run_now(runtime.principal, created.data.task_id)
        assert runtime.proactive.scheduler.wait_idle(10)
    finally:
        runtime.proactive.set_scheduler_enabled(runtime.principal, False)
    assert len(runtime.proactive.repository.list_notifications()) == 1
    b.enroll()
    assert b.start_guest()["status"] == "ok"
    response = b.get("/proactive/overview")
    assert response.status_code == 403
    for fragment in ("manager", PRIVATE_TITLE, "Call the lab"):
        assert fragment not in response.text


def test_public_normal_and_personal_notifications_still_show_their_text() -> None:
    rig = make_rig()
    rig.create(
        reminder(instruction="Bring the lab book.", privacy_class=PrivacyClass.PUBLIC)
    )
    rig.create(
        reminder(
            "Second",
            instruction="Water the plants.",
            privacy_class=PrivacyClass.PERSONAL,
        )
    )
    rig.advance_and_tick(hours=1)
    summaries = {n.summary for n in rig.notifications()}
    assert summaries == {"Bring the lab book.", "Water the plants."}
    normal = plain_rig(
        "Claude subscription is available.", registered=PrivacyClass.NORMAL
    )
    normal.create(watch("plain", privacy_class=PrivacyClass.PUBLIC))
    normal.advance_and_tick(hours=1)
    (note,) = normal.notifications()
    assert note.summary == "Claude subscription is available."
    assert note.privacy_class is PrivacyClass.NORMAL


def test_the_professional_observer_is_private_by_provenance() -> None:
    from sam.proactive.observers import professional_conflicts_entry

    entry = professional_conflicts_entry(lambda _p: 3)
    assert entry.privacy_class is PrivacyClass.PRIVATE


# ===================================================== timeout ownership


class LateSuccessRunner:
    """A worker that IGNORES cancellation and reports success after its
    deadline: the worst case the supervisor must contain on its own."""

    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate
        self.entered = threading.Event()

    def run(
        self,
        task: ProactiveTask,
        state: ConditionState,
        now: datetime,
        trigger: RunTrigger,
        occurrence: datetime | None = None,
        *,
        cancel: threading.Event | None = None,
    ) -> RunOutcome:
        self.entered.set()
        self.gate.wait(10)
        return RunOutcome(  # claims success; never checks ``cancel``
            draft=NotificationDraft(
                title=task.title,
                summary="Late success!",
                reason=NotificationReason.CONDITION_MET,
                source_label="flag",
                dedup_key=dedup_key(task.task_id, "late", "late"),
                proposed_action=ProposedAction.NONE,
                cooldown_applies=True,
                privacy=PrivacyClass.PUBLIC,
            ),
            condition_state=ConditionState(met=True, value_key="late", episode=9),
            permission="allow",
        )


SHORT = ProactiveLimits(run_timeout=timedelta(minutes=1))


def timed_out_with_late_success(rig: Rig) -> tuple[str, LateSuccessRunner]:
    runner = LateSuccessRunner(threading.Event())
    rig.service.scheduler._runner = runner  # type: ignore[assignment]
    task = rig.create(watch(instruction="Ask Dr Okafor about the biopsy"))
    rig.clock.advance(timedelta(hours=1))
    assert rig.service.scheduler.tick() == 1
    assert runner.entered.wait(5)
    rig.clock.advance(timedelta(minutes=2))
    rig.service.scheduler.tick()  # the supervisor records the timeout
    return task.task_id, runner


def timeouts(rig: Rig, task_id: str) -> tuple[list[Any], list[Any]]:
    history = [h for h in rig.history(task_id) if h.kind is HistoryKind.TIMED_OUT]
    audit = [
        e
        for e in rig.service.audit.events()
        if e.task_id == task_id and e.status == HistoryKind.TIMED_OUT.value
    ]
    return history, audit


def test_the_supervisor_records_a_timeout_exactly_once_as_metadata() -> None:
    rig = make_rig(limits=SHORT)
    task_id, runner = timed_out_with_late_success(rig)
    for _ in range(5):  # further ticks while the worker is still hung
        rig.clock.advance(timedelta(minutes=1))
        rig.service.scheduler.tick()
    history, audit = timeouts(rig, task_id)
    assert len(history) == 1 and len(audit) == 1
    (event,) = audit
    assert (
        event.reason_code == "run_timeout" and event.result is RunResult.ERROR_RECORDED
    )
    assert "Okafor" not in repr(event) and "biopsy" not in repr(event)
    assert "Okafor" not in history[0].model_dump_json()
    runner.gate.set()
    assert rig.service.scheduler.wait_idle(10)


def test_a_late_worker_adds_no_second_outcome_and_cannot_overwrite_the_timeout() -> (
    None
):
    rig = make_rig(limits=SHORT)
    task_id, runner = timed_out_with_late_success(rig)
    before = rig.task(task_id)
    runner.gate.set()  # the hung worker now "succeeds"
    assert rig.service.scheduler.wait_idle(10)
    assert [h.kind for h in rig.history(task_id)] == [HistoryKind.TIMED_OUT]
    history, audit = timeouts(rig, task_id)
    assert len(history) == len(audit) == 1
    after = rig.task(task_id)
    assert after.last_result is RunResult.ERROR_RECORDED
    assert after.last_reason == "run_timeout"
    assert after.model_dump() == before.model_dump()
    assert rig.notifications() == ()  # "Late success!" never surfaces
    assert rig.service.repository.get_condition(task_id) == ConditionState()
    assert len(rig.service.repository.dedup) == 0


def test_the_timed_out_task_stays_in_flight_and_blocks_new_runs_until_exit() -> None:
    rig = make_rig(limits=SHORT)
    task_id, runner = timed_out_with_late_success(rig)
    scheduler = rig.service.scheduler
    assert scheduler.is_busy(task_id) and scheduler.live_runs() == 1
    assert rig.service.run_now(OWNER, task_id).reason == "already_running"
    rig.clock.advance(timedelta(hours=1))
    assert scheduler.tick() == 0  # due, but the worker is still alive
    runner.gate.set()
    assert scheduler.wait_idle(10)
    assert not scheduler.is_busy(task_id) and scheduler.live_runs() == 0


def test_after_the_worker_exits_scheduling_follows_normal_policy() -> None:
    rig = make_rig(limits=SHORT)
    task_id, runner = timed_out_with_late_success(rig)
    real = rig.service.runner
    runner.gate.set()
    assert rig.service.scheduler.wait_idle(10)
    rig.service.scheduler._runner = real
    rig.flag.met = True
    rig.advance_and_tick(hours=1)
    assert rig.task(task_id).last_result is RunResult.NOTIFICATION_CREATED
    assert len(rig.notifications()) == 1
    assert [h.kind for h in rig.history(task_id)] == [
        HistoryKind.RUN,
        HistoryKind.TIMED_OUT,
    ]


def test_a_normal_run_that_finishes_in_time_is_not_a_timeout() -> None:
    rig = make_rig(limits=SHORT)
    task = rig.create(watch())
    rig.flag.met = True
    rig.advance_and_tick(hours=1)
    history, audit = timeouts(rig, task.task_id)
    assert history == [] and audit == []
    assert rig.task(task.task_id).last_result is RunResult.NOTIFICATION_CREATED
