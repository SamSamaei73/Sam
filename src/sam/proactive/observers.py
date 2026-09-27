"""Built-in, trusted, deterministic observers.

Each one reads a trusted signal and returns a boolean plus a short value key.
None writes anything, calls a model, opens a file or reaches the network, and
each declares the permission it needs (checked by the runner on every run).
Cross-domain signals are injected as read-only callables by Sam's composition
root, so this package imports no other domain (Memory, Knowledge,
Professional) directly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from sam.models.models import Availability, PrivacyClass, ProviderId
from sam.permissions.models import (
    PermissionAction,
    PermissionResource,
    PermissionScope,
    Principal,
)
from sam.proactive.conditions import ConditionEntry, Observation, RequiredPermission
from sam.proactive.models import ProposedAction, RunFailure
from sam.proactive.schedule import ScheduleError, resolve_local, zone

MAX_LEAD_HOURS = 24 * 60


def _proactive_read(condition_id: str) -> RequiredPermission:
    return RequiredPermission(
        PermissionResource.PROACTIVE,
        PermissionAction.READ,
        PermissionScope.from_path(f"proactive/conditions/{condition_id}"),
    )


# ------------------------------------------------------------- deadline


def _deadline(params: Mapping[str, str]) -> datetime | None:
    """``date`` (YYYY-MM-DD), optional ``time`` (HH:MM, default 23:59) and
    ``timezone`` (IANA, default UTC)."""

    try:
        day = datetime.strptime(params["date"], "%Y-%m-%d").date()
        at = (
            datetime.strptime(params["time"], "%H:%M").time()
            if "time" in params
            else time(23, 59)
        )
        tz = zone(params.get("timezone", "UTC"))
    except (KeyError, ValueError, ScheduleError):
        return None
    return resolve_local(day, at, tz)


def _lead(params: Mapping[str, str]) -> int | None:
    raw = params.get("lead_hours", "24")
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if 1 <= value <= MAX_LEAD_HOURS else None


def _deadline_params_ok(params: Mapping[str, str]) -> bool:
    return _deadline(params) is not None and _lead(params) is not None


class DeadlineObserver:
    """Met while ``deadline - lead_hours <= now < deadline``. Pure clock
    arithmetic: no model, no source."""

    def observe(
        self, principal: Principal, params: Mapping[str, str], now: datetime
    ) -> Observation:
        deadline = _deadline(params)
        lead = _lead(params)
        if deadline is None or lead is None:
            return Observation(met=None, failure=RunFailure.INVALID_TASK)
        remaining = deadline - now.astimezone(UTC)
        met = timedelta(0) <= remaining <= timedelta(hours=lead)
        if met:
            hours = int(remaining.total_seconds() // 3600)
            summary = f"Deadline in about {hours} hour(s)."
        elif remaining < timedelta(0):
            summary = "The deadline has passed."
        else:
            summary = "The deadline is not close yet."
        return Observation(met=met, value_key=deadline.isoformat(), summary=summary)


def deadline_entry() -> ConditionEntry:
    return ConditionEntry(
        condition_id="deadline_approaching",
        label="deadline",
        permission=_proactive_read("deadline_approaching"),
        observer=DeadlineObserver(),
        required_params=frozenset({"date"}),
        optional_params=frozenset({"time", "timezone", "lead_hours"}),
        validate_params=_deadline_params_ok,
        proposed_action=ProposedAction.REVIEW_DEADLINE,
        max_seconds=1.0,  # pure clock arithmetic
        privacy_class=PrivacyClass.PERSONAL,  # the owner's own deadline
    )


# ------------------------------------------------------ provider status

_WATCHABLE_PROVIDERS = frozenset(
    {ProviderId.CLAUDE_SUBSCRIPTION.value, ProviderId.GEMINI_FREE.value}
)


@dataclass(frozen=True)
class ProviderStatusObserver:
    """Reads Sam's own view of a provider's availability (the same state the
    Models settings page shows). It never calls the provider's model."""

    state_of: Callable[[ProviderId], Availability]

    def observe(
        self, principal: Principal, params: Mapping[str, str], now: datetime
    ) -> Observation:
        provider = params.get("provider", "")
        if provider not in _WATCHABLE_PROVIDERS:
            return Observation(met=None, failure=RunFailure.INVALID_TASK)
        state = self.state_of(ProviderId(provider))
        available = state is Availability.AVAILABLE
        label = "available" if available else state.value.replace("_", " ")
        return Observation(
            met=available,
            value_key=state.value,
            summary=f"{provider.replace('_', ' ').title()} is {label}.",
        )


def provider_status_entry(
    state_of: Callable[[ProviderId], Availability],
) -> ConditionEntry:
    return ConditionEntry(
        condition_id="model_provider_status",
        label="model provider",
        permission=_proactive_read("model_provider_status"),
        observer=ProviderStatusObserver(state_of),
        required_params=frozenset({"provider"}),
        validate_params=lambda p: p.get("provider") in _WATCHABLE_PROVIDERS,
        proposed_action=ProposedAction.OPEN_MODEL_SETTINGS,
        # Reading availability may run the Claude adapter's readiness probes
        # (at most two, each with a 20 s timeout of their own).
        max_seconds=45.0,
        privacy_class=PrivacyClass.NORMAL,  # Sam's own provider status
    )


# ------------------------------------------- professional open conflicts


@dataclass(frozen=True)
class CountObserver:
    """Met while a read-only count is above zero. The count function is a
    permission-checked READ supplied by the composition root."""

    count_of: Callable[[Principal], int | None]
    noun: str

    def observe(
        self, principal: Principal, params: Mapping[str, str], now: datetime
    ) -> Observation:
        count = self.count_of(principal)
        if count is None:
            return Observation(met=None, failure=RunFailure.SOURCE_UNAVAILABLE)
        return Observation(
            met=count > 0,
            value_key=str(count),
            summary=f"{count} {self.noun} need your attention.",
        )


def professional_conflicts_entry(
    count_of: Callable[[Principal], int | None],
) -> ConditionEntry:
    return ConditionEntry(
        condition_id="professional_open_conflicts",
        label="professional profile",
        permission=RequiredPermission(
            PermissionResource.PROFESSIONAL,
            PermissionAction.READ,
            PermissionScope.identifier("profile:read"),
        ),
        observer=CountObserver(count_of, "professional conflict(s)"),
        proposed_action=ProposedAction.OPEN_PROFESSIONAL,
        max_seconds=5.0,  # an in-memory read
        # Professional data is private by provenance: only the template is shown.
        privacy_class=PrivacyClass.PRIVATE,
    )


__all__ = [
    "CountObserver",
    "DeadlineObserver",
    "ProviderStatusObserver",
    "deadline_entry",
    "professional_conflicts_entry",
    "provider_status_entry",
]
