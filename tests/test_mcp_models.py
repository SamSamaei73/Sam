"""Tests for sam.mcp.models: identity, schema subset, trusted descriptors,
results. Everything is pure data - no gateway involved."""

from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import ValidationError

from sam.mcp.models import (
    MAX_MCP_OUTPUT_SIZE,
    MAX_MCP_TIMEOUT_SECONDS,
    CredentialReference,
    MCPErrorCategory,
    MCPExecutionResult,
    MCPExecutionStatus,
    MCPToolId,
    MCPToolPermissionBinding,
    MCPToolRequest,
    MCPToolResult,
    MCPToolSchema,
    MCPVerificationResult,
    PermissionOutcomeSummary,
    VerificationStatus,
    sanitize_display_text,
)
from sam.permissions.models import PermissionAction, PermissionResource
from tests.mcp_support import ALICE, NOW, read_message_tool, send_message_tool

# ---------------------------------------------------------------- identity


class TestToolId:
    def test_parse_and_str_roundtrip(self) -> None:
        tid = MCPToolId.parse("gmail:send_message")
        assert (tid.server_id, tid.tool_name) == ("gmail", "send_message")
        assert str(tid) == "gmail:send_message"

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "nocolon",
            "a:b:c",
            ":tool",
            "server:",
            "Server:tool",  # uppercase server id
            "srv:too l",
            "srv:tool\n",
            "srv:../tool",
            "srv/x:tool",
            "1srv:tool",
            "srv:1tool",
            "srv:tool:extra",
            "srv:" + "t" * 65,
            "s" * 49 + ":tool",
            "srv:tоol",  # Cyrillic 'o' look-alike
        ],
    )
    def test_malformed_ids_rejected(self, text: str) -> None:
        with pytest.raises((ValueError, ValidationError)):
            MCPToolId.parse(text)

    def test_same_tool_name_on_two_servers_is_two_identities(self) -> None:
        a = MCPToolId.parse("alpha:read")
        b = MCPToolId.parse("beta:read")
        assert a != b
        assert len({a, b}) == 2

    def test_non_string_rejected(self) -> None:
        with pytest.raises(ValueError):
            MCPToolId.parse(123)  # type: ignore[arg-type]

    def test_oversized_id_rejected_before_splitting(self) -> None:
        with pytest.raises(ValueError):
            MCPToolId.parse("a:" + "b" * 10_000)


# ------------------------------------------------------------------ schema


class TestSchema:
    def test_from_untrusted_accepts_the_strict_subset(self) -> None:
        schema = MCPToolSchema.from_untrusted(
            {
                "type": "object",
                "properties": {
                    "a": {"type": "string", "maxLength": 5, "description": "ignored"},
                    "n": {"type": "integer", "minimum": 0, "maximum": 3},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 3,
                    },
                },
                "required": ["a"],
                "additionalProperties": False,
                "title": "ignored",
            }
        )
        assert schema.required == ("a",)
        assert schema.field("a") is not None
        assert schema.field("missing") is None

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            [],
            "object",
            {"type": "array"},
            {"type": "object", "additionalProperties": True},
            {"type": "object", "$ref": "#/x"},
            {
                "type": "object",
                "properties": {"a": {"type": "string", "pattern": ".*"}},
            },
            {"type": "object", "properties": {"a": {"type": "weird"}}},
            {"type": "object", "properties": {"a": {"type": "array"}}},
            {"type": "object", "properties": {"a": "string"}},
            {"type": "object", "properties": {"bad name": {"type": "string"}}},
            {"type": "object", "required": ["ghost"]},
            {"type": "object", "properties": [1]},
            {"type": "object", "required": "a"},
            {"type": "object", "properties": {"a": {"type": "string", "enum": "ab"}}},
            {
                "type": "object",
                "properties": {"a": {"type": "string", "minLength": 5, "maxLength": 1}},
            },
            {
                "type": "object",
                "properties": {"a": {"type": "integer", "minimum": math.inf}},
            },
        ],
    )
    def test_from_untrusted_rejects_anything_outside_the_subset(self, raw: Any) -> None:
        with pytest.raises((ValueError, ValidationError)):
            MCPToolSchema.from_untrusted(raw)

    def test_excessive_schema_nesting_rejected(self) -> None:
        node: dict[str, Any] = {"type": "string"}
        for _ in range(8):
            node = {"type": "object", "properties": {"x": node}}
        with pytest.raises((ValueError, ValidationError)):
            MCPToolSchema.from_untrusted(node)

    def test_schema_equality_is_structural(self) -> None:
        raw = {"type": "object", "properties": {"a": {"type": "string"}}}
        assert MCPToolSchema.from_untrusted(raw) == MCPToolSchema.from_untrusted(
            dict(raw)
        )

    def test_schema_is_immutable(self) -> None:
        schema = read_message_tool().input_schema
        with pytest.raises(ValidationError):
            schema.required = ()


