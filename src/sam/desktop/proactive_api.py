"""Proactive / Automations routes for the desktop UI.

Every route is OWNER-bound: ``owner_bridge_runtime`` refuses it while Guest Mode
is active, before the request body is used and before the service is reached,
so a guest can neither read the owner's tasks and notifications nor create,
edit, delete or run anything. The routes hold no authority of their own: every
operation goes through ``ProactiveService`` and the PermissionEngine under
``PermissionResource.PROACTIVE`` (deleting a task always asks for a
confirmation). A suggested next action in a notification is only a label; no
route executes it.

The activity log records THAT something happened, never a title, an
instruction or a summary.
"""

from __future__ import annotations

from datetime import time
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import ValidationError

from sam.desktop.api import _challenge
from sam.desktop.models import OperationResult, OperationStatus
from sam.desktop.proactive_models import (
    ProactiveCreateRequest,
    ProactiveDeleteRequest,
    ProactiveNotificationRequest,
    ProactiveOverviewResponse,
    ProactiveRunRequest,
    ProactiveScheduleIn,
    ProactiveSchedulerRequest,
    ProactiveSchedulerResponse,
    ProactiveTaskResponse,
    ProactiveUpdateRequest,
    ProCondition,
    ProHistory,
    ProLimits,
    ProNotification,
    ProSchedule,
    ProTask,
)
from sam.desktop.runtime import DesktopRuntime
from sam.desktop.security import owner_bridge_runtime
from sam.models.models import PrivacyClass
from sam.proactive.models import (
    ConditionSpec,
    Daypart,
    Frequency,
    NotificationLevel,
    ProactiveNotification,
    ProactiveTask,
    ProposedAction,
    RunRecord,
    Schedule,
    TaskAction,
    TaskType,
    TimingMode,
    TriggerSemantics,
)
from sam.proactive.service import OpResult, TaskDraft, TaskUpdate

router = APIRouter(prefix="/desktop/v1/proactive", tags=["desktop-proactive"])
_owner_runtime = Depends(owner_bridge_runtime)

_MESSAGES = {
    "permission_denied": "Sam isn't permitted to do that.",
    "confirmation_required": "This needs your confirmation.",
    "confirmation_invalid": "That confirmation is no longer valid.",
    "task_not_found": "That automation no longer exists.",
    "notification_not_found": "That notification no longer exists.",
    "task_not_active": "That automation has finished, so it can't run now.",
    "already_running": "That automation is already running.",
    "busy": "Sam is busy with other automations. Try again shortly.",
    "scheduler_disabled": (
        "Automations are switched off. Turn on scheduling to run them."
    ),
    "stopped": "Automations are stopped.",
    "invalid_task": "That automation isn't valid.",
    "invalid_schedule": "That schedule isn't valid.",
    "invalid_timezone": (
        "That timezone isn't recognised. Use a name like Europe/London."
    ),
    "interval_below_minimum": "Automations can repeat at most once an hour.",
    "exact_time_required": "Choose an exact time for this schedule.",
    "daypart_required": "Choose morning, afternoon or evening.",
    "one_time_has_no_recurrence": "A one-time automation doesn't repeat.",
    "one_time_has_no_end": "A one-time automation has no end date.",
    "recurrence_required": "Choose how often it repeats.",
    "flexible_is_daily_or_weekly": "A flexible schedule repeats daily or weekly.",
    "no_future_run": "That schedule has no future run.",
    "already_expired": "That automation would already have expired.",
    "secret_detected": "That looked like it contained a secret, so it was not saved.",
    "unknown_condition": "That isn't one of Sam's trusted conditions.",
    "condition_params_invalid": "Those condition details aren't valid.",
    "instruction_required": "A summary needs an instruction.",
    "title_required": "Give the automation a title.",
    "title_too_long": "That title is too long.",
    "instruction_too_long": "That instruction is too long.",
    "cooldown_out_of_range": "That cooldown is out of range.",
    "too_many_tasks": "You have reached the maximum number of automations.",
    "too_many_enabled_tasks": "Too many automations are enabled. Disable one first.",
}
_DEFAULT = "The request couldn't be completed."
_PRIVACY_IN = {
    "public": PrivacyClass.PUBLIC,
    "personal": PrivacyClass.PERSONAL,
    "private": PrivacyClass.PRIVATE,
}
_MAX_HISTORY = 100


def _message(code: str | None) -> str | None:
    return None if code is None else _MESSAGES.get(code, _DEFAULT)


def _operation(runtime: DesktopRuntime, result: OpResult[Any]) -> OperationResult:
    reference = result.operation_id or None
    if result.permission == "confirm_required":
        return OperationResult(
            status="confirmation_required",
            reason_code="confirmation_required",
            message=_message("confirmation_required"),
            reference_id=reference,
            challenge=_challenge(runtime, result.confirmation_id),
        )
    if result.permission == "deny":
        code = result.reason or "permission_denied"
        return OperationResult(
            status="denied",
            reason_code=code,
            message=_message(code),
            reference_id=reference,
        )
    if result.ok:
        return OperationResult(status="ok", reference_id=reference)
    code = result.reason or "failed"
    status: OperationStatus = "failed" if code == "internal_error" else "rejected"
    return OperationResult(
        status=status, reason_code=code, message=_message(code), reference_id=reference
    )


