"""``ProactiveService``: the one Sam-owned entry point to the proactive engine.

Every operation is authorized by the PermissionEngine under
``PermissionResource.PROACTIVE`` before anything is read or changed:

    READ    list tasks, history and notifications       LOW
    CREATE  create a task                               MEDIUM
    UPDATE  edit / enable / disable, mark notifications MEDIUM
    EXECUTE run a task now (and every scheduled run)    MEDIUM
    DELETE  delete a task                               HIGH + confirmation

A principal only ever sees and changes its OWN tasks and notifications; an
unknown or foreign id is reported as not found. Model output can only become a
DRAFT (``propose_from_model``): it is schema-validated, schedule-validated and
policy-validated, is always disabled, and can only become a task through
``create_task`` by the owner, which the PermissionEngine authorizes.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sam.models.models import MAX_ATTEMPTS, PrivacyClass
from sam.models.router import ModelRouter
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    DecisionOutcome,
    PermissionAction,
    PermissionRequest,
    PermissionResource,
    PermissionScope,
    Principal,
)
from sam.proactive.audit import (
    InMemoryProactiveAuditSink,
    ProactiveAuditSink,
    ProactiveOperation,
    new_event,
)
from sam.proactive.clock import Clock, SystemClock
from sam.proactive.conditions import ConditionRegistry
from sam.proactive.models import (
    HARD_MAX_INSTRUCTION,
    HARD_MAX_TITLE,
    ConditionSpec,
    NotificationLevel,
    ProactiveNotification,
    ProactiveTask,
    ProposedAction,
    RunRecord,
    Schedule,
    TaskAction,
    TaskStatus,
    TaskType,
    TimingMode,
    clean_text,
)
from sam.proactive.policy import (
    ROUTER_PROBE_ALLOWANCE,
    ProactiveLimits,
    ProactivePolicy,
    TaskInvalid,
)
from sam.proactive.repository import (
    InMemoryProactiveRepository,
    ProactiveRepository,
    RepositoryLimit,
)
from sam.proactive.runner import TaskRunner, task_scope
from sam.proactive.schedule import ScheduleError, next_occurrence
from sam.proactive.scheduler import ProactiveScheduler

_R = PermissionResource.PROACTIVE
_SCOPE_TASKS = PermissionScope.from_path("proactive/tasks")
_SCOPE_NOTIFICATIONS = PermissionScope.from_path("proactive/notifications")
_SCOPE_SCHEDULER = PermissionScope.from_path("proactive/scheduler")


@dataclass(frozen=True)
class OpResult[T]:
    permission: str
    ok: bool
    reason: str | None = None
    confirmation_id: str | None = None
    data: T | None = None
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TaskDraft(_Strict):
    """What an owner (or, as a proposal, a model) may specify. Everything else
    (ids, status, run bookkeeping, owner) is Sam's."""

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
    expires_at: datetime | None = None


class TaskUpdate(_Strict):
    """Trusted task management by the owner. ``None`` leaves a field as is."""

    enabled: bool | None = None
    title: str | None = Field(default=None, min_length=1, max_length=HARD_MAX_TITLE)
    schedule: Schedule | None = None
    instruction: str | None = Field(default=None, max_length=HARD_MAX_INSTRUCTION)
    privacy_class: PrivacyClass | None = None
    notification_level: NotificationLevel | None = None
    cooldown_hours: int | None = Field(default=None, ge=1, le=720)


class ModelTaskProposal(_Strict):
    """The ONLY shape a model may propose. There is no enabled flag, cooldown,
    notification level, privacy class, provider, permission, confirmation,
    command or code field: any such key makes the whole proposal invalid."""

    title: str = Field(min_length=1, max_length=HARD_MAX_TITLE)
    task_type: TaskType
    timing_mode: TimingMode
    action: TaskAction
    schedule: Schedule
    condition: ConditionSpec | None = None
    instruction: str = Field(default="", max_length=HARD_MAX_INSTRUCTION)


