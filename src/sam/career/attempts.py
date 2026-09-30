"""External action attempts: at-most-once SUBMIT / SEND and honest outcomes.

Every consequential external action (an application submission, an outreach
e-mail) is one ``ExternalActionAttempt`` with a Sam-generated ``attempt_id``,
created only AFTER every check has passed and the one-time confirmation has
been consumed, and BEFORE the adapter is called.

At most once: the ledger keeps at most one blocking attempt per (action, item).
While an attempt is IN_FLIGHT, OUTCOME_UNKNOWN or VERIFIED_SUCCESS, a second
request for the same item never reaches the adapter; it is answered from the
ledger. Only VERIFIED_FAILURE (the adapter proved nothing happened) or an
explicit, confirmed owner release lets a new, freshly reviewed attempt start.

Honest outcomes: an adapter returns an ``ExternalActionResult`` with one of

    VERIFIED_SUCCESS  a trusted receipt proves the action happened
    VERIFIED_FAILURE  a trusted result proves it did NOT happen
    OUTCOME_UNKNOWN   nobody can prove either (timeout, reset, lost response)

``interpret`` believes a result only if it is well formed and names exactly
this attempt, item, opportunity and manifest (and, for a success, a trusted
receipt id and the trusted destination). Anything else, including any
exception, is OUTCOME_UNKNOWN: never retried automatically, never SUBMITTED,
never "safely failed".
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from threading import RLock
from typing import Protocol

MAX_ATTEMPTS = 2_000
_RECEIPT = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_REASON = re.compile(r"^[a-z0-9_]{1,64}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class ExternalAction(StrEnum):
    SUBMIT = "submit"
    SEND = "send"


class ExternalActionStatus(StrEnum):
    VERIFIED_SUCCESS = "verified_success"
    VERIFIED_FAILURE = "verified_failure"
    OUTCOME_UNKNOWN = "outcome_unknown"


class AttemptState(StrEnum):
    IN_FLIGHT = "in_flight"
    VERIFIED_SUCCESS = "verified_success"
    VERIFIED_FAILURE = "verified_failure"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RELEASED_BY_OWNER = "released_by_owner"


class ReconciliationState(StrEnum):
    NONE = "none"
    REQUIRED = "required"  # the outcome is unknown: only reconciliation or release
    RECONCILED = "reconciled"  # a trusted lookup settled an unknown outcome
    RELEASED = "released"  # the owner accepted the risk and went back to review


# The ONLY state changes an attempt may make. The durable store enforces the
# same table with a database trigger.
ALLOWED_TRANSITIONS: dict[AttemptState, frozenset[AttemptState]] = {
    AttemptState.IN_FLIGHT: frozenset(
        {
            AttemptState.VERIFIED_SUCCESS,
            AttemptState.VERIFIED_FAILURE,
            AttemptState.OUTCOME_UNKNOWN,
        }
    ),
    AttemptState.OUTCOME_UNKNOWN: frozenset(
        {
            AttemptState.VERIFIED_SUCCESS,
            AttemptState.VERIFIED_FAILURE,
            AttemptState.RELEASED_BY_OWNER,
        }
    ),
    AttemptState.VERIFIED_SUCCESS: frozenset(),
    AttemptState.VERIFIED_FAILURE: frozenset(),
    AttemptState.RELEASED_BY_OWNER: frozenset(),
}

# An unresolved attempt stops any new attempt for the same item; a blocking
# one (unresolved or verified success) stops the same manifest forever.
UNRESOLVED = frozenset({AttemptState.IN_FLIGHT, AttemptState.OUTCOME_UNKNOWN})
# States that stop any new attempt for the same item (service-level answer).
BLOCKING = frozenset(
    {
        AttemptState.IN_FLIGHT,
        AttemptState.OUTCOME_UNKNOWN,
        AttemptState.VERIFIED_SUCCESS,
    }
)


@dataclass(frozen=True)
class AttemptReference:
    """What an adapter is told about an attempt (for dispatch or
    reconciliation): identities and the manifest hash, never content."""

    attempt_id: str
    action: ExternalAction
    opportunity_id: str | None
    item_id: str
    manifest_hash: str


@dataclass(frozen=True)
class ExternalActionResult:
    """The ONLY thing Sam accepts from a submission adapter or e-mail tool.
    Built by trusted integration code, never parsed from page or model text."""

    attempt_id: str
    status: ExternalActionStatus
    manifest_hash: str
    item_id: str
    opportunity_id: str | None = None
    receipt_id: str | None = None
    destination: str | None = None  # final URL / recipient actually reached
    reason_code: str | None = None


@dataclass(frozen=True)
class ExternalActionAttempt:
    attempt_id: str
    action: ExternalAction
    owner_id: str
    opportunity_id: str | None
    item_id: str
    item_version: int
    manifest_hash: str
    created_at: datetime
    state: AttemptState = AttemptState.IN_FLIGHT
    receipt_id: str | None = None
    reason_code: str | None = None
    finished_at: datetime | None = None
    # canonical host (submit) or ``sha256:<hash>`` of the recipient (send)
    destination: str | None = None
    updated_at: datetime | None = None
    reconciliation: ReconciliationState = ReconciliationState.NONE

    @property
    def reference(self) -> AttemptReference:
        return AttemptReference(
            self.attempt_id,
            self.action,
            self.opportunity_id,
            self.item_id,
            self.manifest_hash,
        )


@dataclass(frozen=True)
class Interpretation:
    status: ExternalActionStatus
    receipt_id: str | None
    reason_code: str


def interpret(
    result: object,
    attempt: ExternalActionAttempt,
    destination_ok: Callable[[str | None], bool],
) -> Interpretation:
    """Fail closed: only a well-formed result for exactly this attempt counts.
    A success also needs a trusted receipt id and the trusted destination."""

    unknown = ExternalActionStatus.OUTCOME_UNKNOWN
    if not isinstance(result, ExternalActionResult):
        return Interpretation(unknown, None, "malformed_result")
    if not isinstance(result.status, ExternalActionStatus):
        return Interpretation(unknown, None, "malformed_result")
    if (
        result.attempt_id != attempt.attempt_id
        or result.item_id != attempt.item_id
        or result.opportunity_id != attempt.opportunity_id
    ):
        return Interpretation(unknown, None, "result_for_another_attempt")
    if not isinstance(result.manifest_hash, str) or not _HASH.fullmatch(
        result.manifest_hash
    ):
        return Interpretation(unknown, None, "malformed_result")
    if result.manifest_hash != attempt.manifest_hash:
        return Interpretation(unknown, None, "result_for_another_manifest")
    reason = (
        result.reason_code
        if isinstance(result.reason_code, str) and _REASON.fullmatch(result.reason_code)
        else None
    )
    if result.status is ExternalActionStatus.VERIFIED_SUCCESS:
        receipt = result.receipt_id
        if not isinstance(receipt, str) or not _RECEIPT.fullmatch(receipt):
            return Interpretation(unknown, None, "malformed_receipt")
        if not destination_ok(result.destination):
            return Interpretation(unknown, None, "untrusted_final_destination")
        return Interpretation(result.status, receipt, "verified")
    if result.status is ExternalActionStatus.VERIFIED_FAILURE:
        return Interpretation(result.status, None, reason or "verified_failure")
    return Interpretation(unknown, None, reason or "outcome_unknown")


class LedgerFull(RuntimeError):
    pass


class AttemptConflict(RuntimeError):
    """A blocking attempt already exists for this item (raised by the store:
    in memory by Python, durably by the database's UNIQUE index)."""

    def __init__(self) -> None:
        super().__init__("attempt_already_blocking")
        self.code = "attempt_already_blocking"


class IllegalTransition(RuntimeError):
    def __init__(self) -> None:
        super().__init__("illegal_attempt_transition")
        self.code = "illegal_attempt_transition"


class AttemptStore(Protocol):
    """Where attempts live. ``durable`` is True only for a store whose writes
    are committed to disk before ``insert`` / ``update`` return."""

    durable: bool

    def get(self, attempt_id: str) -> ExternalActionAttempt | None: ...
    def all(self) -> tuple[ExternalActionAttempt, ...]: ...
    def blocking(
        self, action: ExternalAction, item_id: str
    ) -> ExternalActionAttempt | None: ...
    def count(self) -> int: ...
    def insert(self, attempt: ExternalActionAttempt) -> None: ...
    def update(
        self, attempt: ExternalActionAttempt, *, expected: AttemptState
    ) -> None: ...


class InMemoryAttemptStore:
    durable = False

    def __init__(self) -> None:
        self._attempts: dict[str, ExternalActionAttempt] = {}
        self._lock = RLock()

    def get(self, attempt_id: str) -> ExternalActionAttempt | None:
        with self._lock:
            return self._attempts.get(attempt_id)

    def all(self) -> tuple[ExternalActionAttempt, ...]:
        with self._lock:
            return tuple(self._attempts.values())

    def blocking(
        self, action: ExternalAction, item_id: str
    ) -> ExternalActionAttempt | None:
        with self._lock:
            return next(
                (
                    a
                    for a in self._attempts.values()
                    if a.action is action
                    and a.item_id == item_id
                    and a.state in BLOCKING
                ),
                None,
            )

    def count(self) -> int:
        with self._lock:
            return len(self._attempts)

    def insert(self, attempt: ExternalActionAttempt) -> None:
        """The same rules the durable store's UNIQUE indexes enforce."""

        with self._lock:
            for other in self._attempts.values():
                if (
                    other.action is not attempt.action
                    or other.item_id != attempt.item_id
                ):
                    continue
                if other.state in UNRESOLVED:
                    raise AttemptConflict()  # one unresolved attempt per item
                if (
                    other.state in BLOCKING
                    and other.manifest_hash == attempt.manifest_hash
                ):
                    raise AttemptConflict()  # a manifest is never attempted twice
            self._attempts[attempt.attempt_id] = attempt

    def update(self, attempt: ExternalActionAttempt, *, expected: AttemptState) -> None:
        with self._lock:
            current = self._attempts.get(attempt.attempt_id)
            if current is None or current.state is not expected:
                raise IllegalTransition()
            if attempt.state not in ALLOWED_TRANSITIONS[expected]:
                raise IllegalTransition()
            self._attempts[attempt.attempt_id] = attempt


class AttemptLedger:
    """The at-most-once ledger over an ``AttemptStore``. ``lock`` is also the
    service's single-flight lock: validation, confirmation consumption and
    attempt creation happen under it; the adapter call happens outside it.
    With a durable store, an attempt is on disk (IN_FLIGHT) before any
    adapter is called, and the database itself refuses a second blocking
    attempt for the same item, even from another process."""

    def __init__(
        self, max_attempts: int = MAX_ATTEMPTS, store: AttemptStore | None = None
    ) -> None:
        self.lock = RLock()
        self._store: AttemptStore = store or InMemoryAttemptStore()
        self._max = max_attempts

    @property
    def durable(self) -> bool:
        return bool(self._store.durable)

    def get(self, attempt_id: str) -> ExternalActionAttempt | None:
        return self._store.get(attempt_id)

    def all(self) -> tuple[ExternalActionAttempt, ...]:
        return self._store.all()

    def blocking(
        self, action: ExternalAction, item_id: str
    ) -> ExternalActionAttempt | None:
        return self._store.blocking(action, item_id)

    def has_capacity(self) -> bool:
        with self.lock:
            return self._store.count() < self._max

    def start(
        self,
        *,
        action: ExternalAction,
        owner_id: str,
        opportunity_id: str | None,
        item_id: str,
        item_version: int,
        manifest_hash: str,
        now: datetime,
        destination: str | None = None,
    ) -> ExternalActionAttempt:
        with self.lock:
            if self._store.count() >= self._max:
                raise LedgerFull("attempt_ledger_full")
            attempt = ExternalActionAttempt(
                attempt_id="at_" + uuid.uuid4().hex[:24],
                action=action,
                owner_id=owner_id,
                opportunity_id=opportunity_id,
                item_id=item_id,
                item_version=item_version,
                manifest_hash=manifest_hash,
                created_at=now,
                destination=destination,
                updated_at=now,
            )
            self._store.insert(attempt)  # AttemptConflict if one already blocks
            return attempt

    def finish(
        self, attempt_id: str, outcome: Interpretation, now: datetime
    ) -> ExternalActionAttempt:
        state = AttemptState(outcome.status.value)
        with self.lock:
            current = self._store.get(attempt_id)
            if current is None:
                raise IllegalTransition()
            if current.state is AttemptState.OUTCOME_UNKNOWN:
                reconciliation = (
                    ReconciliationState.REQUIRED
                    if state is AttemptState.OUTCOME_UNKNOWN
                    else ReconciliationState.RECONCILED
                )
            else:
                reconciliation = (
                    ReconciliationState.REQUIRED
                    if state is AttemptState.OUTCOME_UNKNOWN
                    else ReconciliationState.NONE
                )
            done = replace(
                current,
                state=state,
                receipt_id=outcome.receipt_id,
                reason_code=outcome.reason_code,
                finished_at=now,
                updated_at=now,
                reconciliation=reconciliation,
            )
            if state is not current.state:
                self._store.update(done, expected=current.state)
            return done

    def release(self, attempt_id: str, now: datetime) -> ExternalActionAttempt:
        """The owner explicitly accepts the risk of an unknown outcome."""

        with self.lock:
            current = self._store.get(attempt_id)
            if current is None or current.state is not AttemptState.OUTCOME_UNKNOWN:
                raise RuntimeError("attempt_not_unknown")
            done = replace(
                current,
                state=AttemptState.RELEASED_BY_OWNER,
                reason_code="released_by_owner",
                finished_at=now,
                updated_at=now,
                reconciliation=ReconciliationState.RELEASED,
            )
            self._store.update(done, expected=AttemptState.OUTCOME_UNKNOWN)
            return done

    def recover_after_restart(self, now: datetime) -> tuple[ExternalActionAttempt, ...]:
        """Crash recovery: an attempt that was IN_FLIGHT when the previous
        process died may already have happened. It becomes OUTCOME_UNKNOWN
        (reconciliation required), never failed and never retryable."""

        recovered: list[ExternalActionAttempt] = []
        with self.lock:
            for attempt in self._store.all():
                if attempt.state is AttemptState.IN_FLIGHT:
                    unknown = replace(
                        attempt,
                        state=AttemptState.OUTCOME_UNKNOWN,
                        reason_code="interrupted_by_restart",
                        updated_at=now,
                        reconciliation=ReconciliationState.REQUIRED,
                    )
                    self._store.update(unknown, expected=AttemptState.IN_FLIGHT)
                    recovered.append(unknown)
        return tuple(recovered)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "BLOCKING",
    "UNRESOLVED",
    "MAX_ATTEMPTS",
    "AttemptConflict",
    "AttemptLedger",
    "AttemptStore",
    "AttemptReference",
    "AttemptState",
    "ExternalAction",
    "ExternalActionAttempt",
    "ExternalActionResult",
    "ExternalActionStatus",
    "IllegalTransition",
    "InMemoryAttemptStore",
    "Interpretation",
    "LedgerFull",
    "ReconciliationState",
    "interpret",
]
