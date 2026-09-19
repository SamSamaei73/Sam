"""The deterministic Permission Engine.

Answers exactly one question: "is this principal authorized to perform
this action, on this resource, in this scope, right now?" The LLM is
treated as an untrusted requester throughout this module — it can request
an action, but nothing in this file lets a request author its own
authorization. Risk comes only from ``sam.permissions.policy`` (static,
code-reviewed); grants come only from the injected ``PermissionStore``;
confirmations come only from the injected ``ConfirmationProvider`` and must
be freshly, exactly consumed.

Evaluation precedence (deterministic, documented — never dependent on
dict/set iteration order):

1. Unknown (resource, action) combination → DENY (``UNCLASSIFIED_ACTION``).
2. No grant exists whose scope contains the request, is ACTIVE, and is not
   expired → DENY. If a scope-matching grant exists but is revoked or
   expired, the more specific reason (``GRANT_REVOKED`` /
   ``GRANT_EXPIRED``) is reported instead of the generic
   ``NO_MATCHING_GRANT``.
3. Among every fully-matching grant (ACTIVE, unexpired, scope contains the
   request), the *most specific* one (longest scope, then earliest
   ``created_at``, then lexicographically smallest ``grant_id``) is cited
   as ``grant_id`` on the decision — purely for audit reference. This
   never changes the outcome: outcome and confirmation requirement are
   computed from the union of all fully-matching grants (see next point),
   so a broad, permissive grant can never silently relax a stricter
   narrower one that also matches.
4. If policy or *any* fully-matching grant requires confirmation, or the
   policy risk is CRITICAL (always, regardless of policy/grant data — see
   the assertion in ``sam.permissions.policy``), the request needs a
   confirmation:
   - no ``confirmation_id`` supplied → ``CONFIRM_REQUIRED``, and a new
     PENDING confirmation is created and returned for a future caller to
     approve/deny out of band.
   - a ``confirmation_id`` is supplied → it must ``consume`` successfully
     (APPROVED, unexpired, and matching this exact principal / action /
     resource / scope / target) or the request is DENIED
     (``CONFIRMATION_INVALID``); a confirmation is never implicitly
     trusted, and a used confirmation can never be consumed again.
5. Otherwise → ALLOW.
6. Any unexpected internal failure (a raising store, an unforeseen bug) is
   caught and converted to DENY (``SYSTEM_ERROR``) — the engine never
   fails open. See ``evaluate``.

Every evaluation is recorded to the injected ``AuditSink`` exactly once,
including denials and system errors. A failing audit sink never changes
the returned decision (see ``evaluate``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sam.permissions.audit import AuditSink
from sam.permissions.confirmation import ConfirmationProvider
from sam.permissions.errors import ConfirmationError
from sam.permissions.models import (
    AuditEvent,
    ConfirmationRecord,
    DecisionOutcome,
    DenialReason,
    GrantStatus,
    PermissionDecision,
    PermissionGrant,
    PermissionRequest,
    RiskLevel,
    utc_now,
)
from sam.permissions.policy import classify
from sam.permissions.store import PermissionStore


@dataclass(frozen=True)
class _MatchResult:
    """Internal-only aggregation over every grant matching the request."""

    matching_grants: tuple[PermissionGrant, ...]
    best_denial_reason: DenialReason


class PermissionEngine:
    """Ties policy, grant storage, confirmation, and audit together.

    No global state: construct one per application (or per test) with
    explicit collaborators. Nothing here imports FastAPI, the Anthropic
    SDK, a database driver, or any concrete tool/integration.
    """

    def __init__(
        self,
        *,
        store: PermissionStore,
        confirmation_provider: ConfirmationProvider,
        audit_sink: AuditSink,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._confirmations = confirmation_provider
        self._audit = audit_sink
        self._clock = clock

    def evaluate(
        self, request: PermissionRequest, *, confirmation_id: str | None = None
    ) -> PermissionDecision:
        """Evaluate one request. Never raises; always fails closed.

        ``request`` must already be a validly constructed
        ``PermissionRequest`` — a caller that cannot construct one (an
        unknown action/resource string, a malformed scope) must treat that
        construction failure itself as a denial; it never reaches here.
        """

        now = self._clock()
        try:
            decision = self._evaluate_unsafe(
                request, confirmation_id=confirmation_id, now=now
            )
        except Exception:
            # Fail closed: an unexpected failure anywhere in evaluation
            # (a raising store, a raising confirmation provider outside
            # the documented ConfirmationError contract, a programming
            # error) must never be interpreted as ALLOW. The exception's
            # own text is deliberately not logged or included anywhere —
            # it may carry provider- or store-specific internals.
            decision = PermissionDecision(
                outcome=DecisionOutcome.DENY,
                request=request,
                risk=RiskLevel.CRITICAL,
                decided_at=now,
                reason=DenialReason.SYSTEM_ERROR,
            )
        self._record_audit(decision, now=now)
        return decision

    def _evaluate_unsafe(
        self,
        request: PermissionRequest,
        *,
        confirmation_id: str | None,
        now: datetime,
    ) -> PermissionDecision:
        policy_entry = classify(request.resource, request.action)
        if policy_entry is None:
            return PermissionDecision(
                outcome=DecisionOutcome.DENY,
                request=request,
                risk=RiskLevel.CRITICAL,
                decided_at=now,
                reason=DenialReason.UNCLASSIFIED_ACTION,
            )

        candidates = self._store.list_grants(
            request.principal, resource=request.resource, action=request.action
        )
        match = self._match_grants(candidates, request=request, now=now)

        if not match.matching_grants:
            return PermissionDecision(
                outcome=DecisionOutcome.DENY,
                request=request,
                risk=policy_entry.risk,
                decided_at=now,
                reason=match.best_denial_reason,
            )

        primary_grant = self._most_specific(match.matching_grants)
        # CRITICAL always requires confirmation — never waivable, defense in
        # depth alongside the assertion in sam.permissions.policy.
        requires_confirmation = (
            policy_entry.requires_confirmation
            or policy_entry.risk is RiskLevel.CRITICAL
            or any(
                grant.always_require_confirmation for grant in match.matching_grants
            )
        )

        if not requires_confirmation:
            return PermissionDecision(
                outcome=DecisionOutcome.ALLOW,
                request=request,
                risk=policy_entry.risk,
                decided_at=now,
                grant_id=primary_grant.grant_id,
            )

        if confirmation_id is None:
            confirmation = self._confirmations.request(
                request, risk=policy_entry.risk, now=now
            )
            return PermissionDecision(
                outcome=DecisionOutcome.CONFIRM_REQUIRED,
                request=request,
                risk=policy_entry.risk,
                decided_at=now,
                grant_id=primary_grant.grant_id,
                confirmation=confirmation,
            )

        try:
            consumed = self._confirmations.consume(
                confirmation_id, request=request, now=now
            )
        except ConfirmationError:
            return PermissionDecision(
                outcome=DecisionOutcome.DENY,
                request=request,
                risk=policy_entry.risk,
                decided_at=now,
                reason=DenialReason.CONFIRMATION_INVALID,
            )

        return PermissionDecision(
            outcome=DecisionOutcome.ALLOW,
            request=request,
            risk=policy_entry.risk,
            decided_at=now,
            grant_id=primary_grant.grant_id,
            confirmation=consumed,
        )

    @staticmethod
    def _match_grants(
        candidates: Sequence[PermissionGrant],
        *,
        request: PermissionRequest,
        now: datetime,
    ) -> _MatchResult:
        fully_matching: list[PermissionGrant] = []
        best_reason = DenialReason.NO_MATCHING_GRANT
        for grant in candidates:
            if not grant.scope.contains(request.scope):
                continue
            if grant.status is not GrantStatus.ACTIVE:
                best_reason = DenialReason.GRANT_REVOKED
                continue
            if grant.expires_at is not None and grant.expires_at <= now:
                best_reason = DenialReason.GRANT_EXPIRED
                continue
            fully_matching.append(grant)
        return _MatchResult(
            matching_grants=tuple(fully_matching), best_denial_reason=best_reason
        )

    @staticmethod
    def _most_specific(grants: Sequence[PermissionGrant]) -> PermissionGrant:
        """Deterministic tie-break: longest scope, then oldest, then id."""

        return min(
            grants,
            key=lambda grant: (
                -len(grant.scope.segments),
                grant.created_at,
                grant.grant_id,
            ),
        )

    def _record_audit(self, decision: PermissionDecision, *, now: datetime) -> None:
        event = AuditEvent(
            event_id=uuid4().hex,
            occurred_at=now,
            principal=decision.request.principal,
            action=decision.request.action,
            resource=decision.request.resource,
            scope_summary=decision.request.scope.as_text(),
            risk=decision.risk,
            outcome=decision.outcome,
            reason=decision.reason,
            grant_id=decision.grant_id,
            confirmation_id=(
                decision.confirmation.confirmation_id if decision.confirmation else None
            ),
            confirmation_status=(
                decision.confirmation.status if decision.confirmation else None
            ),
            correlation_id=decision.request.correlation_id,
        )
        try:
            self._audit.record(event)
        except Exception:
            # Audit is best-effort observability, not an authorization
            # gate: a failing sink must never change a decision already
            # computed above, and must never crash the caller. This is a
            # deliberate, tested, documented trade-off — see
            # test_engine.py's audit-failure tests.
            return


def new_grant_id() -> str:
    """A convenience id generator for callers creating grants."""

    return uuid4().hex


__all__ = [
    "ConfirmationRecord",
    "PermissionEngine",
]
