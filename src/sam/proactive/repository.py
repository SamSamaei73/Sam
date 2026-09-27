"""Persistence contract for the proactive engine.

``ProactiveRepository`` is an abstraction; Phase 15 ships ONLY
``InMemoryProactiveRepository``: no SQLite, no file, no cloud persistence.
Tasks, watch state, notifications and history live for the process lifetime
and are gone after a restart (so there is never a backlog to replay).

Every collection is bounded: task counts are enforced on insert and edit, and
notifications and history keep only the newest entries.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Iterable
from threading import RLock
from typing import Protocol

from sam.proactive.dedup import DedupLedger
from sam.proactive.models import (
    ConditionState,
    ProactiveNotification,
    ProactiveTask,
    RunRecord,
)
from sam.proactive.policy import ProactiveLimits


class RepositoryLimit(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProactiveRepository(Protocol):
    dedup: DedupLedger

    def add_task(self, task: ProactiveTask) -> None: ...
    def replace_task(self, task: ProactiveTask) -> None: ...
    def get_task(self, task_id: str) -> ProactiveTask | None: ...
    def list_tasks(self) -> tuple[ProactiveTask, ...]: ...
    def delete_task(self, task_id: str) -> bool: ...
    def get_condition(self, task_id: str) -> ConditionState: ...
    def set_condition(self, task_id: str, state: ConditionState) -> None: ...
    def add_notification(self, notification: ProactiveNotification) -> None: ...
    def list_notifications(self) -> tuple[ProactiveNotification, ...]: ...
    def replace_notification(self, notification: ProactiveNotification) -> None: ...
    def remove_notification(self, notification_id: str) -> bool: ...
    def add_history(self, record: RunRecord) -> None: ...
    def list_history(self) -> tuple[RunRecord, ...]: ...


class InMemoryProactiveRepository:
    def __init__(self, limits: ProactiveLimits | None = None) -> None:
        self.limits = limits or ProactiveLimits()
        self._tasks: dict[str, ProactiveTask] = {}
        self._conditions: dict[str, ConditionState] = {}
        self._notifications: OrderedDict[str, ProactiveNotification] = OrderedDict()
        self._history: deque[RunRecord] = deque(maxlen=self.limits.max_history)
        self.dedup = DedupLedger(
            window=self.limits.dedup_window, max_keys=self.limits.max_dedup_keys
        )
        self._lock = RLock()

    # ------------------------------------------------------------- tasks

    def _check_counts(self, tasks: Iterable[ProactiveTask]) -> None:
        items = list(tasks)
        if len(items) > self.limits.max_tasks:
            raise RepositoryLimit("too_many_tasks")
        if sum(1 for t in items if t.enabled) > self.limits.max_enabled_tasks:
            raise RepositoryLimit("too_many_enabled_tasks")

    def add_task(self, task: ProactiveTask) -> None:
        with self._lock:
            if task.task_id in self._tasks:
                raise RepositoryLimit("duplicate_task")
            self._check_counts([*self._tasks.values(), task])
            self._tasks[task.task_id] = task

    def replace_task(self, task: ProactiveTask) -> None:
        with self._lock:
            if task.task_id not in self._tasks:
                raise KeyError(task.task_id)
            others = [t for k, t in self._tasks.items() if k != task.task_id]
            self._check_counts([*others, task])
            self._tasks[task.task_id] = task

    def get_task(self, task_id: str) -> ProactiveTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self) -> tuple[ProactiveTask, ...]:
        with self._lock:
            return tuple(sorted(self._tasks.values(), key=lambda t: t.created_at))

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            self._conditions.pop(task_id, None)
            return self._tasks.pop(task_id, None) is not None

    # --------------------------------------------------------- condition

    def get_condition(self, task_id: str) -> ConditionState:
        with self._lock:
            return self._conditions.get(task_id, ConditionState())

    def set_condition(self, task_id: str, state: ConditionState) -> None:
        with self._lock:
            if task_id in self._tasks:
                self._conditions[task_id] = state

    # ----------------------------------------------------- notifications

    def add_notification(self, notification: ProactiveNotification) -> None:
        with self._lock:
            self._notifications[notification.notification_id] = notification
            while len(self._notifications) > self.limits.max_notifications:
                self._notifications.popitem(last=False)

    def list_notifications(self) -> tuple[ProactiveNotification, ...]:
        with self._lock:
            return tuple(reversed(self._notifications.values()))

    def replace_notification(self, notification: ProactiveNotification) -> None:
        with self._lock:
            if notification.notification_id in self._notifications:
                self._notifications[notification.notification_id] = notification

    def remove_notification(self, notification_id: str) -> bool:
        with self._lock:
            return self._notifications.pop(notification_id, None) is not None

    # ----------------------------------------------------------- history

    def add_history(self, record: RunRecord) -> None:
        with self._lock:
            self._history.append(record)

    def list_history(self) -> tuple[RunRecord, ...]:
        with self._lock:
            return tuple(reversed(self._history))


__all__ = [
    "InMemoryProactiveRepository",
    "ProactiveRepository",
    "RepositoryLimit",
]
