"""Shared fixtures for the proactive-engine tests.

Everything is deterministic: a ``ManualClock`` (no test sleeps for real
time), a real PermissionEngine with explicit grants, fake observers, and the
Phase 13 router over fake providers. No test contacts a real provider, sends
anything or touches an external service.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sam.models.models import PrivacyClass
from sam.models.router import ModelRouter
from sam.permissions.audit import InMemoryAuditSink
from sam.permissions.confirmation import InMemoryConfirmationProvider
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    PermissionAction,
    PermissionGrant,
    PermissionResource,
    PermissionScope,
    Principal,
    PrincipalKind,
)
from sam.permissions.store import InMemoryPermissionStore
from sam.proactive.clock import ManualClock
from sam.proactive.conditions import (
    ConditionEntry,
    ConditionRegistry,
    Observation,
    RequiredPermission,
)
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
from sam.proactive.observers import deadline_entry
from sam.proactive.policy import ProactiveLimits
from sam.proactive.service import OpResult, ProactiveService, TaskDraft

START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)  # a Monday, 09:00 UTC (London GMT)
OWNER = Principal(kind=PrincipalKind.USER, id="owner")
STRANGER = Principal(kind=PrincipalKind.USER, id="stranger")
ALL_PROACTIVE = (
    PermissionAction.READ,
    PermissionAction.CREATE,
    PermissionAction.UPDATE,
    PermissionAction.EXECUTE,
    PermissionAction.DELETE,
)


class FlagObserver:
    """A watch observer whose answer the test sets. It can also block (to
    test concurrency and timeouts) or fail."""

    def __init__(self) -> None:
        self.met: bool | None = False
        self.value: str | None = "v1"
        self.fail = False
        self.calls = 0
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def observe(
        self, principal: Principal, params: Mapping[str, str], now: datetime
    ) -> Observation:
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.fail:
            raise RuntimeError("source down")
        return Observation(met=self.met, value_key=self.value, summary="Flag changed.")


def flag_entry(
    observer: FlagObserver,
    *,
    condition_id: str = "flag",
    permission: RequiredPermission | None = None,
) -> ConditionEntry:
    return ConditionEntry(
        condition_id=condition_id,
        label="flag",
        permission=permission
        or RequiredPermission(
            PermissionResource.PROACTIVE,
            PermissionAction.READ,
            PermissionScope.from_path(f"proactive/conditions/{condition_id}"),
        ),
        observer=observer,
        optional_params=frozenset({"note"}),
    )


@dataclass
class Rig:
    service: ProactiveService
    engine: PermissionEngine
    grants: InMemoryPermissionStore
    confirmations: InMemoryConfirmationProvider
    permission_audit: InMemoryAuditSink
    clock: ManualClock
    flag: FlagObserver
    grant_ids: dict[tuple[PermissionResource, PermissionAction], str] = field(
        default_factory=dict
    )

    # ------------------------------------------------------------ helpers

    def grant(
        self,
        resource: PermissionResource,
        action: PermissionAction,
        scope: str = "proactive",
        *,
        principal: Principal = OWNER,
        always_confirm: bool = False,
    ) -> str:
        grant_id = f"g-{resource.value}-{action.value}-{len(self.grant_ids)}"
        now = self.clock()
        self.grants.create_grant(
            PermissionGrant(
                grant_id=grant_id,
                principal=principal,
                resource=resource,
                action=action,
                scope=(
                    PermissionScope.identifier(scope)
                    if ":" in scope
                    else PermissionScope.from_path(scope)
                ),
                always_require_confirmation=always_confirm,
                created_at=now,
                updated_at=now,
            )
        )
        self.grant_ids[(resource, action)] = grant_id
        return grant_id

    def revoke(self, resource: PermissionResource, action: PermissionAction) -> None:
        self.grants.revoke_grant(self.grant_ids[(resource, action)], now=self.clock())

    def create(self, draft: TaskDraft) -> ProactiveTask:
        result = self.service.create_task(OWNER, draft)
        assert result.ok and result.data is not None, result.reason
        return result.data

    def task(self, task_id: str) -> ProactiveTask:
        found = self.service.repository.get_task(task_id)
        assert found is not None
        return found

    def tick(self) -> int:
        count = self.service.scheduler.tick()
        assert self.service.scheduler.wait_idle(10)
        return count

    def advance_and_tick(self, **delta: float) -> int:
        self.clock.advance(timedelta(**delta))
        return self.tick()

    def notifications(self) -> tuple[ProactiveNotification, ...]:
        return self.service.repository.list_notifications()

    def history(self, task_id: str | None = None) -> tuple[RunRecord, ...]:
        records = self.service.repository.list_history()
        return tuple(r for r in records if task_id is None or r.task_id == task_id)

    def overview(self) -> OpResult[Any]:
        return self.service.overview(OWNER)


def inject_unvetted(rig: Rig, entry: ConditionEntry) -> None:
    """Simulate a MIS-registered observer: bypass ``ConditionRegistry.of`` (which
    refuses any non-READ observer) to prove the runner still refuses it."""

    current = {e.condition_id: e for e in rig.service.policy.registry.entries()}
    current[entry.condition_id] = entry
    rig.service.policy.registry = ConditionRegistry(current)


def make_rig(
    *,
    router: ModelRouter | None = None,
    limits: ProactiveLimits | None = None,
    extra_entries: tuple[ConditionEntry, ...] = (),
    grant_all: bool = True,
    start: datetime = START,
    scheduler_on: bool = True,
) -> Rig:
    clock = ManualClock(start)
    grants = InMemoryPermissionStore()
    confirmations = InMemoryConfirmationProvider()
    permission_audit = InMemoryAuditSink()
    engine = PermissionEngine(
        store=grants,
        confirmation_provider=confirmations,
        audit_sink=permission_audit,
        clock=clock,
    )
    flag = FlagObserver()
    registry = ConditionRegistry.of(
        (flag_entry(flag), deadline_entry(), *extra_entries)
    )
    service = ProactiveService(
        permission_engine=engine,
        registry=registry,
        router=router,
        limits=limits,
        clock=clock,
        run_loop=False,  # tests tick explicitly; no background thread
    )
    rig = Rig(service, engine, grants, confirmations, permission_audit, clock, flag)
    if grant_all:
        for action in ALL_PROACTIVE:
            rig.grant(PermissionResource.PROACTIVE, action)
    if scheduler_on:
        # The owner's switch (off by default); tests without grants enable the
        # scheduler directly, since the switch itself needs PROACTIVE/UPDATE.
        rig.service.scheduler.enable(loop=False)
    return rig


# ---------------------------------------------------------------- drafts


def schedule(
    *,
    tz: str = "Europe/London",
    start_date: date = date(2026, 1, 5),
    at: time | None = time(10, 0),
    daypart: Daypart | None = None,
    frequency: Frequency = Frequency.NONE,
    interval: int = 1,
    weekdays: tuple[int, ...] = (),
    until: date | None = None,
    max_runs: int | None = None,
) -> Schedule:
    return Schedule(
        timezone=tz,
        start_date=start_date,
        time_of_day=at,
        daypart=daypart,
        frequency=frequency,
        interval=interval,
        weekdays=weekdays,
        until=until,
        max_runs=max_runs,
    )


def reminder(
    title: str = "Call the lab",
    *,
    sched: Schedule | None = None,
    recurring: bool = False,
    flexible: bool = False,
    instruction: str = "",
    **extra: Any,
) -> TaskDraft:
    if sched is None:
        if flexible:
            sched = schedule(
                at=None,
                daypart=Daypart.MORNING,
                frequency=Frequency.DAILY if recurring else Frequency.NONE,
                start_date=date(2026, 1, 6),
            )
        else:
            sched = schedule(frequency=Frequency.DAILY if recurring else Frequency.NONE)
    return TaskDraft(
        title=title,
        task_type=TaskType.RECURRING if recurring else TaskType.ONE_TIME,
        timing_mode=(
            TimingMode.FLEXIBLE_SCHEDULE if flexible else TimingMode.EXACT_SCHEDULE
        ),
        action=TaskAction.REMINDER,
        schedule=sched,
        instruction=instruction,
        **extra,
    )


def summary(
    instruction: str = "Summarize what needs my attention today.",
    *,
    privacy: PrivacyClass = PrivacyClass.PERSONAL,
    **extra: Any,
) -> TaskDraft:
    return TaskDraft(
        title="Morning summary",
        task_type=TaskType.RECURRING,
        timing_mode=TimingMode.EXACT_SCHEDULE,
        action=TaskAction.SUMMARY,
        schedule=schedule(frequency=Frequency.DAILY),
        instruction=instruction,
        privacy_class=privacy,
        **extra,
    )


def watch(
    condition_id: str = "flag",
    *,
    params: dict[str, str] | None = None,
    semantics: TriggerSemantics = TriggerSemantics.BECOMES_TRUE,
    cooldown_hours: int = 24,
    every_hours: int = 1,
    level: NotificationLevel = NotificationLevel.NOTIFY_OWNER,
    proposed: ProposedAction = ProposedAction.NONE,
    **extra: Any,
) -> TaskDraft:
    return TaskDraft(
        title="Watch the flag",
        task_type=TaskType.CONDITION_WATCH,
        timing_mode=TimingMode.CONDITION_WATCH,
        action=TaskAction.WATCH,
        schedule=schedule(
            at=time(0, 0), frequency=Frequency.HOURLY, interval=every_hours
        ),
        condition=ConditionSpec(
            condition_id=condition_id, params=params or {}, semantics=semantics
        ),
        cooldown_hours=cooldown_hours,
        notification_level=level,
        proposed_action=proposed,
        **extra,
    )


__all__ = [
    "ALL_PROACTIVE",
    "OWNER",
    "START",
    "STRANGER",
    "FlagObserver",
    "Rig",
    "flag_entry",
    "inject_unvetted",
    "make_rig",
    "reminder",
    "schedule",
    "summary",
    "watch",
]