# -------------------------------------------------------------- descriptors


class TestBindingAndDescriptor:
    @pytest.mark.parametrize(
        "resource",
        [
            PermissionResource.FILESYSTEM,
            PermissionResource.TERMINAL,
            PermissionResource.COMPUTER,
            PermissionResource.CODE,
            PermissionResource.KNOWLEDGE,
        ],
    )
    def test_native_local_resources_are_not_bindable(
        self, resource: PermissionResource
    ) -> None:
        with pytest.raises(ValidationError):
            MCPToolPermissionBinding(resource=resource, action=PermissionAction.READ)

    def test_unclassified_resource_action_pair_rejected(self) -> None:
        # GMAIL/PUBLISH is not a row in the permissions policy table.
        with pytest.raises(ValidationError):
            MCPToolPermissionBinding(
                resource=PermissionResource.GMAIL, action=PermissionAction.PUBLISH
            )

    def test_unknown_resource_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MCPToolPermissionBinding(resource="everything", action="read")  # type: ignore[arg-type]

    def test_duplicate_or_malformed_scope_arguments_rejected(self) -> None:
        for bad in (("a", "a"), ("bad name",), ("../x",)):
            with pytest.raises(ValidationError):
                MCPToolPermissionBinding(
                    resource=PermissionResource.GMAIL,
                    action=PermissionAction.READ,
                    scope_arguments=bad,
                )

    def test_too_many_scope_arguments_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MCPToolPermissionBinding(
                resource=PermissionResource.GMAIL,
                action=PermissionAction.READ,
                scope_arguments=("a", "b", "c", "d", "e"),
            )

    def test_credential_for_a_different_server_rejected(self) -> None:
        with pytest.raises(ValidationError):
            read_message_tool(
                credential=CredentialReference(server_id="social", credential_id="x")
            )

    def test_scope_argument_must_be_required_string_or_integer(self) -> None:
        with pytest.raises(ValidationError):
            read_message_tool(
                binding=MCPToolPermissionBinding(
                    resource=PermissionResource.GMAIL,
                    action=PermissionAction.READ,
                    scope_arguments=("ghost",),
                )
            )
        with pytest.raises(ValidationError):
            read_message_tool(
                input_schema=MCPToolSchema.from_untrusted(
                    {
                        "type": "object",
                        "properties": {"account": {"type": "string"}},
                        "required": [],
                    }
                )
            )

    @pytest.mark.parametrize(
        "timeout", [0, -1, math.nan, math.inf, MAX_MCP_TIMEOUT_SECONDS + 0.1]
    )
    def test_timeout_must_be_finite_positive_and_bounded(self, timeout: float) -> None:
        with pytest.raises(ValidationError):
            read_message_tool(timeout_seconds=timeout)

    def test_size_limits_are_bounded(self) -> None:
        with pytest.raises(ValidationError):
            read_message_tool(max_output_bytes=MAX_MCP_OUTPUT_SIZE + 1)
        with pytest.raises(ValidationError):
            read_message_tool(max_input_bytes=0)

    def test_description_is_sanitized_display_text_only(self) -> None:
        tool = read_message_tool(description="  hi\x00 there\x07  ")
        assert tool.description == "hi there"

    def test_descriptor_is_immutable(self) -> None:
        tool = read_message_tool()
        with pytest.raises(ValidationError):
            tool.enabled = False

    def test_descriptor_tool_id_is_canonical(self) -> None:
        assert str(send_message_tool().tool_id) == "mail:send_message"

    def test_sanitize_display_text(self) -> None:
        assert sanitize_display_text(None, max_length=5) is None
        assert sanitize_display_text(" \x00 ", max_length=5) is None
        assert sanitize_display_text("abcdefgh", max_length=3) == "abc"


