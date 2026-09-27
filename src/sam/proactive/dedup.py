"""Deterministic notification deduplication, owned by Sam (never a model).

``dedup_key = sha256(task_id | event identity | normalized state/version)``.
The ledger remembers keys for a bounded window and a bounded count; a key seen
inside the window is a duplicate and creates no notification.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from datetime import datetime, timedelta
from threading import RLock


def dedup_key(task_id: str, event: str, state: str) -> str:
    raw = "\x1f".join((task_id, event, state)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class DedupLedger:
    def __init__(self, *, window: timedelta, max_keys: int) -> None:
        if max_keys < 1:
            raise ValueError("max_keys must be positive")
        self._window = window
        self._max = max_keys
        self._seen: OrderedDict[str, datetime] = OrderedDict()
        self._lock = RLock()

    def _prune(self, now: datetime) -> None:
        while self._seen:
            key, at = next(iter(self._seen.items()))
            if now - at < self._window and len(self._seen) <= self._max:
                break
            del self._seen[key]

    def is_duplicate(self, key: str, now: datetime) -> bool:
        with self._lock:
            self._prune(now)
            return key in self._seen

    def record(self, key: str, now: datetime) -> None:
        with self._lock:
            self._seen.pop(key, None)
            self._seen[key] = now
            self._prune(now)

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)


__all__ = ["DedupLedger", "dedup_key"]
