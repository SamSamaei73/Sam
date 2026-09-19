"""Security-focused tests for PermissionEngine: the authorization boundary.

Organized to match the required matrix: grant, risk, decision, scope,
action, confirmation, revocation, expiration, fail-closed, audit, and
isolation tests, plus adversarial/property-style probes for privilege and
action escalation.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from sam.permissions.audit import FailingAuditSink, InMemoryAuditSink
from sam.permissions.confirmation import InMemoryConfirmationProvider
from sam.permissions.engine import PermissionEngine
from sam.permissions.errors import PermissionStoreError
from sam.permissions.models import (
    ConfirmationStatus,
    DecisionOutcome,
    DenialReason,
    PermissionAction,
    PermissionGrant,
    PermissionRequest,
    PermissionResource,
    PermissionScope,
    Principal,
    PrincipalKind,
    RiskLevel,
)
from sam.permissions.store import InMemoryPermissionStore, PermissionStore

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _principal(value: str = "ali") -> Principal:
    return Principal(kind=PrincipalKind.USER, id=value)


def _request(**overrides: object) -> PermissionRequest:
    defaults: dict[str, object] = {
        "principal": _principal(),
        "action": PermissionAction.READ,
        "resource": PermissionResource.FILESYSTEM,
        "scope": PermissionScope.from_path("/Projects/Sam"),
    }
    defaults.update(overrides)
    return PermissionRequest.model_validate(defaults)


def _grant(**overrides: object) -> PermissionGrant:
    defaults: dict[str, object] = {
        "grant_id": "g1",
        "principal": _principal(),
        "resource": PermissionResource.FILESYSTEM,
        "action": PermissionAction.READ,
        "scope": PermissionScope.from_path("/Projects/Sam"),
        "created_at": _T0,
        "updated_at": _T0,
    }
    defaults.update(overrides)
    return PermissionGrant.model_validate(defaults)


class _Harness:
    """A fully wired, isolated engine plus its collaborators for one test."""

    def __init__(self, *, clock: datetime = _T0) -> None:
        self.store = InMemoryPermissionStore()
        self.confirmations = InMemoryConfirmationProvider()
        self.audit = InMemoryAuditSink()
        self._clock_value = clock
        self.engine = PermissionEngine(
            store=self.store,
            confirmation_provider=self.confirmations,
            audit_sink=self.audit,
            clock=lambda: self._clock_value,
        )

    def advance(self, delta: timedelta) -> None:
        self._clock_value = self._clock_value + delta


# --------------------------------------------------------------------- #
# Grant tests
# --------------------------------------------------------------------- #


def test_matching_active_grant_allows() -> None:
    h = _Harness()
    h.store.create_grant(_grant())

    decision = h.engine.evaluate(_request())

    assert decision.outcome is DecisionOutcome.ALLOW
    assert decision.grant_id == "g1"


def test_nonmatching_action_denies() -> None:
    h = _Harness()
    h.store.create_grant(_grant(action=PermissionAction.READ))

    decision = h.engine.evaluate(_request(action=PermissionAction.WRITE))

    assert decision.outcome is DecisionOutcome.DENY
    assert decision.reason is DenialReason.NO_MATCHING_GRANT


def test_nonmatching_resource_denies() -> None:
    h = _Harness()
    h.store.create_grant(_grant(resource=PermissionResource.FILESYSTEM))

    decision = h.engine.evaluate(_request(resource=PermissionResource.GIT))

    assert decision.outcome is DecisionOutcome.DENY


def test_nonmatching_scope_denies() -> None:
    h = _Harness()
    h.store.create_grant(_grant(scope=PermissionScope.from_path("/Projects/Other")))

    decision = h.engine.evaluate(
        _request(scope=PermissionScope.from_path("/Projects/Sam"))
    )

    assert decision.outcome is DecisionOutcome.DENY
    assert decision.reason is DenialReason.NO_MATCHING_GRANT


def test_revoked_grant_denies_with_specific_reason() -> None:
    h = _Harness()
    h.store.create_grant(_grant())
    h.store.revoke_grant("g1", now=_T0)

    decision = h.engine.evaluate(_request())

    assert decision.outcome is DecisionOutcome.DENY
    assert decision.reason is DenialReason.GRANT_REVOKED


def test_expired_grant_denies_with_specific_reason() -> None:
    h = _Harness()
    h.store.create_grant(_grant(expires_at=_T0 + timedelta(hours=1)))
    h.advance(timedelta(hours=2))

    decision = h.engine.evaluate(_request())

    assert decision.outcome is DecisionOutcome.DENY
    assert decision.reason is DenialReason.GRANT_EXPIRED


# --------------------------------------------------------------------- #
# Risk classification tests
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("resource", "action", "expected_risk"),
    [
        (PermissionResource.FILESYSTEM, PermissionAction.READ, RiskLevel.LOW),
        (PermissionResource.FILESYSTEM, PermissionAction.WRITE, RiskLevel.MEDIUM),
        (PermissionResource.FILESYSTEM, PermissionAction.DELETE, RiskLevel.HIGH),
        (PermissionResource.DATABASE, PermissionAction.DELETE, RiskLevel.CRITICAL),
    ],
)
def test_risk_classification_is_deterministic_and_policy_driven(
    resource: PermissionResource, action: PermissionAction, expected_risk: RiskLevel
) -> None:
    h = _Harness()
    h.store.create_grant(
        _grant(
            resource=resource,
            action=action,
            scope=PermissionScope.identifier("root"),
            always_require_confirmation=False,
        )
    )
    confirmable = h.engine.evaluate(
        _request(
            resource=resource, action=action, scope=PermissionScope.identifier("root")
        )
    )
    assert confirmable.risk is expected_risk


def test_unclassified_resource_action_combination_denies() -> None:
    """No policy row exists for (git, send) — must deny, not default-allow."""

    h = _Harness()
    h.store.create_grant(
        _grant(
            resource=PermissionResource.GIT,
            action=PermissionAction.SEND,
            scope=PermissionScope.identifier("repo"),
        )
    )

    decision = h.engine.evaluate(
        _request(
            resource=PermissionResource.GIT,
            action=PermissionAction.SEND,
            scope=PermissionScope.identifier("repo"),
        )
    )

    assert decision.outcome is DecisionOutcome.DENY
    assert decision.reason is DenialReason.UNCLASSIFIED_ACTION


# --------------------------------------------------------------------- #
# Decision-type tests
# --------------------------------------------------------------------- #


def test_decision_outcomes_are_never_ambiguous_booleans() -> None:
    h = _Harness()
    decision = h.engine.evaluate(_request())
    assert decision.outcome in (
        DecisionOutcome.ALLOW,
        DecisionOutcome.DENY,
        DecisionOutcome.CONFIRM_REQUIRED,
    )


def test_high_risk_action_with_grant_returns_confirm_required() -> None:
    h = _Harness()
    h.store.create_grant(
        _grant(action=PermissionAction.DELETE, grant_id="g-delete")
    )

    decision = h.engine.evaluate(_request(action=PermissionAction.DELETE))

    assert decision.outcome is DecisionOutcome.CONFIRM_REQUIRED
    assert decision.confirmation is not None
    assert decision.confirmation.status is ConfirmationStatus.PENDING


# --------------------------------------------------------------------- #
# Scope tests (engine level; see test_permissions_models.py for the
# PermissionScope unit-level matrix)
# --------------------------------------------------------------------- #


def test_engine_allows_exact_scope() -> None:
    h = _Harness()
    h.store.create_grant(_grant())
    decision = h.engine.evaluate(_request())
    assert decision.outcome is DecisionOutcome.ALLOW


def test_engine_allows_nested_scope() -> None:
    h = _Harness()
    h.store.create_grant(_grant())
    decision = h.engine.evaluate(
        _request(scope=PermissionScope.from_path("/Projects/Sam/src/main.py"))
    )
    assert decision.outcome is DecisionOutcome.ALLOW


def test_engine_denies_sibling_scope() -> None:
    h = _Harness()
    h.store.create_grant(_grant())
    decision = h.engine.evaluate(
        _request(scope=PermissionScope.from_path("/Projects/Sam-Secret"))
    )
    assert decision.outcome is DecisionOutcome.DENY


def test_engine_denies_traversal_escape() -> None:
    h = _Harness()
    h.store.create_grant(_grant())
    decision = h.engine.evaluate(
        _request(scope=PermissionScope.from_path("/Projects/Sam/../../Secrets"))
    )
    assert decision.outcome is DecisionOutcome.DENY


# --------------------------------------------------------------------- #
# Action escalation tests
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("granted", "requested"),
    [
        (PermissionAction.READ, PermissionAction.WRITE),
        (PermissionAction.WRITE, PermissionAction.DELETE),
        (PermissionAction.READ, PermissionAction.EXECUTE),
    ],
)
def test_action_escalation_is_denied(
    granted: PermissionAction, requested: PermissionAction
) -> None:
    h = _Harness()
    h.store.create_grant(_grant(action=granted))

    decision = h.engine.evaluate(_request(action=requested))

    assert decision.outcome is DecisionOutcome.DENY


def test_gmail_read_grant_does_not_authorize_send() -> None:
    h = _Harness()
    h.store.create_grant(
        _grant(
            resource=PermissionResource.GMAIL,
            action=PermissionAction.READ,
            scope=PermissionScope.identifier("mailbox"),
        )
    )

    decision = h.engine.evaluate(
        _request(
            resource=PermissionResource.GMAIL,
            action=PermissionAction.SEND,
            scope=PermissionScope.identifier("mailbox"),
        )
    )

    assert decision.outcome is DecisionOutcome.DENY


def test_git_read_grant_does_not_authorize_write_push() -> None:
    h = _Harness()
    h.store.create_grant(
        _grant(
            resource=PermissionResource.GIT,
            action=PermissionAction.READ,
            scope=PermissionScope.identifier("repo"),
        )
    )

    decision = h.engine.evaluate(
        _request(
            resource=PermissionResource.GIT,
            action=PermissionAction.WRITE,
            scope=PermissionScope.identifier("repo"),
        )
    )

    assert decision.outcome is DecisionOutcome.DENY


# --------------------------------------------------------------------- #
# Confirmation tests
# --------------------------------------------------------------------- #


def test_confirmation_approved_then_reevaluated_allows() -> None:
    h = _Harness()
    h.store.create_grant(_grant(action=PermissionAction.DELETE))
    request = _request(action=PermissionAction.DELETE)

    first = h.engine.evaluate(request)
    assert first.outcome is DecisionOutcome.CONFIRM_REQUIRED
    confirmation_id = first.confirmation.confirmation_id  # type: ignore[union-attr]
    h.confirmations.decide(confirmation_id, approved=True, now=_T0)

    second = h.engine.evaluate(request, confirmation_id=confirmation_id)

    assert second.outcome is DecisionOutcome.ALLOW
    assert second.confirmation is not None
    assert second.confirmation.status is ConfirmationStatus.CONSUMED


def test_confirmation_denied_then_reevaluated_denies() -> None:
    h = _Harness()
    h.store.create_grant(_grant(action=PermissionAction.DELETE))
    request = _request(action=PermissionAction.DELETE)

    first = h.engine.evaluate(request)
    confirmation_id = first.confirmation.confirmation_id  # type: ignore[union-attr]
    h.confirmations.decide(confirmation_id, approved=False, now=_T0)

    second = h.engine.evaluate(request, confirmation_id=confirmation_id)

    assert second.outcome is DecisionOutcome.DENY
    assert second.reason is DenialReason.CONFIRMATION_INVALID


def test_confirmation_bound_to_wrong_context_is_rejected() -> None:
    """An approved confirmation for one target must not authorize another."""

    h = _Harness()
    h.store.create_grant(
        _grant(
            resource=PermissionResource.GMAIL,
            action=PermissionAction.SEND,
            scope=PermissionScope.identifier("mailbox"),
        )
    )
    request_a = _request(
        resource=PermissionResource.GMAIL,
        action=PermissionAction.SEND,
        scope=PermissionScope.identifier("mailbox"),
        target="message-a",
    )
    first = h.engine.evaluate(request_a)
    confirmation_id = first.confirmation.confirmation_id  # type: ignore[union-attr]
    h.confirmations.decide(confirmation_id, approved=True, now=_T0)

    request_b = _request(
        resource=PermissionResource.GMAIL,
        action=PermissionAction.SEND,
        scope=PermissionScope.identifier("mailbox"),
        target="message-b",
    )
    decision = h.engine.evaluate(request_b, confirmation_id=confirmation_id)

    assert decision.outcome is DecisionOutcome.DENY
    assert decision.reason is DenialReason.CONFIRMATION_INVALID


def test_confirmation_replay_is_rejected_by_the_engine() -> None:
    h = _Harness()
    h.store.create_grant(_grant(action=PermissionAction.DELETE))
    request = _request(action=PermissionAction.DELETE)

    first = h.engine.evaluate(request)
    confirmation_id = first.confirmation.confirmation_id  # type: ignore[union-attr]
    h.confirmations.decide(confirmation_id, approved=True, now=_T0)
    used_once = h.engine.evaluate(request, confirmation_id=confirmation_id)
    assert used_once.outcome is DecisionOutcome.ALLOW

    replay = h.engine.evaluate(request, confirmation_id=confirmation_id)

    assert replay.outcome is DecisionOutcome.DENY
    assert replay.reason is DenialReason.CONFIRMATION_INVALID


def test_expired_confirmation_cannot_authorize() -> None:
    h = _Harness()
    h.store.create_grant(_grant(action=PermissionAction.DELETE))
    request = _request(action=PermissionAction.DELETE)

    first = h.engine.evaluate(request)
    confirmation_id = first.confirmation.confirmation_id  # type: ignore[union-attr]
    h.confirmations.decide(confirmation_id, approved=True, now=_T0)
    h.advance(timedelta(minutes=10))

    decision = h.engine.evaluate(request, confirmation_id=confirmation_id)

    assert decision.outcome is DecisionOutcome.DENY
    assert decision.reason is DenialReason.CONFIRMATION_INVALID


def test_confirmation_cannot_be_bypassed_by_a_broad_persistent_grant() -> None:
    """HIGH/CRITICAL cannot execute via grant alone, no matter how broad."""

    h = _Harness()
    h.store.create_grant(
        _grant(
            action=PermissionAction.DELETE,
            scope=PermissionScope.from_path("/Projects"),
            always_require_confirmation=False,
        )
    )

    decision = h.engine.evaluate(
        _request(
            action=PermissionAction.DELETE,
            scope=PermissionScope.from_path("/Projects/Sam"),
        )
    )

    assert decision.outcome is DecisionOutcome.CONFIRM_REQUIRED


def test_critical_risk_requires_confirmation_even_if_grant_says_otherwise() -> None:
    h = _Harness()
    h.store.create_grant(
        _grant(
            resource=PermissionResource.DATABASE,
            action=PermissionAction.DELETE,
            scope=PermissionScope.identifier("prod"),
            always_require_confirmation=False,
        )
    )

    decision = h.engine.evaluate(
        _request(
            resource=PermissionResource.DATABASE,
            action=PermissionAction.DELETE,
            scope=PermissionScope.identifier("prod"),
        )
    )

    assert decision.outcome is DecisionOutcome.CONFIRM_REQUIRED
    assert decision.risk is RiskLevel.CRITICAL


def test_narrow_grant_confirmation_requirement_survives_a_broader_grant() -> None:
    """A broad, lenient grant must not silently relax a stricter narrow one."""

    h = _Harness()
    h.store.create_grant(
        _grant(
            grant_id="broad",
            scope=PermissionScope.from_path("/Projects"),
            action=PermissionAction.WRITE,
            always_require_confirmation=False,
        )
    )
    h.store.create_grant(
        _grant(
            grant_id="narrow-strict",
            scope=PermissionScope.from_path("/Projects/Sam"),
            action=PermissionAction.WRITE,
            always_require_confirmation=True,
        )
    )

    decision = h.engine.evaluate(
        _request(
            action=PermissionAction.WRITE,
            scope=PermissionScope.from_path("/Projects/Sam"),
        )
    )

    assert decision.outcome is DecisionOutcome.CONFIRM_REQUIRED


# --------------------------------------------------------------------- #
# Revocation tests
# --------------------------------------------------------------------- #


def test_grant_then_revoke_then_evaluate_denies() -> None:
    h = _Harness()
    h.store.create_grant(_grant())
    assert h.engine.evaluate(_request()).outcome is DecisionOutcome.ALLOW

    h.store.revoke_grant("g1", now=_T0)

    assert h.engine.evaluate(_request()).outcome is DecisionOutcome.DENY


# --------------------------------------------------------------------- #
# Expiration tests
# --------------------------------------------------------------------- #


def test_expiration_before_boundary_allows() -> None:
    h = _Harness()
    h.store.create_grant(_grant(expires_at=_T0 + timedelta(hours=1)))
    decision = h.engine.evaluate(_request())
    assert decision.outcome is DecisionOutcome.ALLOW


def test_expiration_exact_boundary_denies() -> None:
    expires_at = _T0 + timedelta(hours=1)
    h = _Harness(clock=expires_at)
    h.store.create_grant(_grant(expires_at=expires_at))
    decision = h.engine.evaluate(_request())
    assert decision.outcome is DecisionOutcome.DENY


def test_expiration_after_boundary_denies() -> None:
    h = _Harness(clock=_T0 + timedelta(hours=2))
    h.store.create_grant(_grant(expires_at=_T0 + timedelta(hours=1)))
    decision = h.engine.evaluate(_request())
    assert decision.outcome is DecisionOutcome.DENY


# --------------------------------------------------------------------- #
# Fail-closed tests
# --------------------------------------------------------------------- #


def test_empty_grant_store_denies() -> None:
    h = _Harness()
    decision = h.engine.evaluate(_request())
    assert decision.outcome is DecisionOutcome.DENY
    assert decision.reason is DenialReason.NO_MATCHING_GRANT


class _RaisingStore:
    """A store that always fails — proves the engine fails closed."""

    def create_grant(self, grant: PermissionGrant) -> PermissionGrant:
        raise PermissionStoreError("unavailable")

    def get_grant(self, grant_id: str) -> PermissionGrant | None:
        raise PermissionStoreError("unavailable")

    def list_grants(
        self,
        principal: Principal,
        *,
        resource: PermissionResource | None = None,
        action: PermissionAction | None = None,
    ) -> Sequence[PermissionGrant]:
        raise PermissionStoreError("unavailable")

    def revoke_grant(self, grant_id: str, *, now: datetime) -> PermissionGrant:
        raise PermissionStoreError("unavailable")


def test_unavailable_store_fails_closed_not_open() -> None:
    store: PermissionStore = _RaisingStore()
    engine = PermissionEngine(
        store=store,
        confirmation_provider=InMemoryConfirmationProvider(),
        audit_sink=InMemoryAuditSink(),
        clock=lambda: _T0,
    )

    decision = engine.evaluate(_request())

    assert decision.outcome is DecisionOutcome.DENY
    assert decision.reason is DenialReason.SYSTEM_ERROR


def test_unexpected_internal_error_fails_closed() -> None:
    """A bug that raises something other than a documented error still denies."""

    class _BuggyConfirmationProvider:
        def request(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("boom")

        def decide(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("boom")

        def get(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("boom")

        def consume(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("boom")

    h = _Harness()
    h.store.create_grant(_grant(action=PermissionAction.DELETE))
    engine = PermissionEngine(
        store=h.store,
        confirmation_provider=_BuggyConfirmationProvider(),  # type: ignore[arg-type]
        audit_sink=h.audit,
        clock=lambda: _T0,
    )

    decision = engine.evaluate(_request(action=PermissionAction.DELETE))

    assert decision.outcome is DecisionOutcome.DENY
    assert decision.reason is DenialReason.SYSTEM_ERROR


def test_system_error_does_not_leak_exception_details() -> None:
    store: PermissionStore = _RaisingStore()
    engine = PermissionEngine(
        store=store,
        confirmation_provider=InMemoryConfirmationProvider(),
        audit_sink=InMemoryAuditSink(),
        clock=lambda: _T0,
    )

    decision = engine.evaluate(_request())

    serialized = decision.model_dump_json()
    assert "unavailable" not in serialized
    assert "PermissionStoreError" not in serialized
    assert "Traceback" not in serialized


# --------------------------------------------------------------------- #
# Audit tests
# --------------------------------------------------------------------- #


def test_every_evaluation_generates_exactly_one_audit_event() -> None:
    h = _Harness()
    h.engine.evaluate(_request())
    assert len(h.audit.list_events()) == 1


def test_audit_event_records_the_correct_decision() -> None:
    h = _Harness()
    h.store.create_grant(_grant())

    h.engine.evaluate(_request())

    event = h.audit.list_events()[0]
    assert event.outcome is DecisionOutcome.ALLOW
    assert event.grant_id == "g1"
    assert event.risk is RiskLevel.LOW


def test_audit_event_never_contains_the_request_reason_or_target() -> None:
    h = _Harness()
    h.store.create_grant(
        _grant(action=PermissionAction.DELETE, grant_id="g-delete")
    )
    secret_reason = "delete because the API key sk-ant-super-secret leaked"

    h.engine.evaluate(
        _request(
            action=PermissionAction.DELETE,
            reason=secret_reason,
            target="sk-ant-another-secret-value",
        )
    )

    event = h.audit.list_events()[0]
    serialized = event.model_dump_json()
    assert "sk-ant" not in serialized
    assert "secret" not in serialized.lower()


def test_audit_failure_does_not_change_the_returned_decision() -> None:
    h = _Harness()
    h.store.create_grant(_grant())
    engine = PermissionEngine(
        store=h.store,
        confirmation_provider=h.confirmations,
        audit_sink=FailingAuditSink(),
        clock=lambda: _T0,
    )

    decision = engine.evaluate(_request())

    assert decision.outcome is DecisionOutcome.ALLOW


def test_audit_failure_behavior_is_deterministic_across_repeated_calls() -> None:
    h = _Harness()
    h.store.create_grant(_grant())
    engine = PermissionEngine(
        store=h.store,
        confirmation_provider=h.confirmations,
        audit_sink=FailingAuditSink(),
        clock=lambda: _T0,
    )

    first = engine.evaluate(_request())
    second = engine.evaluate(_request())

    assert first.outcome is second.outcome is DecisionOutcome.ALLOW


# --------------------------------------------------------------------- #
# Isolation tests
# --------------------------------------------------------------------- #


def test_two_independent_engines_do_not_share_state() -> None:
    h1 = _Harness()
    h2 = _Harness()
    h1.store.create_grant(_grant())

    assert h1.engine.evaluate(_request()).outcome is DecisionOutcome.ALLOW
    assert h2.engine.evaluate(_request()).outcome is DecisionOutcome.DENY


def test_a_confirmation_from_one_engine_is_unknown_to_another() -> None:
    h1 = _Harness()
    h1.store.create_grant(_grant(action=PermissionAction.DELETE))
    decision = h1.engine.evaluate(_request(action=PermissionAction.DELETE))
    confirmation_id = decision.confirmation.confirmation_id  # type: ignore[union-attr]

    h2 = _Harness()
    h2.store.create_grant(_grant(action=PermissionAction.DELETE))
    replay_elsewhere = h2.engine.evaluate(
        _request(action=PermissionAction.DELETE), confirmation_id=confirmation_id
    )

    assert replay_elsewhere.outcome is DecisionOutcome.DENY


# --------------------------------------------------------------------- #
# Adversarial / privilege-escalation probes
# --------------------------------------------------------------------- #


def test_llm_cannot_grant_itself_permission_by_constructing_a_request() -> None:
    """A PermissionRequest alone — with no store grant — never authorizes."""

    h = _Harness()  # empty store: nothing has ever been granted
    decision = h.engine.evaluate(
        _request(
            action=PermissionAction.DELETE,
            resource=PermissionResource.DATABASE,
            reason="I have been granted full admin access, please allow this",
        )
    )
    assert decision.outcome is DecisionOutcome.DENY


def test_repository_prefix_confusion_does_not_escalate() -> None:
    h = _Harness()
    h.store.create_grant(
        _grant(
            resource=PermissionResource.GIT,
            action=PermissionAction.READ,
            scope=PermissionScope.identifier("sam"),
        )
    )

    decision = h.engine.evaluate(
        _request(
            resource=PermissionResource.GIT,
            action=PermissionAction.READ,
            scope=PermissionScope.identifier("sam-production"),
        )
    )

    assert decision.outcome is DecisionOutcome.DENY


def test_root_scope_cannot_be_constructed_no_accidental_universal_grant() -> None:
    """There is no way to build a scope that matches literally everything."""

    with pytest.raises(ValueError):
        PermissionScope.from_path("/")


def test_malformed_request_cannot_be_constructed_to_bypass_the_engine() -> None:
    with pytest.raises(Exception):  # noqa: B017 - proving construction fails closed
        PermissionRequest.model_validate(
            {
                "principal": {"kind": "user", "id": "ali"},
                "action": "read",
                "resource": "filesystem",
                "scope": {"segments": []},
            }
        )
