"""Deterministic schedule arithmetic. No model does date or timezone math.

Rules (documented in docs/proactive-agent.md):

* Every schedule has an explicit IANA timezone, stored apart from the local
  wall-clock time. Occurrences are computed from (local date, local time,
  zone) with ``zoneinfo`` every time, so a DST rule change or an owner's
  timezone edit is honoured without rewriting the task.
* A nonexistent local time (DST spring-forward gap) resolves with ``fold=0``,
  which maps it to the same instant as the pre-transition offset: the
  occurrence happens once, shifted forward by the length of the gap.
* An ambiguous local time (DST fall-back) resolves to its FIRST occurrence
  (``fold=0``). Daily and weekly schedules produce one occurrence per local
  date, so the repeated hour never runs twice.
* HOURLY schedules step in elapsed hours from their anchor instant, so the
  local clock time of an hourly run shifts across a DST change while the
  interval between runs stays exact.
* Every search is bounded; nothing iterates over missed history.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sam.proactive.models import (
    DAYPART_WINDOWS,
    Frequency,
    Schedule,
    TaskType,
    TimingMode,
)

# The longest search a single next-occurrence computation may make.
_MAX_SEARCH_DAYS = 800


class ScheduleError(ValueError):
    """A schedule that cannot be used. Always fails closed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def zone(name: str) -> ZoneInfo:
    if ".." in name or name.startswith("/") or "\\" in name:
        raise ScheduleError("invalid_timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ScheduleError("invalid_timezone") from None


def anchor_time(schedule: Schedule) -> time:
    if schedule.time_of_day is not None:
        return schedule.time_of_day
    if schedule.daypart is not None:
        return DAYPART_WINDOWS[schedule.daypart][0]
    raise ScheduleError("missing_time")


def resolve_local(day: date, at: time, tz: ZoneInfo) -> datetime:
    """The UTC instant of a local wall-clock time (see module rules)."""

    return datetime.combine(day, at).replace(tzinfo=tz, fold=0).astimezone(UTC)


def local_date(instant: datetime, tz: ZoneInfo) -> date:
    return instant.astimezone(tz).date()


def period(schedule: Schedule) -> timedelta | None:
    """The shortest possible gap between two occurrences, or None (one-time)."""

    if schedule.frequency is Frequency.HOURLY:
        return timedelta(hours=schedule.interval)
    if schedule.frequency is Frequency.DAILY:
        return timedelta(days=schedule.interval)
    if schedule.frequency is Frequency.WEEKLY:
        days = schedule.weekdays or ()
        if len(days) > 1:
            gaps = [b - a for a, b in zip(days, days[1:], strict=False)]
            gaps.append(7 * schedule.interval - days[-1] + days[0])
            return timedelta(days=min(gaps))
        return timedelta(weeks=schedule.interval)
    return None


def validate_schedule(
    schedule: Schedule,
    task_type: TaskType,
    timing_mode: TimingMode,
    *,
    min_interval: timedelta,
) -> None:
    """Semantic checks beyond the model's shape. Raises ``ScheduleError``."""

    zone(schedule.timezone)
    exact = timing_mode is TimingMode.EXACT_SCHEDULE
    flexible = timing_mode is TimingMode.FLEXIBLE_SCHEDULE
    watch = timing_mode is TimingMode.CONDITION_WATCH
    if (exact or watch) and (schedule.time_of_day is None or schedule.daypart):
        raise ScheduleError("exact_time_required")
    if flexible and (schedule.daypart is None or schedule.time_of_day is not None):
        raise ScheduleError("daypart_required")
    if task_type is TaskType.ONE_TIME:
        if schedule.frequency is not Frequency.NONE:
            raise ScheduleError("one_time_has_no_recurrence")
        if schedule.max_runs not in (None, 1) or schedule.until is not None:
            raise ScheduleError("one_time_has_no_end")
    elif schedule.frequency is Frequency.NONE:
        raise ScheduleError("recurrence_required")
    if flexible and schedule.frequency is Frequency.HOURLY:
        raise ScheduleError("flexible_is_daily_or_weekly")
    gap = period(schedule)
    if gap is not None and gap < min_interval:
        raise ScheduleError("interval_below_minimum")


def _daily_ok(schedule: Schedule, day: date) -> bool:
    return (day - schedule.start_date).days % schedule.interval == 0


def _weekly_ok(schedule: Schedule, day: date) -> bool:
    weekdays = schedule.weekdays or (schedule.start_date.weekday(),)
    if day.weekday() not in weekdays:
        return False
    start_monday = schedule.start_date - timedelta(days=schedule.start_date.weekday())
    weeks = (day - start_monday).days // 7
    return weeks % schedule.interval == 0


def next_occurrence(schedule: Schedule, after: datetime) -> datetime | None:
    """The first occurrence strictly after ``after`` (an aware instant), or
    None when the schedule has ended. Deterministic and bounded."""

    tz = zone(schedule.timezone)
    at = anchor_time(schedule)
    after = after.astimezone(UTC)

    def within_end(day: date) -> bool:
        return schedule.until is None or day <= schedule.until

    if schedule.frequency is Frequency.NONE:
        single = resolve_local(schedule.start_date, at, tz)
        return single if single > after else None

    if schedule.frequency is Frequency.HOURLY:
        base = resolve_local(schedule.start_date, at, tz)
        step = timedelta(hours=schedule.interval)
        if after < base:
            candidate = base
        else:
            candidate = base + step * ((after - base) // step + 1)
        return candidate if within_end(local_date(candidate, tz)) else None

    day = max(schedule.start_date, local_date(after, tz) - timedelta(days=1))
    for _ in range(_MAX_SEARCH_DAYS):
        if not within_end(day):
            return None
        matches = (
            _daily_ok(schedule, day)
            if schedule.frequency is Frequency.DAILY
            else _weekly_ok(schedule, day)
        )
        if matches:
            instant = resolve_local(day, at, tz)
            if instant > after:
                return instant
        day += timedelta(days=1)
    return None


def window_end(schedule: Schedule, occurrence: datetime) -> datetime | None:
    """For a flexible schedule, the end of the daypart window that contains
    ``occurrence``; None for other schedules."""

    if schedule.daypart is None:
        return None
    tz = zone(schedule.timezone)
    end = DAYPART_WINDOWS[schedule.daypart][1]
    return resolve_local(local_date(occurrence, tz), end, tz)


__all__ = [
    "ScheduleError",
    "anchor_time",
    "local_date",
    "next_occurrence",
    "period",
    "resolve_local",
    "validate_schedule",
    "window_end",
    "zone",
]
