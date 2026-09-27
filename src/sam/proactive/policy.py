"""Trusted limits and deterministic validation for proactive tasks.

``ProactiveLimits`` is trusted configuration built by Sam's code, never by a
request or a model. Its constructor refuses values that would make the engine
unsafe (a recurrence faster than one hour, unbounded concurrency, and so on).
``ProactivePolicy.validate`` runs on creation, on every edit AND again on every
run, so a task that became invalid (or was corrupted in storage) fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sam.models.models import PrivacyClass
from sam.models.policies import PrivacyPolicy
from sam.proactive.conditions import ConditionRegistry
from sam.proactive.models import (
    HARD_MAX_INSTRUCTION,
    HARD_MAX_SUMMARY,
    HARD_MAX_TITLE,
    Importance,
    NotificationLevel,
    ProactiveTask,
    TaskAction,
)
from sam.proactive.schedule import ScheduleError, validate_schedule

# The floor for any recurrence or watch cadence. Changing it needs a reviewed
# use case (docs/proactive-agent.md); the limits constructor enforces it.
MIN_INTERVAL_FLOOR = timedelta(hours=1)
# How late an EXACT-time task may start and still run. A task is overdue ("missed")
# iff ``now - scheduled_time > OVERDUE_THRESHOLD``: exactly at the threshold it
# still runs; one second beyond it, it is missed. Deterministic, never a model.
OVERDUE_THRESHOLD = timedelta(hours=1)
# Time the Phase 13 router may spend on provider readiness probes per request
# (the Claude adapter runs at most two 20 s probes), added to the provider call
# timeouts when checking that a summary fits inside the run budget.
ROUTER_PROBE_ALLOWANCE = timedelta(seconds=60)
_PRIVACY = PrivacyPolicy()


@dataclass(frozen=True)
class ProactiveLimits:
    max_tasks: int = 100
    max_enabled_tasks: int = 50
    max_title_chars: int = 120
    max_instruction_chars: int = 1_000
    max_summary_chars: int = 500
    max_notifications: int = 500
    max_history: int = 1_000
    max_value_key_chars: int = 128
    max_concurrent_runs: int = 2
    max_dispatch_per_tick: int = 10
    run_timeout: timedelta = timedelta(minutes=5)
    min_interval: timedelta = MIN_INTERVAL_FLOOR
    overdue_threshold: timedelta = OVERDUE_THRESHOLD
    dedup_window: timedelta = timedelta(days=7)
    max_dedup_keys: int = 5_000
    min_cooldown_hours: int = 1
    max_cooldown_hours: int = 720
    tick_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.min_interval < MIN_INTERVAL_FLOOR:
            raise ValueError("the minimum recurrence interval is one hour")
        if not 1 <= self.max_concurrent_runs <= 8:
            raise ValueError("concurrent runs are bounded to 1-8")
        if not 1 <= self.max_dispatch_per_tick <= 50:
            raise ValueError("dispatch per tick is bounded to 1-50")
        if not 1 <= self.max_tasks <= 1_000:
            raise ValueError("max_tasks is bounded to 1-1000")
        if not 1 <= self.max_enabled_tasks <= self.max_tasks:
            raise ValueError("max_enabled_tasks must not exceed max_tasks")
        if not timedelta(seconds=1) <= self.run_timeout <= timedelta(minutes=30):
            raise ValueError("run_timeout is bounded")
        if not timedelta(minutes=1) <= self.overdue_threshold <= timedelta(hours=24):
            raise ValueError("overdue_threshold is bounded")
        if self.max_title_chars > HARD_MAX_TITLE:
            raise ValueError("title limit exceeds the hard cap")
        if self.max_instruction_chars > HARD_MAX_INSTRUCTION:
            raise ValueError("instruction limit exceeds the hard cap")
        if self.max_summary_chars > HARD_MAX_SUMMARY:
            raise ValueError("summary limit exceeds the hard cap")
        if self.tick_seconds < 5:
            raise ValueError("the scheduler never polls faster than every 5 seconds")
        if not 1 <= self.max_notifications <= 10_000:
            raise ValueError("notification retention is bounded")
        if not 1 <= self.max_history <= 10_000:
            raise ValueError("history retention is bounded")


class TaskInvalid(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def is_secret(*texts: str) -> bool:
    """Phase 13's own detector: SECRET content is never stored or sent."""

    return _PRIVACY.classify(texts, PrivacyClass.PUBLIC) is PrivacyClass.SECRET


class ProactivePolicy:
    def __init__(self, limits: ProactiveLimits, registry: ConditionRegistry) -> None:
        self.limits = limits
        self.registry = registry

    def validate(self, task: ProactiveTask) -> None:
        """Every rule a task must satisfy. Raises ``TaskInvalid``."""

        limits = self.limits
        try:
            # Round-trip through the strict model: a record corrupted in storage
            # (or built without validation) cannot pass.
            ProactiveTask.model_validate(task.model_dump())
        except ValueError:
            raise TaskInvalid("invalid_task") from None
        if len(task.title) > limits.max_title_chars:
            raise TaskInvalid("title_too_long")
        if len(task.instruction) > limits.max_instruction_chars:
            raise TaskInvalid("instruction_too_long")
        if is_secret(task.title, task.instruction, *self._params(task)):
            raise TaskInvalid("secret_detected")
        if not (
            limits.min_cooldown_hours
            <= task.cooldown_hours
            <= limits.max_cooldown_hours
        ):
            raise TaskInvalid("cooldown_out_of_range")
        if task.action is TaskAction.SUMMARY and not task.instruction.strip():
            raise TaskInvalid("instruction_required")
        if (
            task.action is TaskAction.SUMMARY
            and task.privacy_class >= PrivacyClass.PRIVATE
        ):
            # A PRIVATE notification never shows derived text, so a private
            # summary could never be displayed: nothing private is sent to a
            # provider for it. Private summaries are not supported in Phase 15.
            raise TaskInvalid("private_summary_not_supported")
        try:
            validate_schedule(
                task.schedule,
                task.task_type,
                task.timing_mode,
                min_interval=limits.min_interval,
            )
        except ScheduleError as error:
            raise TaskInvalid(error.code) from None
        if task.condition is not None:
            spec = self.registry.get(task.condition.condition_id)
            if spec is None:
                raise TaskInvalid("unknown_condition")
            if not spec.accepts(task.condition.params):
                raise TaskInvalid("condition_params_invalid")

    @staticmethod
    def _params(task: ProactiveTask) -> tuple[str, ...]:
        return tuple(task.condition.params.values()) if task.condition else ()

    def is_expired(self, task: ProactiveTask, now: datetime) -> bool:
        return task.expires_at is not None and now >= task.expires_at


class ProactiveNotificationPolicy:
    """Decides only whether and how a candidate is surfaced. It never grants
    anything: a notification is not an approved action."""

    @staticmethod
    def surfaces(level: NotificationLevel) -> bool:
        return level is not NotificationLevel.SILENT

    @staticmethod
    def importance(level: NotificationLevel) -> Importance:
        return (
            Importance.ATTENTION
            if level is NotificationLevel.REQUIRES_ATTENTION
            else Importance.INFO
        )


__all__ = [
    "MIN_INTERVAL_FLOOR",
    "OVERDUE_THRESHOLD",
    "ROUTER_PROBE_ALLOWANCE",
    "ProactiveLimits",
    "ProactiveNotificationPolicy",
    "ProactivePolicy",
    "TaskInvalid",
    "is_secret",
]
