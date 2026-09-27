"""Cooldown: the minimum time between two notifications from one task.

The cooldown is part of the owner's task (``cooldown_hours``), bounded by
``ProactiveLimits`` and changed only through trusted task management. Nothing a
model returns is consulted here.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def cooldown_allows(
    last_notified_at: datetime | None, now: datetime, cooldown_hours: int
) -> bool:
    if last_notified_at is None:
        return True
    return now - last_notified_at >= timedelta(hours=cooldown_hours)


__all__ = ["cooldown_allows"]
