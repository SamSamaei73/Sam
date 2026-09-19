"""Tests for permission domain models: validation, scope semantics, safety."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from sam.permissions.models import (
    AuditEvent,
    ConfirmationRecord,
    ConfirmationStatus,
    DecisionOutcome,
    DenialReason,
    GrantStatus,
    PermissionAction,
    PermissionDecision,
    PermissionGrant,
    PermissionRequest,
    PermissionResource,
    PermissionScope,
    Principal,
    PrincipalKind,
    RiskLevel,
)


def _principal(value: str = "ali") -> Principal:
    return Principal(kind=PrincipalKind.USER, id=value)


# --------------------------------------------------------------------- #
# PermissionScope: the security-critical matching primitive.
# --------------------------------------------------------------------- #


def test_scope_exact_match() -> None:
    grant = PermissionScope.from_path("/Projects/Sam")
    request = PermissionScope.from_path("/Projects/Sam")
    assert grant.contains(request)


def test_scope_nested_descendant_matches() -> None:
    grant = PermissionScope.from_path("/Projects/Sam")
    request = PermissionScope.from_path("/Projects/Sam/src/main.py")
    assert grant.contains(request)


def test_scope_sibling_is_rejected_not_substring_matched() -> None:
    """`/Projects/Sam` must never authorize `/Projects/Sam-Secret`."""

    grant = PermissionScope.from_path("/Projects/Sam")
    request = PermissionScope.from_path("/Projects/Sam-Secret")
    assert not grant.contains(request)


def test_scope_parent_is_not_authorized_by_child() -> None:
    grant = PermissionScope.from_path("/Projects/Sam/src")
    request = PermissionScope.from_path("/Projects/Sam")
    assert not grant.contains(request)


def test_scope_identifier_prefix_confusion_is_rejected() -> None:
    """A grant for repository "sam" must not authorize "sam-production"."""

    grant = PermissionScope.identifier("sam")
    request = PermissionScope.identifier("sam-production")
    assert not grant.contains(request)


def test_scope_traversal_resolves_outside_grant_and_is_denied() -> None:
    grant = PermissionScope.from_path("/Projects/Sam")
    request = PermissionScope.from_path("/Projects/Sam/../../Secrets")
    assert request.segments == ("Secrets",)
    assert not grant.contains(request)


def test_scope_traversal_above_root_raises() -> None:
    with pytest.raises(ValueError, match="traverses above its root"):
        PermissionScope.from_path("../../etc/passwd")


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_scope_from_path_rejects_blank_input(raw: str) -> None:
    with pytest.raises(ValueError):
        PermissionScope.from_path(raw)


def test_scope_from_path_rejects_path_resolving_to_nothing() -> None:
    with pytest.raises(ValueError):
        PermissionScope.from_path("./.")


def test_scope_rejects_unnormalized_segments_directly() -> None:
    with pytest.raises(ValidationError):
        PermissionScope(segments=("Projects", ".."))


def test_scope_rejects_empty_segment() -> None:
    with pytest.raises(ValidationError):
        PermissionScope(segments=("Projects", ""))


def test_scope_wildcard_literal_is_not_special() -> None:
    """A literal "*" segment must behave as an opaque string, not a glob."""

    grant = PermissionScope.identifier("*")
    unrelated_request = PermissionScope.from_path("/Projects")
    assert not grant.contains(unrelated_request)


def test_scope_unicode_forms_are_compared_by_code_point() -> None:
    """Different Unicode normalization forms are treated as different scopes.

    This is a documented, deliberately conservative limitation: it can only
    cause an over-restrictive denial, never an unintended match.
    """

    nfc = PermissionScope.identifier("Café")  # NFC (precomposed é)
    nfd = PermissionScope.identifier("Café")  # NFD (e + combining accent)
    assert nfc.segments != nfd.segments
    assert not nfc.contains(nfd)


def test_scope_as_text_is_bounded_safe_summary() -> None:
    scope = PermissionScope.from_path("/Projects/Sam")
    assert scope.as_text() == "Projects/Sam"


# --------------------------------------------------------------------- #
# PermissionRequest
# --------------------------------------------------------------------- #


def test_request_rejects_unknown_action_string() -> None:
    with pytest.raises(ValidationError):
        PermissionRequest.model_validate(
            {
                "principal": {"kind": "user", "id": "ali"},
                "action": "detonate",
                "resource": "filesystem",
                "scope": {"segments": ["Projects"]},
            }
        )


def test_request_rejects_unknown_resource_string() -> None:
    with pytest.raises(ValidationError):
        PermissionRequest.model_validate(
            {
                "principal": {"kind": "user", "id": "ali"},
                "action": "read",
                "resource": "nuclear_launch",
                "scope": {"segments": ["Projects"]},
            }
        )


def test_request_requires_scope() -> None:
    with pytest.raises(ValidationError):
        PermissionRequest.model_validate(
            {
                "principal": {"kind": "user", "id": "ali"},
                "action": "read",
                "resource": "filesystem",
            }
        )


def test_request_is_frozen() -> None:
    request = PermissionRequest(
        principal=_principal(),
        action=PermissionAction.READ,
        resource=PermissionResource.FILESYSTEM,
        scope=PermissionScope.from_path("/Projects/Sam"),
    )
    with pytest.raises(ValidationError):
        request.action = PermissionAction.DELETE


def test_request_sanitizes_control_characters_in_reason_and_target() -> None:
    request = PermissionRequest(
        principal=_principal(),
        action=PermissionAction.READ,
        resource=PermissionResource.FILESYSTEM,
        scope=PermissionScope.from_path("/Projects/Sam"),
        reason="hello\x00world\x1b[31m",
        target="msg\x07id",
    )
    assert "\x00" not in (request.reason or "")
    assert "\x1b" not in (request.reason or "")
    assert "\x07" not in (request.target or "")


def test_request_blank_reason_becomes_none() -> None:
    request = PermissionRequest(
        principal=_principal(),
        action=PermissionAction.READ,
        resource=PermissionResource.FILESYSTEM,
        scope=PermissionScope.from_path("/Projects/Sam"),
        reason="   ",
    )
    assert request.reason is None


def test_request_reason_is_bounded_length() -> None:
    request = PermissionRequest(
        principal=_principal(),
        action=PermissionAction.READ,
        resource=PermissionResource.FILESYSTEM,
        scope=PermissionScope.from_path("/Projects/Sam"),
        reason="x" * 10_000,
    )
    assert request.reason is not None
    assert len(request.reason) <= 500


# --------------------------------------------------------------------- #
# PermissionGrant
# --------------------------------------------------------------------- #


def _grant(**overrides: object) -> PermissionGrant:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    defaults: dict[str, object] = {
        "grant_id": "g1",
        "principal": _principal(),
        "resource": PermissionResource.FILESYSTEM,
        "action": PermissionAction.READ,
        "scope": PermissionScope.from_path("/Projects/Sam"),
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return PermissionGrant.model_validate(defaults)


def test_grant_rejects_naive_datetimes() -> None:
    with pytest.raises(ValidationError):
        _grant(created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1))


def test_grant_active_status_must_not_carry_revoked_at() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError):
        _grant(status=GrantStatus.ACTIVE, revoked_at=now)


def test_grant_revoked_status_requires_revoked_at() -> None:
    with pytest.raises(ValidationError):
        _grant(status=GrantStatus.REVOKED)


def test_grant_is_usable_true_when_active_and_unexpired() -> None:
    grant = _grant(expires_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert grant.is_usable(now=datetime(2026, 1, 2, tzinfo=UTC))


def test_grant_is_usable_false_when_revoked() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    grant = _grant(status=GrantStatus.REVOKED, revoked_at=now)
    assert not grant.is_usable(now=now)


def test_grant_expiration_boundary_is_inclusive_of_expiry() -> None:
    expires_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    grant = _grant(expires_at=expires_at)
    assert grant.is_usable(now=expires_at - timedelta(seconds=1))
    assert not grant.is_usable(now=expires_at)
    assert not grant.is_usable(now=expires_at + timedelta(seconds=1))


def test_grant_is_frozen() -> None:
    grant = _grant()
    with pytest.raises(ValidationError):
        grant.status = GrantStatus.REVOKED


# --------------------------------------------------------------------- #
# ConfirmationRecord
# --------------------------------------------------------------------- #


def _confirmation_record(**overrides: object) -> ConfirmationRecord:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    defaults: dict[str, object] = {
        "confirmation_id": "c1",
        "principal": _principal(),
        "action": PermissionAction.SEND,
        "resource": PermissionResource.GMAIL,
        "scope": PermissionScope.identifier("mailbox"),
        "target": "message-a",
        "risk": RiskLevel.HIGH,
        "status": ConfirmationStatus.APPROVED,
        "created_at": now,
        "expires_at": now + timedelta(minutes=5),
    }
    defaults.update(overrides)
    return ConfirmationRecord.model_validate(defaults)


def test_confirmation_matches_identical_request() -> None:
    record = _confirmation_record()
    request = PermissionRequest(
        principal=_principal(),
        action=PermissionAction.SEND,
        resource=PermissionResource.GMAIL,
        scope=PermissionScope.identifier("mailbox"),
        target="message-a",
    )
    assert record.matches(request)


def test_confirmation_does_not_match_different_target() -> None:
    """A confirmation for message A must not authorize message B."""

    record = _confirmation_record(target="message-a")
    request = PermissionRequest(
        principal=_principal(),
        action=PermissionAction.SEND,
        resource=PermissionResource.GMAIL,
        scope=PermissionScope.identifier("mailbox"),
        target="message-b",
    )
    assert not record.matches(request)


def test_confirmation_does_not_match_different_action() -> None:
    record = _confirmation_record()
    request = PermissionRequest(
        principal=_principal(),
        action=PermissionAction.DELETE,
        resource=PermissionResource.GMAIL,
        scope=PermissionScope.identifier("mailbox"),
        target="message-a",
    )
    assert not record.matches(request)


def test_confirmation_rejects_naive_datetimes() -> None:
    now = datetime(2026, 1, 1)
    with pytest.raises(ValidationError):
        _confirmation_record(created_at=now, expires_at=now)


# --------------------------------------------------------------------- #
# PermissionDecision shape invariants
# --------------------------------------------------------------------- #


def _request() -> PermissionRequest:
    return PermissionRequest(
        principal=_principal(),
        action=PermissionAction.READ,
        resource=PermissionResource.FILESYSTEM,
        scope=PermissionScope.from_path("/Projects/Sam"),
    )


def test_deny_decision_requires_a_reason() -> None:
    with pytest.raises(ValidationError):
        PermissionDecision(
            outcome=DecisionOutcome.DENY,
            request=_request(),
            risk=RiskLevel.LOW,
            decided_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_allow_decision_must_not_carry_a_denial_reason() -> None:
    with pytest.raises(ValidationError):
        PermissionDecision(
            outcome=DecisionOutcome.ALLOW,
            request=_request(),
            risk=RiskLevel.LOW,
            decided_at=datetime(2026, 1, 1, tzinfo=UTC),
            reason=DenialReason.NO_MATCHING_GRANT,
        )


def test_confirm_required_decision_must_carry_a_confirmation() -> None:
    with pytest.raises(ValidationError):
        PermissionDecision(
            outcome=DecisionOutcome.CONFIRM_REQUIRED,
            request=_request(),
            risk=RiskLevel.HIGH,
            decided_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_decision_is_frozen() -> None:
    decision = PermissionDecision(
        outcome=DecisionOutcome.DENY,
        request=_request(),
        risk=RiskLevel.LOW,
        decided_at=datetime(2026, 1, 1, tzinfo=UTC),
        reason=DenialReason.NO_MATCHING_GRANT,
    )
    with pytest.raises(ValidationError):
        decision.outcome = DecisionOutcome.ALLOW


# --------------------------------------------------------------------- #
# AuditEvent: never carries free-text request content.
# --------------------------------------------------------------------- #


def test_audit_event_has_no_field_for_free_text_reason_or_target() -> None:
    field_names = set(AuditEvent.model_fields)
    assert "reason" in field_names  # only the closed-enum DenialReason
    assert "target" not in field_names
    assert "prompt" not in field_names


def test_audit_event_reason_must_be_a_denial_reason_not_free_text() -> None:
    with pytest.raises(ValidationError):
        AuditEvent(
            event_id="e1",
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            principal=_principal(),
            action=PermissionAction.READ,
            resource=PermissionResource.FILESYSTEM,
            scope_summary="Projects/Sam",
            risk=RiskLevel.LOW,
            outcome=DecisionOutcome.DENY,
            reason="the user's secret private note",  # type: ignore[arg-type]
        )


def test_audit_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        AuditEvent(
            event_id="e1",
            occurred_at=datetime(2026, 1, 1),
            principal=_principal(),
            action=PermissionAction.READ,
            resource=PermissionResource.FILESYSTEM,
            scope_summary="Projects/Sam",
            risk=RiskLevel.LOW,
            outcome=DecisionOutcome.ALLOW,
        )