# ----------------------------------------------------------------- requests


class TestRequest:
    def test_request_has_no_credential_permission_or_confirmation_field(self) -> None:
        assert set(MCPToolRequest.model_fields) == {
            "principal",
            "tool_id",
            "arguments",
            "request_id",
            "reason",
        }

    @pytest.mark.parametrize("request_id", ["", "-x", "a b", "a/b", "x" * 101, "ä"])
    def test_request_id_pattern(self, request_id: str) -> None:
        with pytest.raises(ValidationError):
            MCPToolRequest(principal=ALICE, tool_id="a:b", request_id=request_id)

    def test_request_id_defaults_to_unique_value(self) -> None:
        a = MCPToolRequest(principal=ALICE, tool_id="a:b")
        b = MCPToolRequest(principal=ALICE, tool_id="a:b")
        assert a.request_id != b.request_id

    def test_reason_is_sanitized(self) -> None:
        req = MCPToolRequest(principal=ALICE, tool_id="a:b", reason="  why\x00  ")
        assert req.reason == "why"

    def test_oversized_tool_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MCPToolRequest(principal=ALICE, tool_id="x" * 500)


# ------------------------------------------------------------------ results


def _result(**over: Any) -> MCPToolResult:
    fields: dict[str, Any] = dict(
        server_id="mail",
        tool_id="mail:read_message",
        execution_id="e1",
        content_json='{"a":1}',
        size_bytes=7,
    )
    fields.update(over)
    return MCPToolResult(**fields)


class TestResults:
    def test_result_is_always_untrusted_external_content(self) -> None:
        assert _result().untrusted_external_content is True
        with pytest.raises(ValidationError):
            _result(untrusted_external_content=False)

    def test_content_property_returns_a_fresh_copy(self) -> None:
        result = _result()
        first = result.content
        first["a"] = 99
        assert result.content == {"a": 1}

    def _exec(self, **over: Any) -> MCPExecutionResult:
        fields: dict[str, Any] = dict(
            request_id="r",
            execution_id="e",
            principal=ALICE,
            status=MCPExecutionStatus.FAILED,
            error_category=MCPErrorCategory.TRANSPORT_ERROR,
            created_at=NOW,
        )
        fields.update(over)
        return MCPExecutionResult(**fields)

    def test_success_requires_a_result_and_verified_or_not_required(self) -> None:
        ok = self._exec(
            status=MCPExecutionStatus.SUCCEEDED,
            error_category=None,
            result=_result(),
            verification=MCPVerificationResult(status=VerificationStatus.NOT_REQUIRED),
        )
        assert ok.result is not None
        with pytest.raises(ValidationError):
            self._exec(
                status=MCPExecutionStatus.SUCCEEDED, error_category=None, result=None
            )
        with pytest.raises(ValidationError):
            self._exec(
                status=MCPExecutionStatus.SUCCEEDED,
                error_category=None,
                result=_result(),
                verification=MCPVerificationResult(
                    status=VerificationStatus.UNVERIFIED
                ),
            )

    def test_unverified_carries_the_result_but_is_not_success(self) -> None:
        r = self._exec(
            status=MCPExecutionStatus.UNVERIFIED,
            error_category=None,
            result=_result(),
            verification=MCPVerificationResult(status=VerificationStatus.UNVERIFIED),
        )
        assert r.status is MCPExecutionStatus.UNVERIFIED

    def test_failed_result_cannot_carry_a_result(self) -> None:
        with pytest.raises(ValidationError):
            self._exec(result=_result())

    def test_failure_requires_a_category(self) -> None:
        with pytest.raises(ValidationError):
            self._exec(error_category=None)

    def test_confirmation_required_needs_no_category(self) -> None:
        r = self._exec(
            status=MCPExecutionStatus.CONFIRMATION_REQUIRED,
            error_category=None,
            permission_outcome=PermissionOutcomeSummary.CONFIRM_REQUIRED,
        )
        assert r.result is None

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._exec(created_at=NOW.replace(tzinfo=None))
