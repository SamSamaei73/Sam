"""Trusted, closed vocabulary and immutable records for the proactive engine.

Everything a task can do is an enum member defined here. A task stores DATA
only: a schedule, a closed action kind, an optional reference to a condition in
Sam's trusted registry, and the owner's instruction text. There is no field for
code, a command, a module path, a provider, a permission, a risk level or a
confirmation, and every model refuses unknown fields, so none can be smuggled
in by a request or by model output.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sam.models.models import PrivacyClass
from sam.permissions.models import Principal

# Absolute caps. ``ProactiveLimits`` (policy.py) may only tighten these.
HARD_MAX_TITLE = 200
HARD_MAX_INSTRUCTION = 2_000
HARD_MAX_SUMMARY = 1_000
HARD_MAX_PARAMS = 8
HARD_MAX_PARAM_CHARS = 200
MAX_INTERVAL = {"hourly": 168, "daily": 365, "weekly": 52}
MAX_RUNS = 1_000

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ID = r"^[a-z0-9_-]{1,64}$"
CONDITION_ID = r"^[a-z][a-z0-9_]{1,47}$"
PARAM_KEY = r"^[a-z][a-z0-9_]{0,31}$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TaskType(StrEnum):
    ONE_TIME = "one_time"
    RECURRING = "recurring"
    CONDITION_WATCH = "condition_watch"


class TimingMode(StrEnum):
    """Three timing semantics, never collapsed into one cron string.

    EXACT_SCHEDULE: the owner gave an exact wall-clock time.
    FLEXIBLE_SCHEDULE: the owner gave a daypart; any time inside it is fine.
    CONDITION_WATCH: checked on a cadence, notifies only on a trusted change.
    """

    EXACT_SCHEDULE = "exact_schedule"
    FLEXIBLE_SCHEDULE = "flexible_schedule"
    CONDITION_WATCH = "condition_watch"


class Frequency(StrEnum):
    """There is deliberately no minutely or secondly member."""

    NONE = "none"
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"


class Daypart(StrEnum):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"


# Local wall-clock windows for a flexible schedule: [start, end).
DAYPART_WINDOWS: dict[Daypart, tuple[time, time]] = {
    Daypart.MORNING: (time(8, 0), time(12, 0)),
    Daypart.AFTERNOON: (time(12, 0), time(17, 0)),
    Daypart.EVENING: (time(17, 0), time(21, 0)),
}


class TaskAction(StrEnum):
    """What a run does. A closed set: nothing else can ever run.

    REMINDER: a deterministic notification (no model).
    SUMMARY: a short summary through the Phase 13 ModelRouter.
    WATCH: observe one trusted condition from Sam's registry.
    """

    REMINDER = "reminder"
    SUMMARY = "summary"
    WATCH = "watch"


class TriggerSemantics(StrEnum):
    BECOMES_TRUE = "becomes_true"
    ON_CHANGE = "on_change"
    REPEAT_WHILE_TRUE = "repeat_while_true"


class NotificationLevel(StrEnum):
    SILENT = "silent"
    NOTIFY_OWNER = "notify_owner"
    REQUIRES_ATTENTION = "requires_attention"


class Importance(StrEnum):
    INFO = "info"
    ATTENTION = "attention"


class ProposedAction(StrEnum):
    """A label for what the owner might do next. It is never executed: the
    owner acts through Sam's normal, permissioned interactive flows."""

    NONE = "none"
    REVIEW_IN_SAM = "review_in_sam"
    OPEN_PROFESSIONAL = "open_professional"
    OPEN_MODEL_SETTINGS = "open_model_settings"
    REVIEW_DEADLINE = "review_deadline"


class TaskStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    MISSED = "missed"
    EXPIRED = "expired"
    INVALID = "invalid"


class RunResult(StrEnum):
    NO_NOTIFICATION = "no_notification"
    NOTIFICATION_CREATED = "notification_created"
    ACTION_PROPOSED = "action_proposed"
    ERROR_RECORDED = "error_recorded"


class RunFailure(StrEnum):
    TEMPORARY_FAILURE = "temporary_failure"
    PERMISSION_DENIED = "permission_denied"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMIT = "rate_limit"
    SOURCE_UNAVAILABLE = "source_unavailable"
    INVALID_TASK = "invalid_task"
    EXPIRED = "expired"
    POLICY_BLOCKED = "policy_blocked"


