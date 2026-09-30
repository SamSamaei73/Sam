"""Durable Proactive repository (Phase 17).

The Phase 15 ``InMemoryProactiveRepository`` (bounded tasks, notifications
and history), write-through mirrored to the ``documents`` table, plus a
durable dedup ledger. What survives a restart:

* tasks with their schedules, explicit timezones, enabled flags and
  ``next_run_at``: a restart never replays a backlog, because missed recurring
  runs still SKIP_TO_NEXT at the next tick;
* each watch's condition state, including ``last_notified_at`` (the cooldown);
* the dedup ledger: an event already notified is not notified again because
  Sam restarted;
* the bounded notification inbox and run history (same retention limits).

Never persisted: confirmations, step-up state, grants or any reusable
authorization artifact (none live in this repository).
"""

from __future__ import annotations

from collections import OrderedDict, deque
from datetime import datetime
from typing import Any

from sam.proactive.dedup import DedupLedger
from sam.proactive.models import (
    ConditionState,
    ProactiveNotification,
    ProactiveTask,
    RunRecord,
)
from sam.proactive.policy import ProactiveLimits
from sam.proactive.repository import InMemoryProactiveRepository
from sam.storage.database import Database
from sam.storage.documents import DocumentMirror, State, WriteThrough, durable


class _DurableDedup(DedupLedger):
    """The Phase 15 ledger; every ``record`` is persisted with the repository."""

    def __init__(self, owner: SQLiteProactiveRepository, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._owner = owner

    def record(self, key: str, now: datetime) -> None:
        self._owner._record_dedup(key, now)


class SQLiteProactiveRepository(WriteThrough, InMemoryProactiveRepository):
    def __init__(self, db: Database, limits: ProactiveLimits | None = None) -> None:
        super().__init__(limits)
        self.dedup = _DurableDedup(
            self,
            window=self.limits.dedup_window,
            max_keys=self.limits.max_dedup_keys,
        )
        mirror = DocumentMirror(db, "proactive")
        self._load(mirror.load())
        self._start_mirror(mirror)

    # ---------------------------------------------------------- mirror

    def _serialize(self) -> State:
        return {
            "task": {k: v.model_dump_json() for k, v in self._tasks.items()},
            "condition": {k: v.model_dump_json() for k, v in self._conditions.items()},
            "notification": {
                k: v.model_dump_json() for k, v in self._notifications.items()
            },
            "history": {r.record_id: r.model_dump_json() for r in self._history},
            "dedup": {k: at.isoformat() for k, at in self.dedup._seen.items()},
        }

    def _snapshot(self) -> Any:
        return (
            dict(self._tasks),
            dict(self._conditions),
            OrderedDict(self._notifications),
            deque(self._history, maxlen=self._history.maxlen),
            OrderedDict(self.dedup._seen),
        )

    def _restore(self, snapshot: Any) -> None:
        tasks, conditions, notifications, history, seen = snapshot
        self._tasks = dict(tasks)
        self._conditions = dict(conditions)
        self._notifications = OrderedDict(notifications)
        self._history = deque(history, maxlen=history.maxlen)
        self.dedup._seen = OrderedDict(seen)

    def _load(self, state: State) -> None:
        self._tasks = {
            k: ProactiveTask.model_validate_json(v)
            for k, v in state.get("task", {}).items()
        }
        self._conditions = {
            k: ConditionState.model_validate_json(v)
            for k, v in state.get("condition", {}).items()
        }
        notifications = sorted(
            (
                ProactiveNotification.model_validate_json(v)
                for v in state.get("notification", {}).values()
            ),
            key=lambda n: (n.created_at, n.notification_id),
        )
        self._notifications = OrderedDict(
            (n.notification_id, n)
            for n in notifications[-self.limits.max_notifications :]
        )
        history = sorted(
            (
                RunRecord.model_validate_json(v)
                for v in state.get("history", {}).values()
            ),
            key=lambda r: (r.started_at, r.record_id),
        )
        self._history = deque(history, maxlen=self.limits.max_history)
        seen = sorted(
            (datetime.fromisoformat(at), key)
            for key, at in state.get("dedup", {}).items()
        )
        self.dedup._seen = OrderedDict((key, at) for at, key in seen)

    # -------------------------------------------------------- mutators

    @durable
    def _record_dedup(self, key: str, now: datetime) -> None:
        DedupLedger.record(self.dedup, key, now)  # the base, unpersisted write

    add_task = durable(InMemoryProactiveRepository.add_task)
    replace_task = durable(InMemoryProactiveRepository.replace_task)
    delete_task = durable(InMemoryProactiveRepository.delete_task)
    set_condition = durable(InMemoryProactiveRepository.set_condition)
    add_notification = durable(InMemoryProactiveRepository.add_notification)
    replace_notification = durable(InMemoryProactiveRepository.replace_notification)
    remove_notification = durable(InMemoryProactiveRepository.remove_notification)
    add_history = durable(InMemoryProactiveRepository.add_history)


MUTATORS = (
    "add_task",
    "replace_task",
    "delete_task",
    "set_condition",
    "add_notification",
    "replace_notification",
    "remove_notification",
    "add_history",
)

__all__ = ["MUTATORS", "SQLiteProactiveRepository"]
