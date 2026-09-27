"""Condition watches: the deterministic state machine, deduplication, cooldown
and the built-in observers. Sam decides; no model is involved."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sam.models.models import Availability, ProviderId
from sam.permissions.models import PermissionAction, PermissionResource
from sam.proactive.conditions import Observation, transition
from sam.proactive.dedup import DedupLedger, dedup_key
from sam.proactive.models import (
    ChangeKind,
    ConditionState,
    HistoryKind,
    NotificationLevel,
    NotificationReason,
    ProposedAction,
    RunFailure,
    RunResult,
    TriggerSemantics,
)
from sam.proactive.observers import (
    professional_conflicts_entry,
    provider_status_entry,
)
from tests.proactive_support import OWNER, make_rig, watch

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def obs(met: bool | None, value: str = "v1") -> Observation:
    return Observation(met=met, value_key=value)


# ------------------------------------------------------- state machine


@pytest.mark.parametrize(
    ("before", "after", "change", "candidate"),
    [
        (False, False, ChangeKind.NO_CHANGE, False),
        (False, True, ChangeKind.CONDITION_MET, True),
        (True, True, ChangeKind.NO_CHANGE, False),
        (True, False, ChangeKind.CONDITION_CLEARED, False),
    ],
)
def test_becomes_true_transitions(
    before: bool, after: bool, change: ChangeKind, candidate: bool
) -> None:
    previous = ConditionState(met=before, value_key="v1", observed_at=T0)
    step = transition(previous, obs(after), TriggerSemantics.BECOMES_TRUE, T0)
    assert (step.change, step.candidate) == (change, candidate)
    assert step.state.met is after


def test_met_again_opens_a_new_episode() -> None:
    state = ConditionState()
    episodes = []
    for value in (False, True, True, False, True):
        state = transition(state, obs(value), TriggerSemantics.BECOMES_TRUE, T0).state
        episodes.append(state.episode)
    assert episodes == [0, 1, 1, 1, 2]


def test_errors_and_unknowns_never_change_state() -> None:
    previous = ConditionState(met=True, value_key="v1", episode=3, observed_at=T0)
    failed = transition(
        previous,
        Observation(met=None, failure=RunFailure.SOURCE_UNAVAILABLE),
        TriggerSemantics.BECOMES_TRUE,
        T0,
    )
    unknown = transition(previous, obs(None), TriggerSemantics.BECOMES_TRUE, T0)
    assert failed.change is ChangeKind.ERROR and unknown.change is ChangeKind.UNKNOWN
    assert failed.state == previous == unknown.state
    assert not failed.candidate and not unknown.candidate


def test_on_change_takes_a_baseline_then_reports_value_changes() -> None:
    state = ConditionState()
    first = transition(state, obs(True, "available"), TriggerSemantics.ON_CHANGE, T0)
    assert first.change is ChangeKind.NO_CHANGE and not first.candidate
    same = transition(
        first.state, obs(True, "available"), TriggerSemantics.ON_CHANGE, T0
    )
    assert same.change is ChangeKind.NO_CHANGE
    changed = transition(
        same.state, obs(False, "rate_limited"), TriggerSemantics.ON_CHANGE, T0
    )
    assert changed.change is ChangeKind.CHANGED and changed.candidate
    assert changed.reason is NotificationReason.CONDITION_CHANGED


# --------------------------------------------- end to end notification counts


def test_the_canonical_sequence_notifies_exactly_when_it_should() -> None:
    """false -> false -> true -> true -> false -> true (after cooldown)."""

    rig = make_rig()
    task = rig.create(watch(cooldown_hours=1))
    changes = []
    for met in (False, False, True, True, False, True):
        rig.flag.met = met
        rig.advance_and_tick(hours=1)
        changes.append(rig.history(task.task_id)[0].change)
    assert changes == [
        ChangeKind.NO_CHANGE,
        ChangeKind.NO_CHANGE,
        ChangeKind.CONDITION_MET,
        ChangeKind.NO_CHANGE,
        ChangeKind.CONDITION_CLEARED,
        ChangeKind.CONDITION_MET,
    ]
    notes = rig.notifications()
    assert len(notes) == 2
    assert {n.reason_code for n in notes} == {NotificationReason.CONDITION_MET}


def test_a_repeated_identical_observation_is_not_renotified() -> None:
    rig = make_rig()
    rig.create(watch(cooldown_hours=1))
    rig.flag.met = True
    for _ in range(10):
        rig.advance_and_tick(hours=1)
    assert len(rig.notifications()) == 1


def test_met_again_inside_the_cooldown_is_suppressed_then_allowed_after_it() -> None:
    rig = make_rig()
    task = rig.create(watch(cooldown_hours=24))
    for met in (True, False, True):  # three hourly checks: met, cleared, met again
        rig.flag.met = met
        rig.advance_and_tick(hours=1)
    assert len(rig.notifications()) == 1
    assert rig.task(task.task_id).last_reason == "cooldown"
    rig.flag.met = False
    rig.advance_and_tick(hours=24)
    rig.flag.met = True
    rig.advance_and_tick(hours=1)
    assert len(rig.notifications()) == 2


def test_repeat_while_true_is_deduplicated_by_observed_value() -> None:
    rig = make_rig()
    task = rig.create(
        watch(semantics=TriggerSemantics.REPEAT_WHILE_TRUE, cooldown_hours=1)
    )
    rig.flag.met, rig.flag.value = True, "3 open items"
    for _ in range(5):  # the cooldown passes each hour; the value never changes
        rig.advance_and_tick(hours=1)
    assert len(rig.notifications()) == 1
    assert rig.task(task.task_id).last_reason == "duplicate"
    rig.flag.value = "4 open items"
    rig.advance_and_tick(hours=1)
    assert len(rig.notifications()) == 2
    assert rig.notifications()[0].reason_code is NotificationReason.CONDITION_STILL_TRUE


def test_a_silent_watch_tracks_state_but_never_notifies() -> None:
    rig = make_rig()
    task = rig.create(watch(level=NotificationLevel.SILENT))
    rig.flag.met = True
    rig.advance_and_tick(hours=1)
    assert rig.notifications() == ()
    assert rig.service.repository.get_condition(task.task_id).met is True
    assert rig.task(task.task_id).last_reason == "silent"


def test_state_persists_for_the_repository_lifetime_and_goes_with_the_task() -> None:
    rig = make_rig()
    task = rig.create(watch())
    rig.flag.met = True
    rig.advance_and_tick(hours=1)
    state = rig.service.repository.get_condition(task.task_id)
    assert state.met and state.episode == 1 and state.last_notified_at == rig.clock()
    rig.advance_and_tick(hours=1)
    assert rig.service.repository.get_condition(task.task_id).episode == 1
    rig.service.repository.delete_task(task.task_id)
    assert rig.service.repository.get_condition(task.task_id) == ConditionState()


def test_a_failed_observation_records_an_error_and_no_notification() -> None:
    rig = make_rig()
    task = rig.create(watch())
    rig.flag.met, rig.flag.fail = True, True
    rig.advance_and_tick(hours=1)
    assert rig.notifications() == ()
    done = rig.task(task.task_id)
    assert done.last_result is RunResult.ERROR_RECORDED
    assert done.last_failure is RunFailure.SOURCE_UNAVAILABLE
    assert rig.service.repository.get_condition(task.task_id).met is False
    rig.flag.fail = False
    rig.advance_and_tick(hours=1)
    assert len(rig.notifications()) == 1  # the real change is still reported


def test_a_proposed_action_is_a_label_not_an_execution() -> None:
    rig = make_rig()
    task = rig.create(watch(proposed=ProposedAction.REVIEW_IN_SAM))
    rig.flag.met = True
    rig.advance_and_tick(hours=1)
    assert rig.task(task.task_id).last_result is RunResult.ACTION_PROPOSED
    (note,) = rig.notifications()
    assert note.proposed_action is ProposedAction.REVIEW_IN_SAM
    # Nothing but PROACTIVE rows was ever asked of the PermissionEngine.
    assert {e.resource for e in rig.permission_audit.list_events()} == {
        PermissionResource.PROACTIVE
    }


# ------------------------------------------------------------- dedup unit


def test_the_dedup_ledger_is_windowed_and_bounded() -> None:
    ledger = DedupLedger(window=timedelta(hours=2), max_keys=3)
    key = dedup_key("t1", "event", "state")
    assert key == dedup_key("t1", "event", "state") != dedup_key("t1", "event", "x")
    ledger.record(key, T0)
    assert ledger.is_duplicate(key, T0 + timedelta(hours=1))
    assert not ledger.is_duplicate(key, T0 + timedelta(hours=3))
    for i in range(10):
        ledger.record(dedup_key("t", "e", str(i)), T0)
    assert len(ledger) == 3


# ------------------------------------------------------- built-in observers


def test_a_deadline_watch_notifies_inside_its_lead_time() -> None:
    rig = make_rig()
    task = rig.create(
        watch(
            "deadline_approaching",
            params={
                "date": "2026-01-07",
                "time": "12:00",
                "timezone": "Europe/London",
                "lead_hours": "24",
            },
            proposed=ProposedAction.REVIEW_DEADLINE,
        )
    )
    rig.advance_and_tick(hours=1)  # Jan 5 10:00: more than a day away
    assert rig.notifications() == ()
    rig.clock.set(datetime(2026, 1, 6, 12, 30, tzinfo=UTC))
    rig.tick()
    (note,) = rig.notifications()
    assert note.source_label == "deadline" and "Deadline in about 23" in note.summary
    rig.clock.set(datetime(2026, 1, 7, 13, 0, tzinfo=UTC))
    rig.tick()
    assert rig.history(task.task_id)[0].change is ChangeKind.CONDITION_CLEARED


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"date": "tomorrow"},
        {"date": "2026-01-07", "lead_hours": "0"},
        {"date": "2026-01-07", "lead_hours": "-5"},
        {"date": "2026-01-07", "timezone": "Mars/Base"},
        {"date": "2026-01-07", "command": "rm -rf /"},
    ],
)
def test_a_deadline_watch_with_bad_parameters_is_refused(
    params: dict[str, str],
) -> None:
    rig = make_rig()
    result = rig.service.create_task(
        OWNER, watch("deadline_approaching", params=params)
    )
    assert not result.ok and result.reason == "condition_params_invalid"


def test_a_provider_status_watch_reports_changes_without_calling_a_model() -> None:
    states = {ProviderId.CLAUDE_SUBSCRIPTION: Availability.AVAILABLE}
    entry = provider_status_entry(lambda provider: states[provider])
    rig = make_rig(extra_entries=(entry,))
    rig.create(
        watch(
            "model_provider_status",
            params={"provider": "claude_subscription"},
            semantics=TriggerSemantics.ON_CHANGE,
            cooldown_hours=1,
        )
    )
    rig.advance_and_tick(hours=1)  # baseline
    assert rig.notifications() == ()
    states[ProviderId.CLAUDE_SUBSCRIPTION] = Availability.USAGE_LIMIT
    rig.advance_and_tick(hours=1)
    (note,) = rig.notifications()
    assert note.reason_code is NotificationReason.CONDITION_CHANGED
    assert "usage limit" in note.summary
    bad = rig.service.create_task(
        OWNER, watch("model_provider_status", params={"provider": "openai_api"})
    )
    assert not bad.ok and bad.reason == "condition_params_invalid"


def test_a_professional_watch_needs_the_professional_read_permission_every_run() -> (
    None
):
    counts = {"n": 2}
    reads: list[str] = []

    def count(principal: object) -> int:
        reads.append("read")
        return counts["n"]

    rig = make_rig(extra_entries=(professional_conflicts_entry(count),))
    task = rig.create(watch("professional_open_conflicts"))
    rig.advance_and_tick(hours=1)
    assert rig.task(task.task_id).last_failure is RunFailure.PERMISSION_DENIED
    assert reads == []  # nothing was read without PROFESSIONAL/READ
    rig.grant(PermissionResource.PROFESSIONAL, PermissionAction.READ, "profile:read")
    rig.advance_and_tick(hours=1)
    assert reads == ["read"] and len(rig.notifications()) == 1
    assert rig.notifications()[0].proposed_action is ProposedAction.NONE
    rig.revoke(PermissionResource.PROFESSIONAL, PermissionAction.READ)
    rig.advance_and_tick(hours=1)
    assert reads == ["read"]
    assert rig.task(task.task_id).last_failure is RunFailure.PERMISSION_DENIED
    assert HistoryKind.RUN in [h.kind for h in rig.history(task.task_id)]
