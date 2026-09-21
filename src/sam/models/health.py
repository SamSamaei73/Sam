"""Provider runtime state with bounded cooldowns. Holds no credential, no
prompt and no provider error body."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import RLock

from sam.models.models import Availability, FailureCategory, ProviderId

COOLDOWNS: dict[FailureCategory, tuple[Availability, timedelta]] = {
    FailureCategory.RATE_LIMIT: (Availability.RATE_LIMITED, timedelta(seconds=60)),
    FailureCategory.AUTH_FAILURE: (Availability.UNAUTHORIZED, timedelta(minutes=5)),
    FailureCategory.UNAVAILABLE: (Availability.UNAVAILABLE, timedelta(seconds=30)),
    FailureCategory.TIMEOUT: (Availability.UNAVAILABLE, timedelta(seconds=30)),
}
USAGE_LIMIT_COOLDOWN = timedelta(minutes=10)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HealthTracker:
    """A transient failure never disables a provider permanently."""

    def __init__(self, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._until: dict[ProviderId, tuple[Availability, datetime]] = {}
        self._lock = RLock()

    def record_failure(
        self,
        provider_id: ProviderId,
        category: FailureCategory,
        *,
        usage_limit: bool = False,
    ) -> None:
        now = self._clock()
        with self._lock:
            if usage_limit and category is FailureCategory.RATE_LIMIT:
                self._until[provider_id] = (
                    Availability.USAGE_LIMIT,
                    now + USAGE_LIMIT_COOLDOWN,
                )
            elif category in COOLDOWNS:
                state, span = COOLDOWNS[category]
                self._until[provider_id] = (state, now + span)

    def record_success(self, provider_id: ProviderId) -> None:
        with self._lock:
            self._until.pop(provider_id, None)

    def state(self, provider_id: ProviderId) -> Availability:
        with self._lock:
            entry = self._until.get(provider_id)
            if entry is None:
                return Availability.AVAILABLE
            if self._clock() >= entry[1]:
                del self._until[provider_id]
                return Availability.AVAILABLE
            return entry[0]


__all__ = ["COOLDOWNS", "HealthTracker"]