def _rejected(code: str) -> OperationResult:
    return OperationResult(status="rejected", reason_code=code, message=_message(code))


def _privacy_out(value: PrivacyClass) -> Any:
    return value.name.lower()


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


def _schedule_in(payload: ProactiveScheduleIn) -> Schedule:
    at = None
    if payload.time_of_day is not None:
        hours, minutes = payload.time_of_day.split(":")
        at = time(int(hours), int(minutes))
    return Schedule(
        timezone=payload.timezone,
        start_date=payload.start_date,
        time_of_day=at,
        daypart=Daypart(payload.daypart) if payload.daypart else None,
        frequency=Frequency(payload.frequency),
        interval=payload.interval,
        weekdays=tuple(payload.weekdays),
        until=payload.until,
        max_runs=payload.max_runs,
    )


def _task(task: ProactiveTask, running: bool) -> ProTask:
    s = task.schedule
    return ProTask(
        task_id=task.task_id,
        title=task.title,
        task_type=task.task_type.value,
        timing_mode=task.timing_mode.value,
        action=task.action.value,
        schedule=ProSchedule(
            timezone=s.timezone,
            start_date=s.start_date.isoformat(),
            time_of_day=s.time_of_day.strftime("%H:%M") if s.time_of_day else None,
            daypart=s.daypart.value if s.daypart else None,
            frequency=s.frequency.value,
            interval=s.interval,
            weekdays=list(s.weekdays),
            until=_iso(s.until),
            max_runs=s.max_runs,
        ),
        condition_id=task.condition.condition_id if task.condition else None,
        condition_params=dict(task.condition.params) if task.condition else {},
        semantics=task.condition.semantics.value if task.condition else None,
        instruction=task.instruction,
        privacy_class=_privacy_out(task.privacy_class),
        notification_level=task.notification_level.value,
        proposed_action=task.proposed_action.value,
        cooldown_hours=task.cooldown_hours,
        enabled=task.enabled,
        status=task.status.value,
        expires_at=_iso(task.expires_at),
        created_at=task.created_at.isoformat(),
        next_run_at=_iso(task.next_run_at),
        last_run_at=_iso(task.last_run_at),
        last_result=task.last_result.value if task.last_result else None,
        last_failure=task.last_failure.value if task.last_failure else None,
        last_reason=task.last_reason,
        running=running,
        version=task.version,
    )


def _notification(n: ProactiveNotification) -> ProNotification:
    return ProNotification(
        notification_id=n.notification_id,
        task_id=n.task_id,
        title=n.title,
        summary=n.summary,
        created_at=n.created_at.isoformat(),
        reason_code=n.reason_code.value,
        importance=n.importance.value,
        source_label=n.source_label,
        proposed_action=n.proposed_action.value,
        privacy_class=_privacy_out(n.privacy_class),
        read=n.read,
    )


def _history(r: RunRecord) -> ProHistory:
    return ProHistory(
        record_id=r.record_id,
        task_id=r.task_id,
        kind=r.kind.value,
        trigger=r.trigger.value if r.trigger else None,
        started_at=r.started_at.isoformat(),
        completed_at=_iso(r.completed_at),
        result=r.result.value if r.result else None,
        failure=r.failure.value if r.failure else None,
        change=r.change.value if r.change else None,
        reason_code=r.reason_code,
        notification_created=r.notification_created,
        provider_id=r.provider_id,
    )


def _task_response(
    runtime: DesktopRuntime, result: OpResult[ProactiveTask]
) -> ProactiveTaskResponse:
    base = _operation(runtime, result)
    task = result.data
    scheduler = runtime.proactive.scheduler
    return ProactiveTaskResponse(
        **base.model_dump(),
        task=_task(task, scheduler.is_busy(task.task_id)) if task else None,
    )


