"""A small, bounded, Sam-owned scheduler. No system cron, no thread per task.

* DISABLED BY DEFAULT. A new scheduler runs nothing (``tick`` and ``run_now``
  do no work) until it is explicitly enabled by the owner's trusted settings
  path (``ProactiveService.set_scheduler_enabled``). Enabling starts one loop
  thread; disabling stops it. Enabling grants no permission of any kind.
* ``tick()`` is the whole algorithm and is deterministic for a given clock:
  it reaps timed-out runs, then walks the due tasks in (next_run_at, task_id)
  order. The background loop only calls ``tick()`` and then waits on an Event
  (``ProactiveLimits.tick_seconds``, at least 5 s): no busy loop.
* Work is bounded three ways: at most ``max_dispatch_per_tick`` dispatches per
  tick, at most ``max_concurrent_runs`` live runs (each run is one daemon
  worker thread, and a LIVE thread always counts, finished or not), and a task
  that is still running is never started again. A due task that
  cannot start because the pool is full simply stays due (backpressure); a
  recurring task that comes due while its previous run is still going is
  COALESCED (skipped to its next occurrence), never run twice at once.
* Missed runs (SKIP_TO_NEXT): a recurring task that is later than its grace
  (1 h for an exact time, the end of the daypart for a flexible one) is not
  replayed; one history record is written and the next occurrence AFTER NOW
  is scheduled, however many occurrences were missed. A one-time task that is
  later than its grace is marked MISSED (with a notification unless it is
  silent). A watch never "misses": it simply checks once now.
* Timeouts: every run has a deadline and a cancel Event. A run past its
  deadline is recorded as a TEMPORARY_FAILURE (``run_timeout``) and cancelled
  COOPERATIVELY: the runner checks the Event between every step, so after the
  blocking call it was in returns, it authorizes, observes, routes and writes
  nothing more. Python cannot kill a running thread, so until that thread
  actually exits: the task stays IN FLIGHT (it is never started again), the run
  keeps its concurrency slot (the bound on live threads always holds), and
  whatever it eventually returns is discarded: no notification, no dedup,
  cooldown or condition state, no task update, no success audit. Every
  observation and model boundary a run uses has its own finite timeout no
  longer than the run budget (checked when the service is built). Worker
  threads are daemon threads, so a hung one never blocks process exit.
* Deleting a task stops its future runs; a run already in flight for a deleted
  task surfaces nothing.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event, RLock, Thread, current_thread
from typing import Literal

from sam.proactive.audit import ProactiveAuditSink, ProactiveOperation, new_event
from sam.proactive.clock import Clock
from sam.proactive.cooldown import cooldown_allows
from sam.proactive.models import (
    ChangeKind,
    ConditionState,
    HistoryKind,
    NotificationReason,
    ProactiveNotification,
    ProactiveTask,
    ProposedAction,
    RunFailure,
    RunRecord,
    RunResult,
    RunTrigger,
    TaskStatus,
    TaskType,
    TimingMode,
)
from sam.proactive.notifications import SafeNotification, Withheld, safe_notification
from sam.proactive.policy import ProactiveNotificationPolicy, ProactivePolicy
from sam.proactive.repository import ProactiveRepository
from sam.proactive.runner import NotificationDraft, RunOutcome, TaskRunner
from sam.proactive.schedule import ScheduleError, next_occurrence, window_end

logger = logging.getLogger(__name__)

RunStart = Literal[
    "started", "already_running", "busy", "not_found", "stopped", "scheduler_disabled"
]


@dataclass
class _Live:
    run_id: str
    task_id: str
    trigger: RunTrigger
    started_at: datetime
    deadline: datetime
    cancel: Event = field(default_factory=Event)
    thread: Thread | None = None
    abandoned: bool = False


class ProactiveScheduler:
    def __init__(
        self,
        *,
        repository: ProactiveRepository,
        runner: TaskRunner,
        policy: ProactivePolicy,
        audit: ProactiveAuditSink,
        clock: Clock,
    ) -> None:
        self._repo = repository
        self._runner = runner
        self._policy = policy
        self._limits = policy.limits
        self._audit = audit
        self._clock = clock
        self._notify = ProactiveNotificationPolicy()
        self._live: dict[str, _Live] = {}
        self._busy: set[str] = set()
        self._lock = RLock()
        self._enabled = False  # fail closed: nothing runs until the owner enables it
        self._stop = Event()
        self._thread: Thread | None = None
        self._closed = False

    # --------------------------------------------------------- lifecycle

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled and not self._closed

    def enable(self, *, loop: bool = True) -> None:
        """Allow scheduled work. With ``loop`` a single background thread calls
        ``tick``; without it (tests) the caller ticks explicitly."""

        with self._lock:
            if self._closed:
                return
            self._enabled = True
            if not loop or (self._thread is not None and self._thread.is_alive()):
                return
            self._stop = Event()
            self._thread = Thread(
                target=self._loop,
                args=(self._stop,),
                name="sam-proactive-scheduler",
                daemon=True,
            )
            self._thread.start()

    def disable(self, timeout: float = 5.0) -> None:
        """Stop starting new runs. Runs already in flight follow the timeout
        policy; they are not abandoned just because scheduling was turned off."""

        with self._lock:
            self._enabled = False
            stop, thread = self._stop, self._thread
            self._thread = None
        stop.set()
        if thread is not None and thread is not current_thread():
            thread.join(timeout)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Application shutdown: disable, and cooperatively cancel live runs."""

        self.disable(timeout)
        with self._lock:
            self._closed = True
            for live in self._live.values():
                live.cancel.set()

    def _loop(self, stop: Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # never let one bad tick stop the scheduler
                logger.warning("proactive scheduler tick failed")
            stop.wait(self._limits.tick_seconds)

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def live_runs(self) -> int:
        with self._lock:
            return len(self._live)

    def is_busy(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._busy

    def live_threads(self) -> int:
        """Worker threads that are actually alive (timed out or not)."""

        with self._lock:
            return sum(
                1
                for e in self._live.values()
                if e.thread is not None and e.thread.is_alive()
            )

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Wait (real time, bounded) until no run is in flight. For tests and
        shutdown only; scheduling never depends on it."""

        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                threads = [
                    e.thread for e in self._live.values() if e.thread is not None
                ]
            if not threads:
                return True
            for thread in threads:
                thread.join(max(0.0, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                with self._lock:
                    return not self._live

    # -------------------------------------------------------------- tick

    def tick(self) -> int:
        now = self._clock()
        dispatched = 0
        with self._lock:
            if self._closed or not self._enabled:
                return 0  # disabled: zero task execution
            self._reap(now)
            candidates = [
                t
                for t in self._repo.list_tasks()
                if t.enabled and t.status is TaskStatus.ACTIVE
            ]
            for task in candidates:
                if (
                    self._policy.is_expired(task, now)
                    and task.task_id not in self._busy
                ):
                    self._finish_task(task, TaskStatus.EXPIRED, now)
                    self._history(
                        task, HistoryKind.EXPIRED, now, failure=RunFailure.EXPIRED
                    )
            due = sorted(
                (
                    t
                    for t in self._repo.list_tasks()
                    if t.enabled
                    and t.status is TaskStatus.ACTIVE
                    and t.next_run_at is not None
                    and t.next_run_at <= now
                ),
                key=lambda t: (t.next_run_at, t.task_id),
            )
            for task in due:
                if task.task_id in self._busy:
                    self._coalesce(task, now)
                    continue
                if self._missed(task, now):
                    self._handle_missed(task, now)
                    continue
                if (
                    dispatched >= self._limits.max_dispatch_per_tick
                    or len(self._live) >= self._limits.max_concurrent_runs
                ):
                    break  # backpressure: the rest stay due for the next tick
                self._dispatch(task, now, RunTrigger.SCHEDULED)
                dispatched += 1
        return dispatched

    def run_now(self, task_id: str) -> RunStart:
        """Start a manual run now (the service has already authorized it)."""

        now = self._clock()
        with self._lock:
            if self._closed:
                return "stopped"
            if not self._enabled:
                return "scheduler_disabled"
            task = self._repo.get_task(task_id)
            if task is None:
                return "not_found"
            if task_id in self._busy:
                return "already_running"
            if len(self._live) >= self._limits.max_concurrent_runs:
                return "busy"
            self._dispatch(task, now, RunTrigger.MANUAL)
            return "started"

    # ---------------------------------------------------------- dispatch

    def _next_after(self, task: ProactiveTask, now: datetime) -> datetime | None:
        if task.task_type is TaskType.ONE_TIME:
            return None
        schedule = task.schedule
        if (
            schedule.max_runs is not None
            and task.runs_completed + 1 >= schedule.max_runs
        ):
            return None
        return next_occurrence(schedule, now)

    def _dispatch(
        self, task: ProactiveTask, now: datetime, trigger: RunTrigger
    ) -> None:
        occurrence = task.next_run_at if trigger is RunTrigger.SCHEDULED else None
        if trigger is RunTrigger.SCHEDULED:
            try:
                upcoming = self._next_after(task, now)
            except ScheduleError:
                self._finish_task(task, TaskStatus.INVALID, now)
                self._history(
                    task,
                    HistoryKind.INVALID,
                    now,
                    failure=RunFailure.INVALID_TASK,
                    reason="invalid_schedule",
                )
                return
            task = task.model_copy(update={"next_run_at": upcoming, "last_run_at": now})
        else:
            task = task.model_copy(update={"last_run_at": now})
        self._repo.replace_task(task)
        state = self._repo.get_condition(task.task_id)
        live = _Live(
            run_id=uuid.uuid4().hex,
            task_id=task.task_id,
            trigger=trigger,
            started_at=now,
            deadline=now + self._limits.run_timeout,
        )
        # Registered BEFORE the thread starts, so the slot and the task's
        # in-flight mark exist for the whole life of the thread.
        self._live[live.run_id] = live
        self._busy.add(task.task_id)
        live.thread = Thread(
            target=self._work,
            args=(live, task, state, now, trigger, occurrence),
            name="sam-proactive-run",
            daemon=True,
        )
        live.thread.start()

    def _work(
        self,
        live: _Live,
        task: ProactiveTask,
        state: ConditionState,
        now: datetime,
        trigger: RunTrigger,
        occurrence: datetime | None,
    ) -> None:
        try:
            outcome = self._runner.run(
                task, state, now, trigger, occurrence, cancel=live.cancel
            )
        except Exception:
            outcome = RunOutcome.failed(RunFailure.TEMPORARY_FAILURE, "run_error")
        self._finalize(live.run_id, outcome)

    def _finalize(self, run_id: str, outcome: RunOutcome) -> None:
        # The ONLY place a live run is released: when its thread is finishing.
        with self._lock:
            live = self._live.pop(run_id, None)
            if live is None:
                return
            self._busy.discard(live.task_id)
            if live.abandoned or outcome.cancelled or live.cancel.is_set():
                return  # timed out or cancelled: every late result is discarded
            task = self._repo.get_task(live.task_id)
            if task is None:
                return  # deleted mid-run: nothing is surfaced
            self._apply(task, live, outcome)

    def _apply(self, task: ProactiveTask, live: _Live, outcome: RunOutcome) -> None:
        completed = self._clock()
        previous = self._repo.get_condition(task.task_id)
        if outcome.condition_state is not None:
            self._repo.set_condition(task.task_id, outcome.condition_state)
        notified = False
        reason = outcome.reason_code
        if outcome.failure is not None:
            result = RunResult.ERROR_RECORDED
        elif outcome.draft is None:
            result = RunResult.NO_NOTIFICATION
        else:
            result, reason, notified = self._decide(
                task, outcome.draft, previous, completed
            )
        status = task.status
        if outcome.failure is RunFailure.INVALID_TASK:
            status = TaskStatus.INVALID
        elif outcome.failure is RunFailure.EXPIRED:
            status = TaskStatus.EXPIRED
        elif (
            live.trigger is RunTrigger.SCHEDULED
            and task.next_run_at is None
            and task.status is TaskStatus.ACTIVE
        ):
            status = TaskStatus.COMPLETED
        done = status is not TaskStatus.ACTIVE
        self._repo.replace_task(
            task.model_copy(
                update={
                    "runs_completed": task.runs_completed + 1,
                    "last_result": result,
                    "last_failure": outcome.failure,
                    "last_reason": reason,
                    "status": status,
                    "enabled": task.enabled and not done,
                    "next_run_at": None if done else task.next_run_at,
                }
            )
        )
        self._history(
            task,
            HistoryKind.RUN,
            live.started_at,
            completed=completed,
            trigger=live.trigger,
            result=result,
            failure=outcome.failure,
            change=outcome.change,
            reason=reason,
            notified=notified,
            provider=outcome.provider_id,
            permission=outcome.permission,
        )

    def _decide(
        self,
        task: ProactiveTask,
        draft: NotificationDraft,
        previous: ConditionState,
        now: datetime,
    ) -> tuple[RunResult, str | None, bool]:
        """Dedup, cooldown and the owner's notification level. Sam decides."""

        if not self._notify.surfaces(task.notification_level):
            return RunResult.NO_NOTIFICATION, "silent", False
        if self._repo.dedup.is_duplicate(draft.dedup_key, now):
            return RunResult.NO_NOTIFICATION, "duplicate", False
        if draft.cooldown_applies and not cooldown_allows(
            previous.last_notified_at, now, task.cooldown_hours
        ):
            return RunResult.NO_NOTIFICATION, "cooldown", False
        safe = self._notify_owner(task, draft, now)
        if draft.cooldown_applies:
            state = self._repo.get_condition(task.task_id)
            self._repo.set_condition(
                task.task_id, state.model_copy(update={"last_notified_at": now})
            )
        reason = (
            safe.reason.value
            if safe.withheld is Withheld.NONE
            else f"withheld_{safe.withheld.value}"
        )
        if safe.proposed_action is not ProposedAction.NONE:
            return RunResult.ACTION_PROPOSED, reason, True
        return RunResult.NOTIFICATION_CREATED, reason, True

    def _notify_owner(
        self, task: ProactiveTask, draft: NotificationDraft, now: datetime
    ) -> SafeNotification:
        """The single write path for notifications, through the final gate."""

        safe = safe_notification(
            title=draft.title,
            summary=draft.summary,
            source_label=draft.source_label,
            reason=draft.reason,
            proposed_action=draft.proposed_action,
            privacy=draft.privacy,
            limits=self._limits,
        )
        self._repo.dedup.record(draft.dedup_key, now)
        self._repo.add_notification(
            ProactiveNotification(
                notification_id=uuid.uuid4().hex,
                task_id=task.task_id,
                owner_id=task.owner.id,
                title=safe.title,
                summary=safe.summary,
                created_at=now,
                reason_code=safe.reason,
                importance=self._notify.importance(task.notification_level),
                source_label=safe.source_label,
                proposed_action=safe.proposed_action,
                privacy_class=safe.privacy,
            )
        )
        return safe

    # ------------------------------------------------ timing edge cases

    def _missed(self, task: ProactiveTask, now: datetime) -> bool:
        assert task.next_run_at is not None
        if task.timing_mode is TimingMode.CONDITION_WATCH:
            return False
        if task.timing_mode is TimingMode.FLEXIBLE_SCHEDULE:
            end = window_end(task.schedule, task.next_run_at)
            return end is not None and now >= end
        return now - task.next_run_at > self._limits.overdue_threshold

    def _handle_missed(self, task: ProactiveTask, now: datetime) -> None:
        if task.task_type is TaskType.ONE_TIME:
            occurrence = task.next_run_at
            self._finish_task(task, TaskStatus.MISSED, now)
            self._history(task, HistoryKind.MISSED, now, reason="missed")
            if self._notify.surfaces(task.notification_level) and occurrence:
                draft = NotificationDraft(
                    title=task.title,
                    summary="This reminder was due while Sam was not running.",
                    reason=NotificationReason.REMINDER_MISSED,
                    source_label="reminder",
                    dedup_key=f"missed:{task.task_id}:{occurrence.isoformat()}",
                    proposed_action=ProposedAction.NONE,
                    cooldown_applies=False,
                    privacy=task.privacy_class,
                )
                if not self._repo.dedup.is_duplicate(draft.dedup_key, now):
                    self._notify_owner(task, draft, now)
            return
        # SKIP_TO_NEXT: one record, no replay, whatever the backlog.
        try:
            upcoming = next_occurrence(task.schedule, now)
        except ScheduleError:
            self._finish_task(task, TaskStatus.INVALID, now)
            return
        self._history(task, HistoryKind.SKIPPED_MISSED, now, reason="skip_to_next")
        if upcoming is None:
            self._finish_task(task, TaskStatus.COMPLETED, now)
        else:
            self._repo.replace_task(task.model_copy(update={"next_run_at": upcoming}))

    def _coalesce(self, task: ProactiveTask, now: datetime) -> None:
        if task.task_type is TaskType.ONE_TIME:
            return  # stays due; runs when the in-flight run finishes
        try:
            upcoming = next_occurrence(task.schedule, now)
        except ScheduleError:
            return
        self._repo.replace_task(task.model_copy(update={"next_run_at": upcoming}))
        self._history(task, HistoryKind.COALESCED, now, reason="already_running")

    def _reap(self, now: datetime) -> None:
        for live in self._live.values():
            if live.abandoned or now < live.deadline:
                continue
            if live.thread is not None and not live.thread.is_alive():
                continue
            # Cancel cooperatively and keep the run LIVE: its slot and the
            # task's in-flight mark are released only when the thread exits.
            live.abandoned = True
            live.cancel.set()
            task = self._repo.get_task(live.task_id)
            if task is None:
                continue
            self._repo.replace_task(
                task.model_copy(
                    update={
                        "last_result": RunResult.ERROR_RECORDED,
                        "last_failure": RunFailure.TEMPORARY_FAILURE,
                        "last_reason": "run_timeout",
                    }
                )
            )
            self._history(
                task,
                HistoryKind.TIMED_OUT,
                live.started_at,
                completed=now,
                trigger=live.trigger,
                result=RunResult.ERROR_RECORDED,
                failure=RunFailure.TEMPORARY_FAILURE,
                reason="run_timeout",
            )

    def _finish_task(
        self, task: ProactiveTask, status: TaskStatus, now: datetime
    ) -> None:
        self._repo.replace_task(
            task.model_copy(
                update={
                    "status": status,
                    "enabled": False,
                    "next_run_at": None,
                    "updated_at": now,
                }
            )
        )

    # ------------------------------------------------------------ record

    def _history(
        self,
        task: ProactiveTask,
        kind: HistoryKind,
        started: datetime,
        *,
        completed: datetime | None = None,
        trigger: RunTrigger | None = None,
        result: RunResult | None = None,
        failure: RunFailure | None = None,
        change: ChangeKind | None = None,
        reason: str | None = None,
        notified: bool = False,
        provider: str | None = None,
        permission: str = "system",
    ) -> None:
        self._repo.add_history(
            RunRecord(
                record_id=uuid.uuid4().hex,
                task_id=task.task_id,
                kind=kind,
                task_type=task.task_type,
                timing_mode=task.timing_mode,
                trigger=trigger,
                started_at=started,
                completed_at=completed or started,
                result=result,
                failure=failure,
                change=change,
                reason_code=reason,
                notification_created=notified,
                provider_id=provider,
            )
        )
        self._audit.record(
            new_event(
                ProactiveOperation.RUN,
                occurred_at=completed or started,
                principal_id=task.owner.id,
                permission=permission,
                status=kind.value,
                task_id=task.task_id,
                task_type=task.task_type,
                timing_mode=task.timing_mode,
                trigger=trigger,
                result=result,
                failure=failure,
                change=change,
                reason_code=reason,
                notification_created=notified,
                provider_id=provider,
                started_at=started,
                completed_at=completed or started,
            )
        )


__all__ = ["ProactiveScheduler", "RunStart"]
