"""Typed bridge models for the Proactive / Automations UI.

Requests are strict (unknown fields are refused) and carry no principal, no
permission, no confirmation for a future run, no provider, no command and no
code: the UI can only describe a schedule, name a condition from Sam's trusted
list, and manage the owner's own tasks. Responses are display data only.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sam.desktop.models import OperationResult

TaskTypeLiteral = Literal["one_time", "recurring", "condition_watch"]
TimingLiteral = Literal["exact_schedule", "flexible_schedule", "condition_watch"]
ActionLiteral = Literal["reminder", "summary", "watch"]
FrequencyLiteral = Literal["none", "hourly", "daily", "weekly"]
DaypartLiteral = Literal["morning", "afternoon", "evening"]
SemanticsLiteral = Literal["becomes_true", "on_change", "repeat_while_true"]
LevelLiteral = Literal["silent", "notify_owner", "requires_attention"]
ProposedLiteral = Literal[
    "none",
    "review_in_sam",
    "open_professional",
    "open_model_settings",
    "review_deadline",
]
TaskPrivacyLiteral = Literal["public", "personal", "private"]
# A notification may be stricter than its task (privacy only ever tightens).
NotificationPrivacyLiteral = Literal[
    "public", "normal", "personal", "private", "secret"
]

_TIME = r"^([01]\d|2[0-3]):[0-5]\d$"
_ID = r"^[a-z0-9_-]{1,64}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------- requests


class ProactiveScheduleIn(_Strict):
    timezone: str = Field(min_length=1, max_length=64)
    start_date: date
    time_of_day: str | None = Field(default=None, pattern=_TIME)
    daypart: DaypartLiteral | None = None
    frequency: FrequencyLiteral = "none"
    interval: int = Field(default=1, ge=1, le=365)
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    until: date | None = None
    max_runs: int | None = Field(default=None, ge=1, le=1_000)


class ProactiveCreateRequest(_Strict):
    title: str = Field(min_length=1, max_length=200)
    task_type: TaskTypeLiteral
    timing_mode: TimingLiteral
    action: ActionLiteral
    schedule: ProactiveScheduleIn
    condition_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,47}$")
    condition_params: dict[str, str] = Field(default_factory=dict, max_length=8)
    semantics: SemanticsLiteral = "becomes_true"
    instruction: str = Field(default="", max_length=2_000)
    privacy_class: TaskPrivacyLiteral = "personal"
    notification_level: LevelLiteral = "notify_owner"
    proposed_action: ProposedLiteral = "none"
    cooldown_hours: int = Field(default=24, ge=1, le=720)
    enabled: bool = True
    expires_at: datetime | None = None


class ProactiveUpdateRequest(_Strict):
    task_id: str = Field(pattern=_ID)
    enabled: bool | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    schedule: ProactiveScheduleIn | None = None
    instruction: str | None = Field(default=None, max_length=2_000)
    privacy_class: TaskPrivacyLiteral | None = None
    notification_level: LevelLiteral | None = None
    cooldown_hours: int | None = Field(default=None, ge=1, le=720)


class ProactiveDeleteRequest(_Strict):
    task_id: str = Field(pattern=_ID)
    confirmation_id: str | None = Field(default=None, max_length=100)


class ProactiveRunRequest(_Strict):
    task_id: str = Field(pattern=_ID)


class ProactiveSchedulerRequest(_Strict):
    """The owner's switch for background scheduling. Nothing else."""

    enabled: bool


class ProactiveNotificationRequest(_Strict):
    notification_id: str = Field(pattern=_ID)
    action: Literal["read", "dismiss"]


# -------------------------------------------------------------- responses


class ProSchedule(BaseModel):
    timezone: str
    start_date: str
    time_of_day: str | None = None
    daypart: DaypartLiteral | None = None
    frequency: FrequencyLiteral
    interval: int
    weekdays: list[int] = Field(default_factory=list)
    until: str | None = None
    max_runs: int | None = None


class ProTask(BaseModel):
    task_id: str
    title: str
    task_type: TaskTypeLiteral
    timing_mode: TimingLiteral
    action: ActionLiteral
    schedule: ProSchedule
    condition_id: str | None = None
    condition_params: dict[str, str] = Field(default_factory=dict)
    semantics: SemanticsLiteral | None = None
    instruction: str = ""
    privacy_class: TaskPrivacyLiteral
    notification_level: LevelLiteral
    proposed_action: ProposedLiteral
    cooldown_hours: int
    enabled: bool
    status: Literal["active", "completed", "missed", "expired", "invalid"]
    expires_at: str | None = None
    created_at: str
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_result: str | None = None
    last_failure: str | None = None
    last_reason: str | None = None
    running: bool = False
    version: int


class ProNotification(BaseModel):
    notification_id: str
    task_id: str
    title: str
    summary: str
    created_at: str
    reason_code: str
    importance: Literal["info", "attention"]
    source_label: str
    proposed_action: ProposedLiteral
    privacy_class: NotificationPrivacyLiteral
    read: bool


class ProHistory(BaseModel):
    record_id: str
    task_id: str
    kind: str
    trigger: str | None = None
    started_at: str
    completed_at: str | None = None
    result: str | None = None
    failure: str | None = None
    change: str | None = None
    reason_code: str | None = None
    notification_created: bool = False
    provider_id: str | None = None


class ProCondition(BaseModel):
    condition_id: str
    label: str
    required_params: list[str] = Field(default_factory=list)
    optional_params: list[str] = Field(default_factory=list)


class ProLimits(BaseModel):
    min_interval_hours: int
    max_tasks: int
    max_enabled_tasks: int
    max_title_chars: int
    max_instruction_chars: int


class ProactiveOverviewResponse(OperationResult):
    tasks: list[ProTask] = Field(default_factory=list)
    notifications: list[ProNotification] = Field(default_factory=list)
    history: list[ProHistory] = Field(default_factory=list)
    conditions: list[ProCondition] = Field(default_factory=list)
    limits: ProLimits | None = None
    live_runs: int = 0
    scheduler_enabled: bool = False


class ProactiveTaskResponse(OperationResult):
    task: ProTask | None = None


class ProactiveSchedulerResponse(OperationResult):
    scheduler_enabled: bool = False


__all__ = [
    "ProCondition",
    "ProHistory",
    "ProLimits",
    "ProNotification",
    "ProSchedule",
    "ProTask",
    "ProactiveCreateRequest",
    "ProactiveDeleteRequest",
    "ProactiveNotificationRequest",
    "ProactiveOverviewResponse",
    "ProactiveRunRequest",
    "ProactiveScheduleIn",
    "ProactiveSchedulerRequest",
    "ProactiveSchedulerResponse",
    "ProactiveTaskResponse",
    "ProactiveUpdateRequest",
]