class ChangeKind(StrEnum):
    NO_CHANGE = "no_change"
    CHANGED = "changed"
    CONDITION_MET = "condition_met"
    CONDITION_CLEARED = "condition_cleared"
    ERROR = "error"
    UNKNOWN = "unknown"


class RunTrigger(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


class HistoryKind(StrEnum):
    RUN = "run"
    MISSED = "missed"
    SKIPPED_MISSED = "skipped_missed"
    COALESCED = "coalesced"
    TIMED_OUT = "timed_out"
    EXPIRED = "expired"
    INVALID = "invalid"


class NotificationReason(StrEnum):
    REMINDER_DUE = "reminder_due"
    REMINDER_MISSED = "reminder_missed"
    SUMMARY_READY = "summary_ready"
    CONDITION_MET = "condition_met"
    CONDITION_CHANGED = "condition_changed"
    CONDITION_STILL_TRUE = "condition_still_true"


# The privacy classes an owner may give a task. SECRET is never storable.
TASK_PRIVACY = frozenset(
    {PrivacyClass.PUBLIC, PrivacyClass.PERSONAL, PrivacyClass.PRIVATE}
)


def clean_text(value: str, limit: int) -> str:
    """Remove control characters, collapse surrounding space, bound length."""

    return _CONTROL.sub("", value).strip()[:limit]


class Schedule(_Frozen):
    """A deterministic schedule. The timezone is stored apart from the local
    wall-clock time, so a DST change or a timezone edit never rewrites the
    owner's intended local time. No cron string, no RRULE text, no code."""

    timezone: str = Field(min_length=1, max_length=64)
    start_date: date
    time_of_day: time | None = None
    daypart: Daypart | None = None
    frequency: Frequency = Frequency.NONE
    interval: int = Field(default=1, ge=1, le=365)
    weekdays: tuple[int, ...] = ()
    until: date | None = None
    max_runs: int | None = Field(default=None, ge=1, le=MAX_RUNS)

    @field_validator("timezone")
    @classmethod
    def _tz_shape(cls, value: str) -> str:
        if not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,2}", value
        ):
            raise ValueError("timezone must be an IANA name such as Europe/London")
        return value

    @field_validator("time_of_day")
    @classmethod
    def _minute_precision(cls, value: time | None) -> time | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            raise ValueError("time_of_day is local wall-clock time without an offset")
        if value.second or value.microsecond:
            raise ValueError("time_of_day has minute precision")
        return value

    @field_validator("weekdays")
    @classmethod
    def _weekdays(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(value) > 7 or any(d < 0 or d > 6 for d in value):
            raise ValueError("weekdays are 0 (Monday) to 6 (Sunday)")
        if len(set(value)) != len(value):
            raise ValueError("weekdays must not repeat")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.frequency is not Frequency.NONE:
            if self.interval > MAX_INTERVAL[self.frequency.value]:
                raise ValueError("interval is too large")
        elif self.interval != 1:
            raise ValueError("a one-time schedule has no interval")
        if self.weekdays and self.frequency is not Frequency.WEEKLY:
            raise ValueError("weekdays apply to weekly schedules only")
        if self.until is not None and self.until < self.start_date:
            raise ValueError("until is before the start date")
        return self


class ConditionSpec(_Frozen):
    """A reference to ONE condition in Sam's trusted registry, never code.
    Parameters are bounded strings that an observer parses itself."""

    condition_id: str = Field(pattern=CONDITION_ID)
    params: dict[str, str] = Field(default_factory=dict)
    semantics: TriggerSemantics = TriggerSemantics.BECOMES_TRUE

    @field_validator("params")
    @classmethod
    def _bounded(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > HARD_MAX_PARAMS:
            raise ValueError("too many condition parameters")
        for key, item in value.items():
            if not re.fullmatch(PARAM_KEY, key):
                raise ValueError("condition parameter names are simple identifiers")
            if len(item) > HARD_MAX_PARAM_CHARS or _CONTROL.search(item):
                raise ValueError("condition parameter value is malformed")
        return value


class ProactiveTask(_Frozen):
    """One owner-defined task plus the scheduler's own bookkeeping.

    The owner controls: title, schedule, enabled, cooldown, notification level,
    privacy class, instruction and expiry (through trusted task management).
    The scheduler controls: status, next/last run, run counts and last result.
    """

    task_id: str = Field(pattern=_ID)
    owner: Principal
    title: str = Field(min_length=1, max_length=HARD_MAX_TITLE)
    task_type: TaskType
    timing_mode: TimingMode
    action: TaskAction
    schedule: Schedule
    condition: ConditionSpec | None = None
    instruction: str = Field(default="", max_length=HARD_MAX_INSTRUCTION)
    privacy_class: PrivacyClass = PrivacyClass.PERSONAL
    notification_level: NotificationLevel = NotificationLevel.NOTIFY_OWNER
    proposed_action: ProposedAction = ProposedAction.NONE
    cooldown_hours: int = Field(default=24, ge=1, le=720)
    enabled: bool = True
    status: TaskStatus = TaskStatus.ACTIVE
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    runs_completed: int = Field(default=0, ge=0)
    last_result: RunResult | None = None
    last_failure: RunFailure | None = None
    last_reason: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")
    version: int = Field(default=1, ge=1)

    @property
    def timezone(self) -> str:
        return self.schedule.timezone

    @field_validator(
        "created_at", "updated_at", "last_run_at", "next_run_at", "expires_at"
    )
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("task timestamps must be timezone-aware")
        return value

    @field_validator("privacy_class")
    @classmethod
    def _privacy(cls, value: PrivacyClass) -> PrivacyClass:
        if value not in TASK_PRIVACY:
            raise ValueError("a task is public, personal or private; never secret")
        return value

    @model_validator(mode="after")
    def _combination(self) -> Self:
        watch = self.task_type is TaskType.CONDITION_WATCH
        if watch != (self.timing_mode is TimingMode.CONDITION_WATCH):
            raise ValueError("only a condition watch uses CONDITION_WATCH timing")
        if watch != (self.action is TaskAction.WATCH):
            raise ValueError("only a condition watch observes a condition")
        if watch != (self.condition is not None):
            raise ValueError("a condition watch needs exactly one condition")
        return self


class ConditionState(_Frozen):
    """What Sam last observed for one watch (the repository keeps it)."""

    met: bool = False
    value_key: str | None = Field(default=None, max_length=128)
    episode: int = Field(default=0, ge=0)
    observed_at: datetime | None = None
    last_notified_at: datetime | None = None


class ProactiveNotification(_Frozen):
    """Bounded, safe metadata for the owner's in-app inbox. No raw document,
    email, credential, prompt or biometric data is ever stored here."""

    notification_id: str = Field(pattern=_ID)
    task_id: str = Field(pattern=_ID)
    owner_id: str = Field(min_length=1, max_length=200)
    title: str = Field(max_length=HARD_MAX_TITLE)
    summary: str = Field(max_length=HARD_MAX_SUMMARY)
    created_at: datetime
    reason_code: NotificationReason
    importance: Importance
    source_label: str = Field(max_length=64)
    proposed_action: ProposedAction = ProposedAction.NONE
    privacy_class: PrivacyClass = PrivacyClass.PERSONAL
    read: bool = False


class RunRecord(_Frozen):
    """One history entry: metadata only, never text from the task."""

    record_id: str = Field(pattern=_ID)
    task_id: str = Field(pattern=_ID)
    kind: HistoryKind
    task_type: TaskType
    timing_mode: TimingMode
    trigger: RunTrigger | None = None
    started_at: datetime
    completed_at: datetime | None = None
    result: RunResult | None = None
    failure: RunFailure | None = None
    change: ChangeKind | None = None
    reason_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")
    notification_created: bool = False
    provider_id: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")


__all__ = [
    "DAYPART_WINDOWS",
    "HARD_MAX_INSTRUCTION",
    "HARD_MAX_SUMMARY",
    "HARD_MAX_TITLE",
    "MAX_RUNS",
    "TASK_PRIVACY",
    "ChangeKind",
    "ConditionSpec",
    "ConditionState",
    "Daypart",
    "Frequency",
    "HistoryKind",
    "Importance",
    "NotificationLevel",
    "NotificationReason",
    "ProactiveNotification",
    "ProactiveTask",
    "ProposedAction",
    "RunFailure",
    "RunRecord",
    "RunResult",
    "RunTrigger",
    "Schedule",
    "TaskAction",
    "TaskStatus",
    "TaskType",
    "TimingMode",
    "TriggerSemantics",
    "clean_text",
]
