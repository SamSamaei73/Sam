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


# States that stop any new attempt for the same item.
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


class AttemptLedger:
    """In-memory, lock-protected attempt store. ``lock`` is also the service's
    single-flight lock: validation, confirmation consumption and attempt
    creation happen under it; the adapter call happens outside it."""

    def __init__(self, max_attempts: int = MAX_ATTEMPTS) -> None:
        self.lock = RLock()
        self._attempts: dict[str, ExternalActionAttempt] = {}
        self._max = max_attempts

    def get(self, attempt_id: str) -> ExternalActionAttempt | None:
        with self.lock:
            return self._attempts.get(attempt_id)

    def all(self) -> tuple[ExternalActionAttempt, ...]:
        with self.lock:
            return tuple(self._attempts.values())

    def blocking(
        self, action: ExternalAction, item_id: str
    ) -> ExternalActionAttempt | None:
        with self.lock:
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

    def has_capacity(self) -> bool:
        with self.lock:
            return len(self._attempts) < self._max

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
    ) -> ExternalActionAttempt:
        with self.lock:
            if len(self._attempts) >= self._max:
                raise LedgerFull("attempt_ledger_full")
            if self.blocking(action, item_id) is not None:
                raise RuntimeError("attempt_already_blocking")
            attempt = ExternalActionAttempt(
                attempt_id="at_" + uuid.uuid4().hex[:24],
                action=action,
                owner_id=owner_id,
                opportunity_id=opportunity_id,
                item_id=item_id,
                item_version=item_version,
                manifest_hash=manifest_hash,
                created_at=now,
            )
            self._attempts[attempt.attempt_id] = attempt
            return attempt

    def finish(
        self, attempt_id: str, outcome: Interpretation, now: datetime
    ) -> ExternalActionAttempt:
        state = AttemptState(outcome.status.value)
        with self.lock:
            done = replace(
                self._attempts[attempt_id],
                state=state,
                receipt_id=outcome.receipt_id,
                reason_code=outcome.reason_code,
                finished_at=now,
            )
            self._attempts[attempt_id] = done
            return done

    def release(self, attempt_id: str, now: datetime) -> ExternalActionAttempt:
        """The owner explicitly accepts the risk of an unknown outcome."""

        with self.lock:
            current = self._attempts[attempt_id]
            if current.state is not AttemptState.OUTCOME_UNKNOWN:
                raise RuntimeError("attempt_not_unknown")
            done = replace(
                current,
                state=AttemptState.RELEASED_BY_OWNER,
                reason_code="released_by_owner",
                finished_at=now,
            )
            self._attempts[attempt_id] = done
            return done


__all__ = [
    "BLOCKING",
    "MAX_ATTEMPTS",
    "AttemptLedger",
    "AttemptReference",
    "AttemptState",
    "ExternalAction",
    "ExternalActionAttempt",
    "ExternalActionResult",
    "ExternalActionStatus",
    "Interpretation",
    "LedgerFull",
    "interpret",
]
