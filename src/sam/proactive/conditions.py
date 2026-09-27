"""Condition watches: a trusted registry of observers and the deterministic
state machine that turns observations into change events.

A task never contains an observer. It names one by ``condition_id``, which is
looked up by exact key in a registry that Sam's own composition code builds
once. There is no dynamic import, no module path, no callable in a task and no
fallback for an unknown id: unknown means invalid. Each observer declares the
permission it needs; the runner asks the PermissionEngine for it on EVERY run.

Transitions (``BECOMES_TRUE``; the initial state is "not met"):

    false -> false   NO_CHANGE          no notification
    false -> true    CONDITION_MET      candidate notification
    true  -> true    NO_CHANGE/CHANGED  no notification (unless REPEAT_WHILE_TRUE)
    true  -> false   CONDITION_CLEARED  state updated, no notification
    error            ERROR              state unchanged
    unknown          UNKNOWN            state unchanged

``ON_CHANGE`` records its first observation as a baseline and then reports
CHANGED whenever the observed value changes. Deduplication and cooldown are
applied afterwards, by Sam, never by a model.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sam.models.models import PrivacyClass
from sam.permissions.models import (
    PermissionAction,
    PermissionResource,
    PermissionScope,
    Principal,
)
from sam.proactive.models import (
    ChangeKind,
    ConditionState,
    NotificationReason,
    ProposedAction,
    RunFailure,
    TriggerSemantics,
    clean_text,
)

MAX_OBSERVATION_SUMMARY = 200
MAX_VALUE_KEY = 128


@dataclass(frozen=True)
class Observation:
    """What an observer saw. ``met`` None means it could not tell (UNKNOWN);
    ``failure`` set means the observation failed (ERROR)."""

    met: bool | None
    value_key: str | None = None
    summary: str = ""
    failure: RunFailure | None = None
    # Set by the (trusted) observer from where the data came from. It is joined
    # with the task's and the registration's class; it can never lower them.
    privacy: PrivacyClass = PrivacyClass.NORMAL


class ConditionObserver(Protocol):
    def observe(
        self, principal: Principal, params: Mapping[str, str], now: datetime
    ) -> Observation: ...


@dataclass(frozen=True)
class RequiredPermission:
    resource: PermissionResource
    action: PermissionAction
    scope: PermissionScope


@dataclass(frozen=True)
class ConditionEntry:
    """One trusted condition. Built by Sam's code only."""

    condition_id: str
    label: str
    permission: RequiredPermission
    observer: ConditionObserver
    required_params: frozenset[str] = frozenset()
    optional_params: frozenset[str] = frozenset()
    validate_params: Callable[[Mapping[str, str]], bool] | None = None
    proposed_action: ProposedAction = ProposedAction.REVIEW_IN_SAM
    # The longest one observation may take. Every observer's own I/O must be
    # bounded by this; the service refuses an entry that exceeds the run budget.
    max_seconds: float = 5.0
    # The trusted privacy class of what this source can reveal (provenance).
    privacy_class: PrivacyClass = PrivacyClass.PERSONAL

    def accepts(self, params: Mapping[str, str]) -> bool:
        names = set(params)
        if not self.required_params <= names:
            return False
        if not names <= self.required_params | self.optional_params:
            return False
        return self.validate_params(params) if self.validate_params else True


@dataclass(frozen=True)
class ConditionRegistry:
    """An immutable, exact-key registry. ``get`` never resolves anything that
    was not registered by Sam's composition code."""

    _entries: Mapping[str, ConditionEntry] = field(default_factory=dict)

    @classmethod
    def of(cls, entries: Iterable[ConditionEntry]) -> ConditionRegistry:
        table: dict[str, ConditionEntry] = {}
        for entry in entries:
            if entry.condition_id in table:
                raise ValueError("duplicate condition id")
            # An observer OBSERVES: it may only ever need a READ permission.
            if entry.permission.action is not PermissionAction.READ:
                raise ValueError("an observer may only require a READ permission")
            if not 0 < entry.max_seconds <= 600:
                raise ValueError("an observer must declare a bounded duration")
            table[entry.condition_id] = entry
        return cls(dict(table))

    def get(self, condition_id: str) -> ConditionEntry | None:
        return self._entries.get(condition_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def entries(self) -> tuple[ConditionEntry, ...]:
        return tuple(self._entries[k] for k in sorted(self._entries))


@dataclass(frozen=True)
class Transition:
    change: ChangeKind
    candidate: bool
    reason: NotificationReason | None
    state: ConditionState


def bounded(observation: Observation) -> Observation:
    """Refuse an oversized observation rather than store it."""

    if observation.value_key is not None and len(observation.value_key) > MAX_VALUE_KEY:
        return Observation(met=None, failure=RunFailure.SOURCE_UNAVAILABLE)
    return Observation(
        met=observation.met,
        value_key=observation.value_key,
        summary=clean_text(observation.summary, MAX_OBSERVATION_SUMMARY),
        failure=observation.failure,
        privacy=observation.privacy,
    )


def transition(
    previous: ConditionState,
    observation: Observation,
    semantics: TriggerSemantics,
    now: datetime,
) -> Transition:
    if observation.failure is not None:
        return Transition(ChangeKind.ERROR, False, None, previous)
    if observation.met is None:
        return Transition(ChangeKind.UNKNOWN, False, None, previous)
    met = observation.met
    value = observation.value_key
    observed = previous.model_copy(update={"observed_at": now})

    if semantics is TriggerSemantics.ON_CHANGE:
        state = observed.model_copy(update={"met": met, "value_key": value})
        if previous.observed_at is None:
            return Transition(ChangeKind.NO_CHANGE, False, None, state)
        if value != previous.value_key:
            state = state.model_copy(update={"episode": previous.episode + 1})
            return Transition(
                ChangeKind.CHANGED, True, NotificationReason.CONDITION_CHANGED, state
            )
        return Transition(ChangeKind.NO_CHANGE, False, None, state)

    if met and not previous.met:
        state = observed.model_copy(
            update={"met": True, "value_key": value, "episode": previous.episode + 1}
        )
        return Transition(
            ChangeKind.CONDITION_MET, True, NotificationReason.CONDITION_MET, state
        )
    if met and previous.met:
        state = observed.model_copy(update={"value_key": value})
        change = (
            ChangeKind.CHANGED if value != previous.value_key else ChangeKind.NO_CHANGE
        )
        repeat = semantics is TriggerSemantics.REPEAT_WHILE_TRUE
        return Transition(
            change,
            repeat,
            NotificationReason.CONDITION_STILL_TRUE if repeat else None,
            state,
        )
    if previous.met and not met:
        state = observed.model_copy(update={"met": False, "value_key": value})
        return Transition(ChangeKind.CONDITION_CLEARED, False, None, state)
    state = observed.model_copy(update={"value_key": value})
    return Transition(ChangeKind.NO_CHANGE, False, None, state)


__all__ = [
    "ConditionEntry",
    "ConditionObserver",
    "ConditionRegistry",
    "Observation",
    "RequiredPermission",
    "Transition",
    "bounded",
    "transition",
]
