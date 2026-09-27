"""The proactive engine's only source of time.

A clock is any zero-argument callable returning a timezone-aware ``datetime``,
the same shape the PermissionEngine already uses. Production uses
``SystemClock``; tests use ``ManualClock`` and never sleep. A model is never a
clock: no scheduling decision reads time from provider output.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import RLock

Clock = Callable[[], datetime]


class SystemClock:
    """Wall-clock UTC time."""

    def __call__(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """A clock that only moves when told to. Thread-safe, never sleeps."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("clock time must be timezone-aware")
        self._now = start.astimezone(UTC)
        self._lock = RLock()

    def __call__(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, delta: timedelta | None = None, **kwargs: float) -> datetime:
        step = delta if delta is not None else timedelta(**kwargs)
        if step < timedelta(0):
            raise ValueError("a clock never moves backwards")
        with self._lock:
            self._now += step
            return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("clock time must be timezone-aware")
        with self._lock:
            if value < self._now:
                raise ValueError("a clock never moves backwards")
            self._now = value.astimezone(UTC)


def aware(value: datetime) -> datetime:
    """Refuse naive datetimes: every stored instant is timezone-aware UTC."""

    if value.tzinfo is None:
        raise ValueError("naive datetimes are never accepted")
    return value.astimezone(UTC)


__all__ = ["Clock", "ManualClock", "SystemClock", "aware"]