@dataclass(frozen=True)
class Overview:
    tasks: tuple[ProactiveTask, ...]
    notifications: tuple[ProactiveNotification, ...]
    history: tuple[RunRecord, ...]
    live_runs: int
    scheduler_enabled: bool


def router_worst_case(router: ModelRouter) -> timedelta:
    """The longest one ``ModelRouter.execute`` can take: readiness probes plus
    the ``MAX_ATTEMPTS`` slowest enabled provider-call timeouts."""

    timeouts = sorted(
        (d.timeout_seconds for d in router.registry() if d.enabled), reverse=True
    )
    calls = sum(timeouts[:MAX_ATTEMPTS])
    return timedelta(seconds=calls) + ROUTER_PROBE_ALLOWANCE


class ProactiveService:
    def __init__(
        self,
        *,
        permission_engine: PermissionEngine,
        registry: ConditionRegistry | None = None,
        repository: ProactiveRepository | None = None,
        router: ModelRouter | None = None,
        audit_sink: ProactiveAuditSink | None = None,
        limits: ProactiveLimits | None = None,
        clock: Clock | None = None,
        run_loop: bool = True,
    ) -> None:
        self.limits = limits or ProactiveLimits()
        registry = registry or ConditionRegistry()
        # Every observation and model boundary must be bounded by the run budget
        # (fail closed at construction).
        for entry in registry.entries():
            if timedelta(seconds=entry.max_seconds) > self.limits.run_timeout:
                raise ValueError("an observer may take longer than the run budget")
        if router is not None and router_worst_case(router) > self.limits.run_timeout:
            raise ValueError("the model router may take longer than the run budget")
        self._run_loop = run_loop
        self.repository: ProactiveRepository = (
            repository or InMemoryProactiveRepository(self.limits)
        )
        self.audit = audit_sink or InMemoryProactiveAuditSink()
        self.clock: Clock = clock or SystemClock()
        self._permissions = permission_engine
        self.policy = ProactivePolicy(self.limits, registry)
        self.runner = TaskRunner(
            permission_engine=permission_engine,
            policy=self.policy,
            router=router,
            digest=self._digest,
        )
        self.scheduler = ProactiveScheduler(
            repository=self.repository,
            runner=self.runner,
            policy=self.policy,
            audit=self.audit,
            clock=self.clock,
        )

    # --------------------------------------------------------- plumbing

    def _authorize(
        self,
        principal: Principal,
        action: PermissionAction,
        scope: PermissionScope,
        confirmation_id: str | None = None,
    ) -> tuple[bool, str, str | None, str | None]:
        try:
            decision = self._permissions.evaluate(
                PermissionRequest(
                    principal=principal, action=action, resource=_R, scope=scope
                ),
                confirmation_id=confirmation_id,
            )
        except Exception:
            return False, "deny", "permission_denied", None
        if decision.outcome is DecisionOutcome.ALLOW:
            return True, "allow", None, None
        if decision.outcome is DecisionOutcome.CONFIRM_REQUIRED:
            pending = (
                decision.confirmation.confirmation_id if decision.confirmation else None
            )
            return False, "confirm_required", "confirmation_required", pending
        reason = (
            "confirmation_invalid"
            if decision.reason is not None
            and decision.reason.value == "confirmation_invalid"
            else "permission_denied"
        )
        return False, "deny", reason, None

    def _record(
        self,
        operation: ProactiveOperation,
        principal: Principal,
        permission: str,
        status: str,
        task: ProactiveTask | None = None,
        reason: str | None = None,
    ) -> None:
        self.audit.record(
            new_event(
                operation,
                occurred_at=self.clock(),
                principal_id=principal.id,
                permission=permission,
                status=status,
                task_id=task.task_id if task else None,
                task_type=task.task_type if task else None,
                timing_mode=task.timing_mode if task else None,
                reason_code=reason,
            )
        )

    def _guarded[T](
        self,
        operation: ProactiveOperation,
        principal: Principal,
        action: PermissionAction,
        scope: PermissionScope,
        body: Callable[[str], OpResult[T]],
        confirmation_id: str | None = None,
    ) -> OpResult[T]:
        allowed, permission, reason, pending = self._authorize(
            principal, action, scope, confirmation_id
        )
        if not allowed:
            self._record(operation, principal, permission, "denied", reason=reason)
            return OpResult(permission, False, reason, pending)
        return body(permission)

    def _owned(self, principal: Principal, task_id: str) -> ProactiveTask | None:
        task = self.repository.get_task(task_id)
        if task is None or task.owner != principal:
            return None
        return task

    def _digest(self, principal: Principal) -> str:
        """Deterministic, content-free context for a summary: counts only."""

        tasks = [t for t in self.repository.list_tasks() if t.owner == principal]
        unread = sum(
            1
            for n in self.repository.list_notifications()
            if n.owner_id == principal.id and not n.read
        )
        active = sum(1 for t in tasks if t.enabled and t.status is TaskStatus.ACTIVE)
        return f"Unread Sam notifications: {unread}. Active automations: {active}."

    @staticmethod
    def _scope_for(task_id: str) -> PermissionScope | None:
        try:
            return task_scope(task_id)
        except ValueError:
            return None

    # ------------------------------------------------------------- reads

    def overview(self, principal: Principal) -> OpResult[Overview]:
        def body(permission: str) -> OpResult[Overview]:
            tasks = tuple(
                t for t in self.repository.list_tasks() if t.owner == principal
            )
            owned = {t.task_id for t in tasks}
            data = Overview(
                tasks=tasks,
                notifications=tuple(
                    n
                    for n in self.repository.list_notifications()
                    if n.owner_id == principal.id
                ),
                history=tuple(
                    r for r in self.repository.list_history() if r.task_id in owned
                ),
                live_runs=self.scheduler.live_runs(),
                scheduler_enabled=self.scheduler.enabled,
            )
            self._record(ProactiveOperation.READ, principal, permission, "ok")
            return OpResult(permission, True, data=data)

        return self._guarded(
            ProactiveOperation.READ,
            principal,
            PermissionAction.READ,
            _SCOPE_TASKS,
            body,
        )

    # ------------------------------------------------------------ writes

    def _build(
        self, principal: Principal, draft: TaskDraft, now: datetime
    ) -> ProactiveTask:
        task = ProactiveTask(
            task_id=uuid.uuid4().hex[:24],
            owner=principal,
            title=clean_text(draft.title, self.limits.max_title_chars),
            task_type=draft.task_type,
            timing_mode=draft.timing_mode,
            action=draft.action,
            schedule=draft.schedule,
            condition=draft.condition,
            instruction=clean_text(
                draft.instruction, self.limits.max_instruction_chars
            ),
            privacy_class=draft.privacy_class,
            notification_level=draft.notification_level,
            proposed_action=draft.proposed_action,
            cooldown_hours=draft.cooldown_hours,
            enabled=draft.enabled,
            expires_at=draft.expires_at,
            created_at=now,
            updated_at=now,
        )
        if not task.title:
            raise TaskInvalid("title_required")
        self.policy.validate(task)
        scheduled = self._rescheduled(task, now)  # a future run must exist
        if not scheduled.enabled:
            return scheduled.model_copy(update={"next_run_at": None})
        return scheduled

    def _rescheduled(self, task: ProactiveTask, now: datetime) -> ProactiveTask:
        try:
            upcoming = next_occurrence(task.schedule, now)
        except ScheduleError as error:
            raise TaskInvalid(error.code) from None
        if upcoming is None:
            raise TaskInvalid("no_future_run")
        if self.policy.is_expired(task, now):
            raise TaskInvalid("already_expired")
        return task.model_copy(
            update={"next_run_at": upcoming, "status": TaskStatus.ACTIVE}
        )

    def create_task(
        self, principal: Principal, draft: TaskDraft
    ) -> OpResult[ProactiveTask]:
        def body(permission: str) -> OpResult[ProactiveTask]:
            try:
                task = self._build(principal, draft, self.clock())
                self.repository.add_task(task)
            except (TaskInvalid, RepositoryLimit) as error:
                self._record(
                    ProactiveOperation.CREATE,
                    principal,
                    permission,
                    "rejected",
                    reason=error.code,
                )
                return OpResult(permission, False, error.code)
            except ValidationError:
                self._record(
                    ProactiveOperation.CREATE,
                    principal,
                    permission,
                    "rejected",
                    reason="invalid_task",
                )
                return OpResult(permission, False, "invalid_task")
            self._record(ProactiveOperation.CREATE, principal, permission, "ok", task)
            return OpResult(permission, True, data=task)

        return self._guarded(
            ProactiveOperation.CREATE,
            principal,
            PermissionAction.CREATE,
            _SCOPE_TASKS,
            body,
        )

    def update_task(
        self, principal: Principal, task_id: str, update: TaskUpdate
    ) -> OpResult[ProactiveTask]:
        scope = self._scope_for(task_id)
        if scope is None:
            return OpResult("deny", False, "task_not_found")

        def body(permission: str) -> OpResult[ProactiveTask]:
            task = self._owned(principal, task_id)
            if task is None:
                return OpResult(permission, False, "task_not_found")
            now = self.clock()
            changes: dict[str, Any] = {
                k: v for k, v in update.model_dump(exclude_none=True).items()
            }
            if "schedule" in changes:
                changes["schedule"] = update.schedule
            if "title" in changes:
                changes["title"] = clean_text(
                    changes["title"], self.limits.max_title_chars
                )
            if "instruction" in changes:
                changes["instruction"] = clean_text(
                    changes["instruction"], self.limits.max_instruction_chars
                )
            try:
                edited = ProactiveTask.model_validate(
                    {
                        **task.model_dump(),
                        **changes,
                        "updated_at": now,
                        "version": task.version + 1,
                    }
                )
                self.policy.validate(edited)
                reschedule = "schedule" in changes or (
                    update.enabled is True and not task.enabled
                )
                if edited.enabled and (
                    reschedule or task.status is not TaskStatus.ACTIVE
                ):
                    edited = self._rescheduled(edited, now)
                elif not edited.enabled:
                    edited = edited.model_copy(update={"next_run_at": None})
                self.repository.replace_task(edited)
            except (TaskInvalid, RepositoryLimit) as error:
                self._record(
                    ProactiveOperation.UPDATE,
                    principal,
                    permission,
                    "rejected",
                    task,
                    error.code,
                )
                return OpResult(permission, False, error.code)
            except ValidationError:
                self._record(
                    ProactiveOperation.UPDATE,
                    principal,
                    permission,
                    "rejected",
                    task,
                    "invalid_task",
                )
                return OpResult(permission, False, "invalid_task")
            self._record(ProactiveOperation.UPDATE, principal, permission, "ok", edited)
            return OpResult(permission, True, data=edited)

        return self._guarded(
            ProactiveOperation.UPDATE, principal, PermissionAction.UPDATE, scope, body
        )

    def delete_task(
        self, principal: Principal, task_id: str, confirmation_id: str | None = None
    ) -> OpResult[bool]:
        scope = self._scope_for(task_id)
        if scope is None:
            return OpResult("deny", False, "task_not_found")

        def body(permission: str) -> OpResult[bool]:
            task = self._owned(principal, task_id)
            if task is None:
                return OpResult(permission, False, "task_not_found")
            self.repository.delete_task(task_id)
            self._record(ProactiveOperation.DELETE, principal, permission, "ok", task)
            return OpResult(permission, True, data=True)

        return self._guarded(
            ProactiveOperation.DELETE,
            principal,
            PermissionAction.DELETE,
            scope,
            body,
            confirmation_id,
        )

    def run_now(self, principal: Principal, task_id: str) -> OpResult[str]:
        scope = self._scope_for(task_id)
        if scope is None:
            return OpResult("deny", False, "task_not_found")

        def body(permission: str) -> OpResult[str]:
            task = self._owned(principal, task_id)
            if task is None:
                return OpResult(permission, False, "task_not_found")
            if task.status is not TaskStatus.ACTIVE:
                return OpResult(permission, False, "task_not_active")
            started = self.scheduler.run_now(task_id)
            self._record(
                ProactiveOperation.RUN_NOW, principal, permission, started, task
            )
            if started != "started":
                return OpResult(permission, False, started)
            return OpResult(permission, True, data=started)

        return self._guarded(
            ProactiveOperation.RUN_NOW, principal, PermissionAction.EXECUTE, scope, body
        )

    def update_notification(
        self,
        principal: Principal,
        notification_id: str,
        action: Literal["read", "dismiss"],
    ) -> OpResult[bool]:
        def body(permission: str) -> OpResult[bool]:
            found = next(
                (
                    n
                    for n in self.repository.list_notifications()
                    if n.notification_id == notification_id
                    and n.owner_id == principal.id
                ),
                None,
            )
            if found is None:
                return OpResult(permission, False, "notification_not_found")
            if action == "read":
                self.repository.replace_notification(
                    found.model_copy(update={"read": True})
                )
            else:
                self.repository.remove_notification(notification_id)
            self._record(ProactiveOperation.NOTIFICATION, principal, permission, action)
            return OpResult(permission, True, data=True)

        return self._guarded(
            ProactiveOperation.NOTIFICATION,
            principal,
            PermissionAction.UPDATE,
            _SCOPE_NOTIFICATIONS,
            body,
        )

    # --------------------------------------------------- scheduler switch

    @property
    def scheduler_enabled(self) -> bool:
        return self.scheduler.enabled

    def set_scheduler_enabled(
        self, principal: Principal, enabled: bool
    ) -> OpResult[bool]:
        """The owner's trusted switch for background scheduling (off by default,
        and off again after every restart). It needs PROACTIVE/UPDATE and grants
        nothing: every run is still authorized on its own. There is no model,
        task or proposal path to it."""

        def body(permission: str) -> OpResult[bool]:
            if enabled:
                self.scheduler.enable(loop=self._run_loop)
            else:
                self.scheduler.disable()
            self._record(
                ProactiveOperation.UPDATE,
                principal,
                permission,
                "scheduler_enabled" if enabled else "scheduler_disabled",
            )
            return OpResult(permission, True, data=self.scheduler.enabled)

        return self._guarded(
            ProactiveOperation.UPDATE,
            principal,
            PermissionAction.UPDATE,
            _SCOPE_SCHEDULER,
            body,
        )

    # ----------------------------------------------------- model proposals

    def propose_from_model(
        self, principal: Principal, raw: Mapping[str, Any] | object
    ) -> OpResult[TaskDraft]:
        """Turn untrusted model output into a DISABLED draft, or refuse it.

        Nothing is stored and nothing is scheduled: the owner must create the
        draft (CREATE permission) and then enable it (UPDATE permission)."""

        try:
            proposal = ModelTaskProposal.model_validate(raw)
            draft = TaskDraft(
                **proposal.model_dump(exclude={"schedule", "condition"}),
                schedule=proposal.schedule,
                condition=proposal.condition,
                enabled=False,
            )
            self.policy.validate(self._build_preview(principal, draft))
        except (ValidationError, TaskInvalid, TypeError, ValueError) as error:
            code = error.code if isinstance(error, TaskInvalid) else "invalid_proposal"
            self._record(
                ProactiveOperation.PROPOSAL, principal, "none", "rejected", reason=code
            )
            return OpResult("none", False, code)
        self._record(ProactiveOperation.PROPOSAL, principal, "none", "draft")
        return OpResult("none", True, data=draft)

    def _build_preview(self, principal: Principal, draft: TaskDraft) -> ProactiveTask:
        now = self.clock()
        return ProactiveTask(
            task_id="preview",
            owner=principal,
            title=draft.title,
            task_type=draft.task_type,
            timing_mode=draft.timing_mode,
            action=draft.action,
            schedule=draft.schedule,
            condition=draft.condition,
            instruction=draft.instruction,
            enabled=False,
            created_at=now,
            updated_at=now,
        )


__all__ = [
    "ModelTaskProposal",
    "OpResult",
    "Overview",
    "ProactiveService",
    "TaskDraft",
    "TaskUpdate",
]
