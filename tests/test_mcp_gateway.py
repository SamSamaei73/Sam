"""Lifecycle tests for sam.mcp.gateway.MCPGateway.

The gateway is exercised end to end against the *real* PermissionEngine,
the real registry, real validation, and fake transports/credentials/
verifiers. No test bypasses authorization to fake a passing result.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Any

import pytest

from sam.mcp.audit import FailingMCPAuditSink, InMemoryMCPAuditSink
from sam.mcp.errors import MCPTimeoutError
from sam.mcp.gateway import MCPGateway
from sam.mcp.models import (
    MCPErrorCategory,
    MCPExecutionStatus,
    MCPToolRequest,
    PermissionOutcomeSummary,
    VerificationStatus,
)
from sam.mcp.verification import FakeMCPVerifier
from sam.permissions.models import PermissionAction, PermissionResource
from tests.mcp_support import (
    ALICE,
    BOB,
    NOW,
    SECRET_MAIL,
    Harness,
    list_events_tool,
    post_request,
    read_message_tool,
    read_request,
    send_request,
)

S = MCPExecutionStatus
C = MCPErrorCategory


def _harness_with_verified_send() -> Harness:
    return Harness(verifiers={"mail:send_message": FakeMCPVerifier()})


# ===================================================================== happy


class TestHappyPath:
    def test_authorized_low_risk_read_executes(self) -> None:
        h = Harness()
        h.grant_mail_read()
        result = h.gateway.execute(read_request())
        assert result.status is S.SUCCEEDED
        assert result.permission_outcome is PermissionOutcomeSummary.ALLOW
        assert result.execution_attempted is True
        assert result.verification is not None
        assert result.verification.status is VerificationStatus.NOT_REQUIRED
        assert result.result is not None
        assert result.result.content == {
            "id": "m-1",
            "subject": "Hello",
            "body": "hi there",
        }
        assert h.transports["mail"].call_count == 1

    def test_result_carries_provenance_and_untrusted_flag(self) -> None:
        h = Harness()
        h.grant_mail_read()
        result = h.gateway.execute(read_request())
        assert result.result is not None
        assert result.result.server_id == "mail"
        assert result.result.tool_id == "mail:read_message"
        assert result.result.execution_id == result.execution_id
        assert result.result.untrusted_external_content is True

    def test_ids_are_preserved_across_authorization_execution_and_audit(self) -> None:
        h = Harness()
        h.grant_mail_read()
        result = h.gateway.execute(read_request(request_id="req-42"))
        assert result.request_id == "req-42"
        [event] = h.audit_events()
        assert event.request_id == "req-42"
        assert event.execution_id == result.execution_id
        # The permission engine saw the same correlation id.
        assert h.permission_audit.list_events()[-1].correlation_id == "req-42"

    def test_server_level_grant_covers_its_tools_and_accounts(self) -> None:
        h = Harness()
        h.grant(PermissionResource.GMAIL, PermissionAction.READ, "mail")
        assert h.gateway.execute(read_request()).status is S.SUCCEEDED
        other = read_request(arguments={"account": "acct-9", "message_id": "m"})
        assert h.gateway.execute(other).status is S.SUCCEEDED

    def test_multiple_servers_work_independently(self) -> None:
        h = Harness()
        h.grant_mail_read()
        h.grant_calendar()
        assert h.gateway.execute(read_request()).status is S.SUCCEEDED
        cal = MCPToolRequest(
            principal=ALICE,
            tool_id="calendar:list_events",
            arguments={"calendar_id": "cal-1"},
        )
        assert h.gateway.execute(cal).status is S.SUCCEEDED
        assert h.transports["mail"].call_count == 1
        assert h.transports["calendar"].call_count == 1
        assert h.transports["social"].call_count == 0


# ============================================================ identity/registry


class TestResolution:
    @pytest.mark.parametrize(
        "tool_id",
        ["", "nocolon", "a:b:c", "Mail:read_message", "mail:read message", "../x:y"],
    )
    def test_malformed_tool_ids_rejected_before_anything_else(
        self, tool_id: str
    ) -> None:
        h = Harness()
        h.grant_mail_read()
        result = h.gateway.execute(
            MCPToolRequest(principal=ALICE, tool_id=tool_id or "x", arguments={})
            if tool_id
            else read_request(tool_id="x")
        )
        assert result.status is S.REJECTED
        assert result.error_category in (C.INVALID_TOOL_ID, C.UNKNOWN_TOOL)
        assert result.permission_outcome is None
        assert h.transports["mail"].call_count == 0

    def test_unknown_tool_rejected(self) -> None:
        h = Harness()
        result = h.gateway.execute(read_request(tool_id="mail:ghost"))
        assert result.status is S.REJECTED
        assert result.error_category is C.UNKNOWN_TOOL
        assert result.execution_attempted is False

    def test_unqualified_tool_name_is_never_resolved(self) -> None:
        h = Harness()
        h.grant_mail_read()
        result = h.gateway.execute(read_request(tool_id="read_message"))
        assert result.status is S.REJECTED
        assert h.transports["mail"].call_count == 0

    def test_same_tool_name_resolves_only_within_the_named_server(self) -> None:
        h = Harness(
            extra_tools=(list_events_tool(tool_name="read_message"),),
        )
        h.grant_mail_read()
        result = h.gateway.execute(read_request())
        assert result.status is S.SUCCEEDED
        assert h.transports["mail"].call_count == 1
        assert h.transports["calendar"].call_count == 0

    def test_disabled_tool_rejected_even_with_a_grant(self) -> None:
        h = Harness()
        h.grant_mail_read()
        h.admin.disable("mail:read_message")
        result = h.gateway.execute(read_request())
        assert result.status is S.REJECTED
        assert result.error_category is C.TOOL_DISABLED
        assert h.transports["mail"].call_count == 0

    def test_reenabling_a_tool_does_not_grant_permission(self) -> None:
        h = Harness()
        h.admin.disable("mail:read_message")
        h.admin.enable("mail:read_message")
        result = h.gateway.execute(read_request())
        assert result.status is S.DENIED

    def test_disabled_flag_cannot_override_a_denial(self) -> None:
        h = Harness()
        # Enabled tool + no grant: the PermissionEngine's DENY stands.
        assert h.registry.get_tool("mail:read_message").enabled is True
        assert h.gateway.execute(read_request()).status is S.DENIED


# ============================================================== permissions


class TestPermissionEngineIsTheAuthority:
    def test_no_grant_is_denied_and_nothing_runs(self) -> None:
        h = Harness()
        result = h.gateway.execute(read_request())
        assert result.status is S.DENIED
        assert result.error_category is C.PERMISSION_DENIED
        assert result.permission_outcome is PermissionOutcomeSummary.DENY
        assert result.execution_attempted is False
        assert h.transports["mail"].call_count == 0

    def test_credential_is_not_resolved_before_authorization(self) -> None:
        h = Harness()
        h.gateway.execute(read_request())
        assert h.credentials.resolve_count == 0

    def test_grant_for_a_different_account_scope_is_denied(self) -> None:
        h = Harness()
        h.grant_mail_read("acct-1")
        other = read_request(arguments={"account": "acct-2", "message_id": "m"})
        assert h.gateway.execute(other).status is S.DENIED
        assert h.transports["mail"].call_count == 0

    def test_grant_for_a_different_tool_on_the_same_server_is_denied(self) -> None:
        h = Harness()
        h.grant(PermissionResource.GMAIL, PermissionAction.READ, "mail", "other_tool")
        assert h.gateway.execute(read_request()).status is S.DENIED

    def test_grant_for_a_different_server_is_denied(self) -> None:
        h = Harness()
        h.grant(PermissionResource.GMAIL, PermissionAction.READ, "calendar")
        assert h.gateway.execute(read_request()).status is S.DENIED

    def test_native_style_grant_without_server_prefix_does_not_match(self) -> None:
        h = Harness()
        h.grant(PermissionResource.GMAIL, PermissionAction.READ, "acct-1")
        assert h.gateway.execute(read_request()).status is S.DENIED

    def test_grant_for_a_different_action_is_denied(self) -> None:
        h = Harness()
        h.grant(PermissionResource.GMAIL, PermissionAction.READ, "mail")
        assert h.gateway.execute(send_request()).status is S.DENIED

    def test_grant_for_a_different_principal_is_denied(self) -> None:
        h = Harness()
        h.grant_mail_read()
        assert h.gateway.execute(read_request(principal=BOB)).status is S.DENIED

    def test_revoked_grant_is_denied(self) -> None:
        h = Harness()
        grant_id = h.grant_mail_read()
        h.pstore.revoke_grant(grant_id, now=NOW)
        result = h.gateway.execute(read_request())
        assert result.status is S.DENIED
        assert h.transports["mail"].call_count == 0

    def test_expired_grant_is_denied(self) -> None:
        h = Harness()
        h.grant(
            PermissionResource.GMAIL,
            PermissionAction.READ,
            "mail",
            "read_message",
            "acct-1",
            expires_at=NOW - timedelta(days=1),
        )
        assert h.gateway.execute(read_request()).status is S.DENIED

    def test_engine_that_raises_fails_closed(self) -> None:
        h = Harness()
        h.grant_mail_read()

        class Broken:
            def evaluate(self, *a: Any, **k: Any) -> object:
                raise RuntimeError("boom")

        gateway = MCPGateway(
            registry=h.registry,
            permission_engine=Broken(),  # type: ignore[arg-type]
            transports=h.transports,
            credential_provider=h.credentials,
            audit_sink=h.audit,
        )
        result = gateway.execute(read_request())
        assert result.status is S.FAILED
        assert result.error_category is C.INTERNAL_ERROR
        assert h.transports["mail"].call_count == 0

    def test_unbindable_resources_cannot_be_reached_via_a_registered_tool(self) -> None:
        # MCP tools cannot be bound to FILESYSTEM; such a descriptor cannot
        # exist, so there is no route to a filesystem grant at all.
        from pydantic import ValidationError

        from sam.mcp.models import MCPToolPermissionBinding

        with pytest.raises(ValidationError):
            MCPToolPermissionBinding(
                resource=PermissionResource.FILESYSTEM, action=PermissionAction.READ
            )

    def test_permission_request_uses_only_trusted_binding(self) -> None:
        h = Harness()
        h.grant_mail_read()
        h.gateway.execute(read_request())
        event = h.permission_audit.list_events()[-1]
        assert event.resource is PermissionResource.GMAIL
        assert event.action is PermissionAction.READ
        assert event.scope_summary == "mail/read_message/acct-1"


# ============================================================== confirmation


class TestConfirmationBoundary:
    def test_high_risk_send_requires_confirmation_and_runs_nothing(self) -> None:
        h = _harness_with_verified_send()
        h.grant_mail_send()
        result = h.gateway.execute(send_request())
        assert result.status is S.CONFIRMATION_REQUIRED
        assert result.permission_outcome is PermissionOutcomeSummary.CONFIRM_REQUIRED
        assert result.confirmation_id
        assert result.execution_attempted is False
        assert h.transports["mail"].call_count == 0
        assert h.credentials.resolve_count == 0

    def test_approved_confirmation_lets_it_run_exactly_once(self) -> None:
        h = _harness_with_verified_send()
        h.grant_mail_send()
        pending = h.gateway.execute(send_request(request_id="r1"))
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        done = h.gateway.execute(
            send_request(request_id="r1"), confirmation_id=pending.confirmation_id
        )
        assert done.status is S.SUCCEEDED
        assert done.verification is not None
        assert done.verification.status is VerificationStatus.VERIFIED
        assert h.transports["mail"].call_count == 1

    def test_unapproved_pending_confirmation_is_denied(self) -> None:
        h = _harness_with_verified_send()
        h.grant_mail_send()
        pending = h.gateway.execute(send_request())
        result = h.gateway.execute(
            send_request(), confirmation_id=pending.confirmation_id
        )
        assert result.status is S.DENIED
        assert result.error_category is C.CONFIRMATION_INVALID
        assert h.transports["mail"].call_count == 0

    def test_rejected_confirmation_is_denied(self) -> None:
        h = _harness_with_verified_send()
        h.grant_mail_send()
        pending = h.gateway.execute(send_request())
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id, approved=False)
        result = h.gateway.execute(
            send_request(), confirmation_id=pending.confirmation_id
        )
        assert result.status is S.DENIED
        assert h.transports["mail"].call_count == 0

    def test_confirmation_cannot_be_replayed(self) -> None:
        h = _harness_with_verified_send()
        h.grant_mail_send()
        pending = h.gateway.execute(send_request())
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        first = h.gateway.execute(
            send_request(), confirmation_id=pending.confirmation_id
        )
        assert first.status is S.SUCCEEDED
        second = h.gateway.execute(
            send_request(), confirmation_id=pending.confirmation_id
        )
        assert second.status is S.DENIED
        assert second.error_category is C.CONFIRMATION_INVALID
        assert h.transports["mail"].call_count == 1

    def test_confirmation_is_bound_to_the_exact_arguments(self) -> None:
        h = _harness_with_verified_send()
        h.grant_mail_send()
        pending = h.gateway.execute(send_request(body="approved text"))
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        swapped = h.gateway.execute(
            send_request(body="DIFFERENT text"), confirmation_id=pending.confirmation_id
        )
        assert swapped.status is S.DENIED
        assert swapped.error_category is C.CONFIRMATION_INVALID
        assert h.transports["mail"].call_count == 0
        # ... and the confirmation is still usable for what was approved.
        ok = h.gateway.execute(
            send_request(body="approved text"), confirmation_id=pending.confirmation_id
        )
        assert ok.status is S.SUCCEEDED

    def test_confirmation_is_bound_to_the_account_scope(self) -> None:
        h = _harness_with_verified_send()
        h.grant_mail_send("acct-1")
        h.grant_mail_send("acct-2")
        pending = h.gateway.execute(send_request(account="acct-1"))
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        result = h.gateway.execute(
            send_request(account="acct-2"), confirmation_id=pending.confirmation_id
        )
        assert result.status is S.DENIED

    def test_confirmation_is_bound_to_the_principal(self) -> None:
        h = _harness_with_verified_send()
        h.grant_mail_send()
        h.grant(
            PermissionResource.GMAIL,
            PermissionAction.SEND,
            "mail",
            "send_message",
            "acct-1",
            principal=BOB,
        )
        pending = h.gateway.execute(send_request())
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        result = h.gateway.execute(
            send_request(principal=BOB), confirmation_id=pending.confirmation_id
        )
        assert result.status is S.DENIED

    def test_unknown_confirmation_id_is_denied(self) -> None:
        h = _harness_with_verified_send()
        h.grant_mail_send()
        result = h.gateway.execute(send_request(), confirmation_id="does-not-exist")
        assert result.status is S.DENIED
        assert h.transports["mail"].call_count == 0

    def test_confirmation_for_one_tool_cannot_authorize_another(self) -> None:
        h = Harness(verifiers={"social:create_post": FakeMCPVerifier()})
        h.grant_mail_send()
        h.grant_post()
        pending = h.gateway.execute(send_request())
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        result = h.gateway.execute(
            post_request(), confirmation_id=pending.confirmation_id
        )
        assert result.status is S.DENIED
        assert h.transports["social"].call_count == 0

    def test_grant_can_add_but_never_waive_confirmation(self) -> None:
        h = Harness()
        h.grant(
            PermissionResource.GMAIL,
            PermissionAction.READ,
            "mail",
            always_confirm=True,
        )
        result = h.gateway.execute(read_request())
        assert result.status is S.CONFIRMATION_REQUIRED

    def test_confirmation_flow_keeps_the_same_request_id_usable(self) -> None:
        h = _harness_with_verified_send()
        h.grant_mail_send()
        first = h.gateway.execute(send_request(request_id="same"))
        assert first.status is S.CONFIRMATION_REQUIRED
        again = h.gateway.execute(send_request(request_id="same"))
        assert again.status is S.CONFIRMATION_REQUIRED  # not treated as a duplicate

    def test_confirmation_record_contains_no_argument_content(self) -> None:
        h = _harness_with_verified_send()
        h.grant_mail_send()
        h.gateway.execute(send_request(body="very private body text"))
        dumped = " ".join(str(r) for r in h.confirmations._list_for_test())
        assert "very private body text" not in dumped
        assert "friend@example.test" not in dumped
        assert "mail:send_message #" in dumped


# ==================================================================== input


class TestInputHandling:
    def test_oversized_input_rejected(self) -> None:
        h = Harness(
            extra_tools=(read_message_tool(tool_name="tiny", max_input_bytes=50),),
            handlers={"mail": {"tiny": lambda a, c: 1}},
        )
        h.grant(PermissionResource.GMAIL, PermissionAction.READ, "mail")
        req = read_request(
            tool_id="mail:tiny", arguments={"account": "a", "message_id": "m" * 60}
        )
        result = h.gateway.execute(req)
        assert result.status is S.REJECTED
        assert result.error_category is C.INPUT_TOO_LARGE
        assert h.transports["mail"].call_count == 0

    def test_deeply_nested_input_rejected(self) -> None:
        h = Harness()
        h.grant_mail_read()
        node: Any = "x"
        for _ in range(3000):
            node = [node]
        req = read_request(arguments={"account": "acct-1", "message_id": node})
        result = h.gateway.execute(req)
        assert result.status is S.REJECTED
        assert h.transports["mail"].call_count == 0

    def test_unknown_or_missing_arguments_rejected_before_authorization(self) -> None:
        h = Harness()
        h.grant_mail_read()
        for args in (
            {"account": "acct-1"},
            {"account": "acct-1", "message_id": "m", "x": 1},
        ):
            result = h.gateway.execute(read_request(arguments=args))
            assert result.status is S.REJECTED
            assert result.permission_outcome is None
        assert h.transports["mail"].call_count == 0

    def test_secret_looking_argument_rejected(self) -> None:
        h = Harness()
        h.grant_mail_read()
        req = read_request(
            arguments={"account": "acct-1", "message_id": "sk-ant-abcdefghijklmnopqrst"}
        )
        result = h.gateway.execute(req)
        assert result.status is S.REJECTED
        assert result.error_category is C.SECRET_IN_INPUT
        assert h.transports["mail"].call_count == 0

    @pytest.mark.parametrize(
        "account",
        ["a/b", "a b", "..", ".", "a:b", "a\nb", "é", "", "x" * 129, "a\\b"],
    )
    def test_scope_bearing_argument_must_be_a_safe_segment(self, account: str) -> None:
        h = Harness()
        h.grant(PermissionResource.GMAIL, PermissionAction.READ, "mail")
        req = read_request(arguments={"account": account, "message_id": "m"})
        result = h.gateway.execute(req)
        assert result.status is S.REJECTED
        assert h.transports["mail"].call_count == 0

    def test_scope_cannot_be_widened_by_a_traversal_like_argument(self) -> None:
        h = Harness()
        h.grant_mail_read("acct-1")
        req = read_request(arguments={"account": "acct-1/../acct-2", "message_id": "m"})
        assert h.gateway.execute(req).status is S.REJECTED

    def test_arguments_are_copied_so_later_mutation_cannot_change_execution(
        self,
    ) -> None:
        h = Harness()
        h.grant_mail_read()
        args = {"account": "acct-1", "message_id": "m-1"}
        request = read_request(arguments=args)
        args["message_id"] = "tampered"
        # The request model holds its own dict copy taken at construction.
        result = h.gateway.execute(request)
        assert result.status is S.SUCCEEDED
        assert h.transports["mail"].calls[0][1]["message_id"] == "m-1"


# ================================================================ execution


class TestExecution:
    def test_transport_exception_is_contained_and_not_leaked(self) -> None:
        def boom(args: Any, cred: Any) -> object:
            raise RuntimeError(f"internal detail {SECRET_MAIL}")

        h = Harness(handlers={"mail": {"read_message": boom}})
        h.grant_mail_read()
        result = h.gateway.execute(read_request())
        assert result.status is S.FAILED
        assert result.error_category is C.TRANSPORT_ERROR
        assert result.execution_attempted is True
        assert SECRET_MAIL not in result.model_dump_json()
        assert h.transports["mail"].call_count == 1

    def test_timeout_is_enforced_and_late_result_is_ignored(self) -> None:
        release = threading.Event()

        def slow(args: Any, cred: Any) -> object:
            release.wait(2.0)
            return {"late": True}

        h = Harness(
            handlers={"mail": {"slow_read": slow}},
            extra_tools=(
                read_message_tool(tool_name="slow_read", timeout_seconds=0.05),
            ),
        )
        h.grant(PermissionResource.GMAIL, PermissionAction.READ, "mail")
        started = time.monotonic()
        try:
            result = h.gateway.execute(read_request(tool_id="mail:slow_read"))
        finally:
            release.set()
        assert time.monotonic() - started < 1.5
        assert result.status is S.FAILED
        assert result.error_category is C.TIMEOUT
        assert result.result is None
        assert result.execution_attempted is True

    def test_transport_reported_timeout_maps_to_timeout(self) -> None:
        def raises_timeout(args: Any, cred: Any) -> object:
            raise MCPTimeoutError("upstream timeout")

        h = Harness(handlers={"mail": {"read_message": raises_timeout}})
        h.grant_mail_read()
        assert h.gateway.execute(read_request()).error_category is C.TIMEOUT

    def test_timeout_is_owned_by_the_registry_not_the_result(self) -> None:
        h = Harness(handlers={"mail": {"read_message": lambda a, c: {"timeout": 9999}}})
        h.grant_mail_read()
        result = h.gateway.execute(read_request())
        assert result.status is S.SUCCEEDED
        assert h.registry.get_tool("mail:read_message").timeout_seconds == 2.0

    def test_huge_result_rejected_not_truncated(self) -> None:
        h = Harness(
            extra_tools=(read_message_tool(tool_name="big", max_output_bytes=200),),
            handlers={"mail": {"big": lambda a, c: {"data": "x" * 5000}}},
        )
        h.grant(PermissionResource.GMAIL, PermissionAction.READ, "mail")
        result = h.gateway.execute(read_request(tool_id="mail:big"))
        assert result.status is S.FAILED
        assert result.error_category is C.OUTPUT_TOO_LARGE
        assert result.result is None

    @pytest.mark.parametrize(
        "bad", [b"bytes", {1, 2}, object(), ("t",), float("nan"), {1: "x"}]
    )
    def test_malformed_result_rejected(self, bad: object) -> None:
        h = Harness(handlers={"mail": {"read_message": lambda a, c: bad}})
        h.grant_mail_read()
        result = h.gateway.execute(read_request())
        assert result.status is S.FAILED
        assert result.error_category is C.OUTPUT_INVALID

    def test_deeply_nested_result_rejected(self) -> None:
        node: Any = 1
        for _ in range(500):
            node = [node]
        h = Harness(handlers={"mail": {"read_message": lambda a, c: node}})
        h.grant_mail_read()
        assert h.gateway.execute(read_request()).error_category is C.OUTPUT_INVALID

    def test_no_transport_registered_for_server_fails_closed(self) -> None:
        h = Harness()
        h.grant_mail_read()
        gateway = MCPGateway(
            registry=h.registry,
            permission_engine=h.pengine,
            transports={},
            credential_provider=h.credentials,
        )
        result = gateway.execute(read_request())
        assert result.status is S.FAILED
        assert result.error_category is C.TRANSPORT_ERROR

    def test_missing_credential_fails_before_any_transport_call(self) -> None:
        h = Harness()
        h.grant_mail_read()
        h.credentials = type(h.credentials)()  # empty provider
        result = h.rebuild_gateway().execute(read_request())
        assert result.status is S.FAILED
        assert result.error_category is C.CREDENTIAL_ERROR
        assert result.execution_attempted is False
        assert h.transports["mail"].call_count == 0

    def test_credential_provider_that_raises_is_contained(self) -> None:
        h = Harness()
        h.grant_mail_read()

        class Bad:
            def resolve(self, *a: Any, **k: Any) -> object:
                raise RuntimeError(f"vault down {SECRET_MAIL}")

        h.credentials = Bad()  # type: ignore[assignment]
        result = h.rebuild_gateway().execute(read_request())
        assert result.error_category is C.CREDENTIAL_ERROR
        assert SECRET_MAIL not in result.model_dump_json()

    def test_tool_without_credential_reference_gets_none(self) -> None:
        seen: list[object] = []

        def handler(args: Any, cred: Any) -> object:
            seen.append(cred)
            return {"ok": 1}

        h = Harness(
            extra_tools=(read_message_tool(tool_name="open_read", credential=None),),
            handlers={"mail": {"open_read": handler}},
        )
        h.grant(PermissionResource.GMAIL, PermissionAction.READ, "mail")
        assert (
            h.gateway.execute(read_request(tool_id="mail:open_read")).status
            is S.SUCCEEDED
        )
        assert seen == [None]
        assert h.credentials.resolve_count == 0


# ======================================================= exactly-once / dedupe


class TestExactlyOnce:
    def test_one_request_causes_exactly_one_transport_call(self) -> None:
        h = Harness()
        h.grant_mail_read()
        h.gateway.execute(read_request())
        assert h.transports["mail"].call_count == 1

    def test_failed_transport_call_is_never_retried(self) -> None:
        def boom(args: Any, cred: Any) -> object:
            raise RuntimeError("x")

        h = Harness(handlers={"mail": {"read_message": boom}})
        h.grant_mail_read()
        h.gateway.execute(read_request())
        assert h.transports["mail"].call_count == 1

    def test_timed_out_call_is_never_retried(self) -> None:
        release = threading.Event()
        h = Harness(
            handlers={"mail": {"slow_read": lambda a, c: release.wait(1.0)}},
            extra_tools=(
                read_message_tool(tool_name="slow_read", timeout_seconds=0.05),
            ),
        )
        h.grant(PermissionResource.GMAIL, PermissionAction.READ, "mail")
        try:
            h.gateway.execute(read_request(tool_id="mail:slow_read"))
        finally:
            release.set()
        assert h.transports["mail"].call_count == 1

    def test_reusing_a_request_id_after_execution_is_rejected(self) -> None:
        h = Harness()
        h.grant_mail_read()
        first = h.gateway.execute(read_request(request_id="dup"))
        second = h.gateway.execute(read_request(request_id="dup"))
        assert first.status is S.SUCCEEDED
        assert second.status is S.REJECTED
        assert second.error_category is C.DUPLICATE_REQUEST
        assert second.execution_attempted is False
        assert h.transports["mail"].call_count == 1

    def test_request_id_reuse_after_a_failed_attempt_is_also_rejected(self) -> None:
        state = {"n": 0}

        def flaky(args: Any, cred: Any) -> object:
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("x")
            return {"ok": 1}

        h = Harness(handlers={"mail": {"read_message": flaky}})
        h.grant_mail_read()
        assert h.gateway.execute(read_request(request_id="r")).status is S.FAILED
        again = h.gateway.execute(read_request(request_id="r"))
        assert again.error_category is C.DUPLICATE_REQUEST
        assert state["n"] == 1  # a caller must use a NEW request id to retry

    def test_denied_requests_do_not_consume_the_request_id(self) -> None:
        h = Harness()
        assert h.gateway.execute(read_request(request_id="r")).status is S.DENIED
        h.grant_mail_read()
        assert h.gateway.execute(read_request(request_id="r")).status is S.SUCCEEDED

    def test_request_ids_are_scoped_per_principal(self) -> None:
        h = Harness()
        h.grant_mail_read()
        h.grant(
            PermissionResource.GMAIL,
            PermissionAction.READ,
            "mail",
            principal=BOB,
        )
        assert h.gateway.execute(read_request(request_id="x")).status is S.SUCCEEDED
        assert (
            h.gateway.execute(read_request(request_id="x", principal=BOB)).status
            is S.SUCCEEDED
        )

    def test_concurrent_identical_requests_execute_at_most_once(self) -> None:
        gate = threading.Event()

        def slowish(args: Any, cred: Any) -> object:
            gate.wait(0.2)
            return {"ok": 1}

        h = Harness(handlers={"mail": {"read_message": slowish}})
        h.grant_mail_read()
        results: list[Any] = []

        def run() -> None:
            results.append(h.gateway.execute(read_request(request_id="race")))

        threads = [threading.Thread(target=run) for _ in range(6)]
        for t in threads:
            t.start()
        gate.set()
        for t in threads:
            t.join(5)
        assert h.transports["mail"].call_count == 1
        assert sum(r.status is S.SUCCEEDED for r in results) == 1


# ============================================================== verification


class TestVerification:
    def test_verified_side_effect_is_reported_verified(self) -> None:
        h = _harness_with_verified_send()
        h.grant_mail_send()
        pending = h.gateway.execute(send_request())
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        done = h.gateway.execute(
            send_request(), confirmation_id=pending.confirmation_id
        )
        assert done.verification is not None
        assert done.verification.status is VerificationStatus.VERIFIED

    def _run_send(self, h: Harness) -> Any:
        h.grant_mail_send()
        pending = h.gateway.execute(send_request())
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        return h.gateway.execute(
            send_request(), confirmation_id=pending.confirmation_id
        )

    def test_required_but_no_verifier_is_unverified_never_success(self) -> None:
        h = Harness()  # send requires verification, none registered
        result = self._run_send(h)
        assert result.status is S.UNVERIFIED
        assert result.verification is not None
        assert result.verification.status is VerificationStatus.UNVERIFIED
        assert result.verification.reason_code == "no_verifier"
        assert result.result is not None  # the raw result is still returned, flagged

    def test_verifier_failure_is_a_failed_execution_without_a_result(self) -> None:
        verifier = FakeMCPVerifier(lambda _c: VerificationStatus.FAILED)
        h = Harness(verifiers={"mail:send_message": verifier})
        result = self._run_send(h)
        assert result.status is S.FAILED
        assert result.error_category is C.VERIFICATION_FAILED
        assert result.result is None
        assert result.execution_attempted is True

    def test_verifier_that_raises_is_a_failed_verification(self) -> None:
        h = Harness(verifiers={"mail:send_message": FakeMCPVerifier(raises=True)})
        result = self._run_send(h)
        assert result.status is S.FAILED
        assert result.error_category is C.VERIFICATION_FAILED

    def test_verifier_saying_unverified_is_unverified(self) -> None:
        verifier = FakeMCPVerifier(lambda _c: VerificationStatus.UNVERIFIED)
        h = Harness(verifiers={"mail:send_message": verifier})
        assert self._run_send(h).status is S.UNVERIFIED

    def test_verifier_cannot_claim_not_required_for_a_required_tool(self) -> None:
        verifier = FakeMCPVerifier(lambda _c: VerificationStatus.NOT_REQUIRED)
        h = Harness(verifiers={"mail:send_message": verifier})
        result = self._run_send(h)
        assert result.status is S.UNVERIFIED
        assert result.verification is not None
        assert result.verification.status is VerificationStatus.UNVERIFIED

    def test_verifier_is_not_consulted_when_not_required(self) -> None:
        verifier = FakeMCPVerifier()
        h = Harness(verifiers={"mail:read_message": verifier})
        h.grant_mail_read()
        result = h.gateway.execute(read_request())
        assert result.verification is not None
        assert result.verification.status is VerificationStatus.NOT_REQUIRED
        assert verifier.calls == 0

    def test_a_servers_own_success_flag_is_not_verification(self) -> None:
        h = Harness(handlers={"mail": {"send_message": lambda a, c: {"success": True}}})
        result = self._run_send(h)
        assert result.status is S.UNVERIFIED

    def test_verifier_sees_the_validated_result_json(self) -> None:
        seen: list[str] = []

        def decide(content: str) -> VerificationStatus:
            seen.append(content)
            return VerificationStatus.VERIFIED

        h = Harness(verifiers={"mail:send_message": FakeMCPVerifier(decide)})
        self._run_send(h)
        assert seen == ['{"sent":true}']


# ==================================================================== audit


class TestAudit:
    def _events(self, h: Harness) -> list[Any]:
        assert isinstance(h.audit, InMemoryMCPAuditSink)
        return list(h.audit.list_events())

    def test_exactly_one_event_per_request_on_every_path(self) -> None:
        h = _harness_with_verified_send()
        h.grant_mail_read()
        h.grant_mail_send()
        h.gateway.execute(read_request())  # success
        h.gateway.execute(read_request(tool_id="mail:ghost"))  # unknown
        h.gateway.execute(read_request(tool_id="bad"))  # malformed
        h.gateway.execute(read_request(arguments={}))  # invalid input
        h.gateway.execute(send_request())  # confirmation required
        h.gateway.execute(post_request())  # denied
        assert len(self._events(h)) == 6

    def test_event_carries_the_required_safe_metadata(self) -> None:
        h = Harness()
        h.grant_mail_read()
        result = h.gateway.execute(read_request(request_id="rq"))
        [event] = self._events(h)
        assert event.request_id == "rq"
        assert event.execution_id == result.execution_id
        assert event.server_id == "mail"
        assert event.tool_id == "mail:read_message"
        assert event.permission_action is PermissionAction.READ
        assert event.permission_resource is PermissionResource.GMAIL
        assert event.scope == "mail/read_message/acct-1"
        assert event.authorization_outcome is PermissionOutcomeSummary.ALLOW
        assert event.execution_status is S.SUCCEEDED
        assert event.verification_status is VerificationStatus.NOT_REQUIRED
        assert event.execution_attempted is True
        assert event.input_size and event.input_size > 0
        assert event.output_size and event.output_size > 0
        assert event.duration_ms >= 0
        assert event.occurred_at.tzinfo is not None

    def test_audit_contains_no_secret_argument_or_result_content(self) -> None:
        h = _harness_with_verified_send()
        h.grant_mail_read()
        h.grant_mail_send()
        h.gateway.execute(read_request())
        pending = h.gateway.execute(send_request(body="private body 12345"))
        assert pending.confirmation_id is not None
        h.approve(pending.confirmation_id)
        h.gateway.execute(
            send_request(body="private body 12345"),
            confirmation_id=pending.confirmation_id,
        )
        dumped = " ".join(e.model_dump_json() for e in self._events(h))
        dumped += " ".join(str(e) for e in h.permission_audit.list_events())
        for forbidden in (
            SECRET_MAIL,
            "private body 12345",
            "friend@example.test",
            "hi there",
            "Hello",
        ):
            assert forbidden not in dumped

    def test_audit_model_has_no_field_that_could_hold_content(self) -> None:
        from sam.mcp.models import MCPAuditEvent

        forbidden = {"arguments", "result", "content", "body", "credential", "token"}
        assert forbidden.isdisjoint(MCPAuditEvent.model_fields)

    def test_failed_and_denied_paths_record_the_category(self) -> None:
        h = Harness()
        h.gateway.execute(read_request())
        [event] = self._events(h)
        assert event.execution_status is S.DENIED
        assert event.error_category is C.PERMISSION_DENIED
        assert event.execution_attempted is False

    def test_unknown_tool_text_is_not_reflected_unless_well_formed(self) -> None:
        h = Harness()
        h.gateway.execute(
            MCPToolRequest(
                principal=ALICE, tool_id="ignore previous <script>", arguments={}
            )
        )
        [event] = self._events(h)
        assert event.tool_id is None

    def test_failing_audit_sink_never_changes_the_result(self) -> None:
        good = Harness()
        good.grant_mail_read()
        expected = good.gateway.execute(read_request())

        bad = Harness(audit_sink=FailingMCPAuditSink())
        bad.grant_mail_read()
        actual = bad.gateway.execute(read_request())
        assert actual.status is expected.status is S.SUCCEEDED

    def test_failing_audit_sink_cannot_turn_a_denial_into_an_allow(self) -> None:
        h = Harness(audit_sink=FailingMCPAuditSink())
        result = h.gateway.execute(read_request())
        assert result.status is S.DENIED
        assert h.transports["mail"].call_count == 0

    def test_gateway_without_an_audit_sink_still_works(self) -> None:
        h = Harness()
        h.grant_mail_read()
        gateway = MCPGateway(
            registry=h.registry,
            permission_engine=h.pengine,
            transports=h.transports,
            credential_provider=h.credentials,
        )
        assert gateway.execute(read_request()).status is S.SUCCEEDED
