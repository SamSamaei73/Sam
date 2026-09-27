"""Scheduling and time: deterministic, explicit timezones, DST-safe, bounded.

Every test uses a ManualClock; nothing sleeps for real time.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from sam.proactive.models import (
    Daypart,
    Frequency,
    HistoryKind,
    NotificationLevel,
    NotificationReason,
    RunResult,
    Schedule,
    TaskStatus,
    TaskType,
    TimingMode,
)
from sam.proactive.policy import ProactiveLimits
from sam.proactive.schedule import ScheduleError, next_occurrence, validate_schedule
from sam.proactive.service import TaskUpdate
from tests.proactive_support import (
    OWNER,
    make_rig,
    reminder,
    schedule,
    watch,
)

LONDON = ZoneInfo("Europe/London")


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ------------------------------------------------------------- one-time


def test_a_one_time_reminder_fires_once_at_its_local_time() -> None:
    rig = make_rig()
    task = rig.create(reminder())
    assert task.next_run_at == utc(2026, 1, 5, 10, 0)  # 10:00 London = GMT in January
    assert rig.advance_and_tick(minutes=59) == 0
    assert rig.notifications() == ()
    assert rig.advance_and_tick(minutes=1) == 1
    (note,) = rig.notifications()
    assert (
        note.title == "Call the lab"
        and note.reason_code is NotificationReason.REMINDER_DUE
    )
    done = rig.task(task.task_id)
    assert done.status is TaskStatus.COMPLETED and not done.enabled
    assert done.next_run_at is None
    assert rig.advance_and_tick(days=3) == 0
    assert len(rig.notifications()) == 1


def test_a_one_time_reminder_in_the_past_is_refused() -> None:
    rig = make_rig()
    result = rig.service.create_task(
        OWNER,
        reminder(sched=schedule(at=time(8, 0))),  # 08:00 today already passed
    )
    assert not result.ok and result.reason == "no_future_run"
    assert rig.service.repository.list_tasks() == ()


# ------------------------------------------------------------ recurring


def test_an_exact_daily_task_runs_once_per_day_at_the_same_local_time() -> None:
    rig = make_rig()
    task = rig.create(reminder(recurring=True))
    rig.advance_and_tick(hours=1)
    assert rig.task(task.task_id).next_run_at == utc(2026, 1, 6, 10, 0)
    for _ in range(24):  # tick every hour for a day: exactly one more run
        rig.advance_and_tick(hours=1)
    runs = [h for h in rig.history(task.task_id) if h.kind is HistoryKind.RUN]
    assert len(runs) == 2 and len(rig.notifications()) == 2
    assert rig.task(task.task_id).status is TaskStatus.ACTIVE


def test_a_weekly_schedule_uses_only_its_weekdays() -> None:
    sched = schedule(frequency=Frequency.WEEKLY, weekdays=(0, 2))  # Mon and Wed
    after = utc(2026, 1, 5, 9, 0)
    first = next_occurrence(sched, after)
    second = next_occurrence(sched, first) if first else None
    third = next_occurrence(sched, second) if second else None
    assert (first, second, third) == (
        utc(2026, 1, 5, 10, 0),
        utc(2026, 1, 7, 10, 0),
        utc(2026, 1, 12, 10, 0),
    )


def test_a_flexible_task_runs_anywhere_in_its_window_and_skips_a_passed_window() -> (
    None
):
    rig = make_rig()
    task = rig.create(reminder(recurring=True, flexible=True))
    assert task.next_run_at == utc(2026, 1, 6, 8, 0)  # morning window opens 08:00
    rig.clock.set(utc(2026, 1, 6, 10, 30))  # late, but still inside the morning
    rig.tick()
    assert len(rig.notifications()) == 1
    assert rig.task(task.task_id).next_run_at == utc(2026, 1, 7, 8, 0)
    rig.clock.set(utc(2026, 1, 7, 12, 30))  # the window has closed: skip, not run
    rig.tick()
    assert len(rig.notifications()) == 1
    kinds = [h.kind for h in rig.history(task.task_id)]
    assert HistoryKind.SKIPPED_MISSED in kinds
    assert rig.task(task.task_id).next_run_at == utc(2026, 1, 8, 8, 0)


# ------------------------------------------------------------------ DST


def test_dst_forward_a_nonexistent_local_time_runs_once_shifted_forward() -> None:
    sched = schedule(
        start_date=date(2026, 3, 27), at=time(1, 30), frequency=Frequency.DAILY
    )
    on_28 = next_occurrence(sched, utc(2026, 3, 28, 0, 0))
    on_29 = next_occurrence(sched, on_28) if on_28 else None
    on_30 = next_occurrence(sched, on_29) if on_29 else None
    assert on_28 == utc(2026, 3, 28, 1, 30)  # 01:30 GMT
    # 01:30 does not exist on 29 March (01:00 -> 02:00): it runs at 02:30 BST.
    assert on_29 == utc(2026, 3, 29, 1, 30)
    assert on_29 is not None and on_29.astimezone(LONDON).time() == time(2, 30)
    assert on_30 == utc(2026, 3, 30, 0, 30)  # 01:30 BST

    rig = make_rig(start=utc(2026, 3, 28, 0, 0))
    task = rig.create(
        reminder(
            recurring=True,
            sched=schedule(
                start_date=date(2026, 3, 28), at=time(1, 30), frequency=Frequency.DAILY
            ),
        )
    )
    for _ in range(3 * 48):  # every 30 minutes for three days
        rig.advance_and_tick(minutes=30)
    runs = [h for h in rig.history(task.task_id) if h.kind is HistoryKind.RUN]
    assert len(runs) == 3  # one per local day, the gap day included


def test_dst_backward_an_ambiguous_local_time_runs_only_once() -> None:
    sched = schedule(
        start_date=date(2026, 10, 24), at=time(1, 30), frequency=Frequency.DAILY
    )
    first = next_occurrence(sched, utc(2026, 10, 25, 0, 0))
    assert first == utc(2026, 10, 25, 0, 30)  # the FIRST 01:30 (BST)
    after = next_occurrence(sched, first) if first else None
    assert after == utc(2026, 10, 26, 1, 30)  # the repeated 01:30 GMT is not used

    rig = make_rig(start=utc(2026, 10, 24, 0, 0))
    task = rig.create(
        reminder(
            recurring=True,
            sched=schedule(
                start_date=date(2026, 10, 24), at=time(1, 30), frequency=Frequency.DAILY
            ),
        )
    )
    for _ in range(3 * 48):
        rig.advance_and_tick(minutes=30)
    runs = [h for h in rig.history(task.task_id) if h.kind is HistoryKind.RUN]
    assert len(runs) == 3


def test_hourly_schedules_keep_exact_elapsed_hours_across_dst() -> None:
    sched = schedule(
        start_date=date(2026, 3, 29), at=time(0, 0), frequency=Frequency.HOURLY
    )
    points = [utc(2026, 3, 28, 23, 0)]
    for _ in range(4):
        nxt = next_occurrence(sched, points[-1])
        assert nxt is not None
        points.append(nxt)
    gaps = {b - a for a, b in zip(points[1:], points[2:], strict=False)}
    assert gaps == {timedelta(hours=1)}
    assert [p.astimezone(LONDON).hour for p in points[1:]] == [0, 2, 3, 4]


def test_changing_the_timezone_keeps_the_wall_clock_time() -> None:
    rig = make_rig()
    task = rig.create(reminder(recurring=True))
    moved = rig.service.update_task(
        OWNER,
        task.task_id,
        TaskUpdate(schedule=schedule(tz="Asia/Tokyo", frequency=Frequency.DAILY)),
    )
    assert moved.ok and moved.data is not None
    # 10:00 in Tokyo (UTC+9) after 18:00 JST today is tomorrow 01:00 UTC.
    assert moved.data.next_run_at == utc(2026, 1, 6, 1, 0)
    assert moved.data.schedule.time_of_day == time(10, 0)
    assert moved.data.version == task.version + 1


# ------------------------------------------------------ missed / overdue


def test_an_overdue_one_time_task_is_marked_missed_not_run_late() -> None:
    rig = make_rig()
    task = rig.create(reminder())
    rig.advance_and_tick(hours=4)  # 3 hours after 10:00, beyond the 1 h grace
    done = rig.task(task.task_id)
    assert done.status is TaskStatus.MISSED and not done.enabled
    (note,) = rig.notifications()
    assert note.reason_code is NotificationReason.REMINDER_MISSED
    assert [h.kind for h in rig.history(task.task_id)] == [HistoryKind.MISSED]


def test_an_overdue_silent_one_time_task_creates_no_notification() -> None:
    rig = make_rig()
    task = rig.create(reminder(notification_level=NotificationLevel.SILENT))
    rig.advance_and_tick(hours=4)
    assert rig.task(task.task_id).status is TaskStatus.MISSED
    assert rig.notifications() == ()


def test_a_slightly_late_exact_task_still_runs_once() -> None:
    rig = make_rig()
    task = rig.create(reminder())
    rig.advance_and_tick(hours=1, minutes=30)  # 30 min late, inside the grace
    assert rig.task(task.task_id).last_result is RunResult.NOTIFICATION_CREATED


def test_missed_recurring_runs_skip_to_next_without_replaying_history() -> None:
    rig = make_rig()
    task = rig.create(reminder(recurring=True))
    rig.clock.advance(timedelta(days=5 * 365))  # five years of missed days
    dispatched = rig.tick()
    assert dispatched == 0 and rig.notifications() == ()
    records = rig.history(task.task_id)
    assert [r.kind for r in records] == [HistoryKind.SKIPPED_MISSED]
    upcoming = rig.task(task.task_id).next_run_at
    assert upcoming is not None and rig.clock() < upcoming <= rig.clock() + timedelta(
        days=1
    )


# -------------------------------------------------------- end conditions


def test_a_recurrence_ends_on_its_until_date() -> None:
    rig = make_rig()
    task = rig.create(
        reminder(
            recurring=True,
            sched=schedule(frequency=Frequency.DAILY, until=date(2026, 1, 6)),
        )
    )
    for _ in range(4 * 24):
        rig.advance_and_tick(hours=1)
    runs = [h for h in rig.history(task.task_id) if h.kind is HistoryKind.RUN]
    assert len(runs) == 2
    assert rig.task(task.task_id).status is TaskStatus.COMPLETED


def test_a_recurrence_ends_after_max_runs() -> None:
    rig = make_rig()
    task = rig.create(
        reminder(recurring=True, sched=schedule(frequency=Frequency.DAILY, max_runs=2))
    )
    for _ in range(5 * 24):
        rig.advance_and_tick(hours=1)
    assert len(rig.notifications()) == 2
    assert rig.task(task.task_id).status is TaskStatus.COMPLETED


# ------------------------------------------------- disable / delete / expire


def test_a_disabled_task_does_not_run_and_reenabling_schedules_from_now() -> None:
    rig = make_rig()
    task = rig.create(reminder(recurring=True))
    off = rig.service.update_task(OWNER, task.task_id, TaskUpdate(enabled=False))
    assert off.ok and off.data is not None and off.data.next_run_at is None
    for _ in range(3 * 24):
        rig.advance_and_tick(hours=1)
    assert rig.notifications() == () and rig.history(task.task_id) == ()
    on = rig.service.update_task(OWNER, task.task_id, TaskUpdate(enabled=True))
    assert on.ok and on.data is not None
    assert on.data.next_run_at is not None and on.data.next_run_at > rig.clock()


def test_a_disabled_task_stays_silent_even_if_it_was_already_due() -> None:
    rig = make_rig()
    task = rig.create(reminder(recurring=True))
    rig.clock.advance(timedelta(hours=1))  # now due
    rig.service.repository.replace_task(
        rig.task(task.task_id).model_copy(update={"enabled": False})
    )
    assert rig.tick() == 0
    assert rig.notifications() == ()


def test_a_deleted_task_never_runs_again() -> None:
    rig = make_rig()
    task = rig.create(reminder(recurring=True))
    pending = rig.service.delete_task(OWNER, task.task_id)
    assert not pending.ok and pending.permission == "confirm_required"
    assert pending.confirmation_id is not None
    rig.confirmations.decide(pending.confirmation_id, approved=True, now=rig.clock())
    done = rig.service.delete_task(OWNER, task.task_id, pending.confirmation_id)
    assert done.ok
    for _ in range(3 * 24):
        rig.advance_and_tick(hours=1)
    assert rig.notifications() == () and rig.service.repository.list_tasks() == ()


def test_an_expired_watch_stops_without_observing() -> None:
    rig = make_rig()
    task = rig.create(watch(expires_at=rig.clock() + timedelta(minutes=90)))
    rig.advance_and_tick(hours=1)
    assert rig.flag.calls == 1
    rig.advance_and_tick(hours=1)
    expired = rig.task(task.task_id)
    assert expired.status is TaskStatus.EXPIRED and not expired.enabled
    assert rig.flag.calls == 1
    assert HistoryKind.EXPIRED in [h.kind for h in rig.history(task.task_id)]


# ------------------------------------------------------ fail-closed input


@pytest.mark.parametrize(
    "tz",
    ["Mars/Olympus", "../../etc/passwd", "UTC+3", "Europe/../etc", "", " ", "a" * 70],
)
def test_an_invalid_timezone_fails_closed(tz: str) -> None:
    rig = make_rig()
    try:
        draft = reminder(sched=schedule(tz=tz))
    except ValidationError:
        pass  # refused by the schedule model itself
    else:
        result = rig.service.create_task(OWNER, draft)
        assert not result.ok and result.reason == "invalid_timezone"
    assert rig.service.repository.list_tasks() == ()


@pytest.mark.parametrize(
    ("kwargs", "task_type", "mode"),
    [
        ({"frequency": Frequency.DAILY}, TaskType.ONE_TIME, TimingMode.EXACT_SCHEDULE),
        ({"frequency": Frequency.NONE}, TaskType.RECURRING, TimingMode.EXACT_SCHEDULE),
        ({"at": None}, TaskType.ONE_TIME, TimingMode.EXACT_SCHEDULE),
        ({"daypart": Daypart.MORNING}, TaskType.ONE_TIME, TimingMode.EXACT_SCHEDULE),
        (
            {"at": None, "daypart": Daypart.MORNING, "frequency": Frequency.HOURLY},
            TaskType.RECURRING,
            TimingMode.FLEXIBLE_SCHEDULE,
        ),
        (
            {"frequency": Frequency.NONE, "until": date(2026, 2, 1)},
            TaskType.ONE_TIME,
            TimingMode.EXACT_SCHEDULE,
        ),
    ],
)
def test_a_malformed_schedule_fails_closed(
    kwargs: dict[str, Any], task_type: TaskType, mode: TimingMode
) -> None:
    rig = make_rig()
    draft = reminder().model_copy(
        update={
            "schedule": schedule(**kwargs),
            "task_type": task_type,
            "timing_mode": mode,
        }
    )
    result = rig.service.create_task(OWNER, draft)
    assert not result.ok
    assert rig.service.repository.list_tasks() == ()


@pytest.mark.parametrize(
    "raw",
    [
        {"frequency": "minutely"},
        {"frequency": "secondly"},
        {"frequency": "daily", "interval": 0},
        {"frequency": "daily", "interval": -1},
        {"frequency": "*/1 * * * *"},
        {"rrule": "FREQ=SECONDLY"},
        {"cron": "* * * * *"},
        {"time_of_day": "10:00:30"},
        {"weekdays": [7]},
        {"until": "2025-01-01"},
    ],
)
def test_schedule_shapes_outside_the_closed_vocabulary_are_refused(
    raw: dict[str, object],
) -> None:
    base: dict[str, object] = {
        "timezone": "Europe/London",
        "start_date": "2026-01-05",
        "time_of_day": "10:00",
    }
    with pytest.raises(ValidationError):
        Schedule.model_validate({**base, **raw})


def test_a_recurrence_faster_than_the_minimum_is_rejected() -> None:
    with pytest.raises(ValueError):
        ProactiveLimits(min_interval=timedelta(minutes=30))
    assert not hasattr(Frequency, "MINUTELY") and not hasattr(Frequency, "SECONDLY")
    strict = ProactiveLimits(min_interval=timedelta(hours=2))
    hourly = schedule(frequency=Frequency.HOURLY, interval=1)
    with pytest.raises(ScheduleError) as error:
        validate_schedule(
            hourly,
            TaskType.RECURRING,
            TimingMode.EXACT_SCHEDULE,
            min_interval=strict.min_interval,
        )
    assert error.value.code == "interval_below_minimum"
    rig = make_rig(limits=strict)
    result = rig.service.create_task(OWNER, watch(every_hours=1))
    assert not result.ok and result.reason == "interval_below_minimum"
    assert rig.service.create_task(OWNER, watch(every_hours=2)).ok


def test_the_scheduler_loop_starts_and_stops_cleanly_without_spinning() -> None:
    with pytest.raises(ValueError):
        ProactiveLimits(tick_seconds=0.01)
    rig = make_rig(scheduler_on=False)
    scheduler = rig.service.scheduler
    assert not scheduler.enabled and not scheduler.running
    scheduler.enable()  # one loop thread
    assert scheduler.enabled and scheduler.running
    scheduler.disable(timeout=5)
    assert not scheduler.enabled and not scheduler.running
    assert scheduler.tick() == 0  # a disabled scheduler dispatches nothing
    scheduler.enable()
    scheduler.shutdown(timeout=5)
    assert not scheduler.running and scheduler.tick() == 0