@router.get("/overview", response_model=ProactiveOverviewResponse)
def proactive_overview(
    runtime: DesktopRuntime = _owner_runtime,
) -> ProactiveOverviewResponse:
    service = runtime.proactive
    result = service.overview(runtime.principal)
    base = _operation(runtime, result)
    data = result.data
    if data is None:
        return ProactiveOverviewResponse(**base.model_dump())
    limits = service.limits
    return ProactiveOverviewResponse(
        **base.model_dump(),
        tasks=[_task(t, service.scheduler.is_busy(t.task_id)) for t in data.tasks],
        notifications=[_notification(n) for n in data.notifications],
        history=[_history(r) for r in data.history[:_MAX_HISTORY]],
        conditions=[
            ProCondition(
                condition_id=e.condition_id,
                label=e.label,
                required_params=sorted(e.required_params),
                optional_params=sorted(e.optional_params),
            )
            for e in service.policy.registry.entries()
        ],
        limits=ProLimits(
            min_interval_hours=int(limits.min_interval.total_seconds() // 3600),
            max_tasks=limits.max_tasks,
            max_enabled_tasks=limits.max_enabled_tasks,
            max_title_chars=limits.max_title_chars,
            max_instruction_chars=limits.max_instruction_chars,
        ),
        live_runs=data.live_runs,
        scheduler_enabled=data.scheduler_enabled,
    )


@router.post("/create", response_model=ProactiveTaskResponse)
def proactive_create(
    payload: ProactiveCreateRequest, runtime: DesktopRuntime = _owner_runtime
) -> ProactiveTaskResponse:
    try:
        condition = (
            ConditionSpec(
                condition_id=payload.condition_id,
                params=payload.condition_params,
                semantics=TriggerSemantics(payload.semantics),
            )
            if payload.condition_id
            else None
        )
        draft = TaskDraft(
            title=payload.title,
            task_type=TaskType(payload.task_type),
            timing_mode=TimingMode(payload.timing_mode),
            action=TaskAction(payload.action),
            schedule=_schedule_in(payload.schedule),
            condition=condition,
            instruction=payload.instruction,
            privacy_class=_PRIVACY_IN[payload.privacy_class],
            notification_level=NotificationLevel(payload.notification_level),
            proposed_action=ProposedAction(payload.proposed_action),
            cooldown_hours=payload.cooldown_hours,
            enabled=payload.enabled,
            expires_at=payload.expires_at,
        )
    except (ValidationError, ValueError):
        runtime.activity.add("proactive", "Automation create", "invalid")
        return ProactiveTaskResponse(**_rejected("invalid_task").model_dump())
    result = runtime.proactive.create_task(runtime.principal, draft)
    runtime.activity.add(
        "proactive",
        "Automation create",
        "ok" if result.ok else (result.reason or "refused"),
    )
    return _task_response(runtime, result)


@router.post("/update", response_model=ProactiveTaskResponse)
def proactive_update(
    payload: ProactiveUpdateRequest, runtime: DesktopRuntime = _owner_runtime
) -> ProactiveTaskResponse:
    try:
        update = TaskUpdate(
            enabled=payload.enabled,
            title=payload.title,
            schedule=_schedule_in(payload.schedule) if payload.schedule else None,
            instruction=payload.instruction,
            privacy_class=(
                _PRIVACY_IN[payload.privacy_class] if payload.privacy_class else None
            ),
            notification_level=(
                NotificationLevel(payload.notification_level)
                if payload.notification_level
                else None
            ),
            cooldown_hours=payload.cooldown_hours,
        )
    except (ValidationError, ValueError):
        runtime.activity.add("proactive", "Automation update", "invalid")
        return ProactiveTaskResponse(**_rejected("invalid_task").model_dump())
    result = runtime.proactive.update_task(runtime.principal, payload.task_id, update)
    runtime.activity.add(
        "proactive",
        "Automation update",
        "ok" if result.ok else (result.reason or "refused"),
    )
    return _task_response(runtime, result)


@router.post("/delete", response_model=OperationResult)
def proactive_delete(
    payload: ProactiveDeleteRequest, runtime: DesktopRuntime = _owner_runtime
) -> OperationResult:
    result = runtime.proactive.delete_task(
        runtime.principal, payload.task_id, payload.confirmation_id
    )
    runtime.activity.add("proactive", "Automation delete", result.permission)
    return _operation(runtime, result)


@router.post("/run", response_model=OperationResult)
def proactive_run(
    payload: ProactiveRunRequest, runtime: DesktopRuntime = _owner_runtime
) -> OperationResult:
    result = runtime.proactive.run_now(runtime.principal, payload.task_id)
    runtime.activity.add(
        "proactive", "Automation run now", "started" if result.ok else "refused"
    )
    return _operation(runtime, result)


@router.post("/scheduler", response_model=ProactiveSchedulerResponse)
def proactive_scheduler(
    payload: ProactiveSchedulerRequest, runtime: DesktopRuntime = _owner_runtime
) -> ProactiveSchedulerResponse:
    """The owner-only switch for background scheduling (off by default and after
    every restart). Guest Mode is refused before this body is read. Turning it on
    grants no permission: every run is still authorized on its own."""

    result = runtime.proactive.set_scheduler_enabled(runtime.principal, payload.enabled)
    runtime.activity.add(
        "proactive",
        "Automations scheduling on"
        if payload.enabled
        else "Automations scheduling off",
        "ok" if result.ok else "refused",
    )
    base = _operation(runtime, result)
    return ProactiveSchedulerResponse(
        **base.model_dump(), scheduler_enabled=runtime.proactive.scheduler_enabled
    )


@router.post("/notification", response_model=OperationResult)
def proactive_notification(
    payload: ProactiveNotificationRequest, runtime: DesktopRuntime = _owner_runtime
) -> OperationResult:
    result = runtime.proactive.update_notification(
        runtime.principal, payload.notification_id, payload.action
    )
    runtime.activity.add(
        "proactive", f"Notification {payload.action}", "ok" if result.ok else "refused"
    )
    return _operation(runtime, result)


__all__ = ["router"]
