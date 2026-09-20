"""Tests for sam.mcp.validation: hostile input and hostile output."""

from __future__ import annotations

import math
from typing import Any

import pytest

from sam.mcp.errors import MCPInputValidationError, MCPOutputValidationError
from sam.mcp.models import (
    MAX_MCP_ARGUMENT_DEPTH,
    MAX_MCP_ARGUMENT_ITEMS,
    MAX_MCP_RESULT_DEPTH,
    MAX_MCP_STRING_LENGTH,
    MCPErrorCategory,
    MCPToolSchema,
)
from sam.mcp.validation import validate_arguments, validate_output
from tests.mcp_support import list_events_tool, read_message_tool, send_message_tool

GOOD = {"account": "acct-1", "message_id": "m-1"}


def _tool_with(properties: dict[str, Any], required: list[str] | None = None):  # type: ignore[no-untyped-def]
    return read_message_tool(
        input_schema=MCPToolSchema.from_untrusted(
            {
                "type": "object",
                "properties": {
                    "account": {"type": "string", "maxLength": 64},
                    **properties,
                },
                "required": ["account", *(required or [])],
            }
        )
    )


class TestArguments:
    def test_valid_arguments_pass_and_are_deep_copied(self) -> None:
        source = dict(GOOD)
        result = validate_arguments(source, read_message_tool())
        assert result == GOOD
        source["account"] = "changed-after"
        assert result["account"] == "acct-1"

    def test_non_object_rejected(self) -> None:
        bad_values: tuple[Any, ...] = (None, [], "x", 3, ("a",), {1, 2})
        for bad in bad_values:
            with pytest.raises(MCPInputValidationError):
                validate_arguments(bad, read_message_tool())

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(MCPInputValidationError):
            validate_arguments({**GOOD, "cc": "x"}, read_message_tool())

    def test_missing_required_rejected(self) -> None:
        with pytest.raises(MCPInputValidationError):
            validate_arguments({"account": "a"}, read_message_tool())

    def test_wrong_types_rejected(self) -> None:
        for bad in (
            {"account": 5, "message_id": "m"},
            {"account": "a", "message_id": None},
        ):
            with pytest.raises(MCPInputValidationError):
                validate_arguments(bad, read_message_tool())

    def test_bool_is_not_an_integer(self) -> None:
        with pytest.raises(MCPInputValidationError):
            validate_arguments({"calendar_id": "c", "limit": True}, list_events_tool())

    def test_integer_bounds(self) -> None:
        tool = list_events_tool()
        assert validate_arguments({"calendar_id": "c", "limit": 1}, tool)
        assert validate_arguments({"calendar_id": "c", "limit": 50}, tool)
        for bad in (0, 51, -1, 2.5):
            with pytest.raises(MCPInputValidationError):
                validate_arguments({"calendar_id": "c", "limit": bad}, tool)

    def test_nan_and_infinity_rejected(self) -> None:
        tool = _tool_with({"n": {"type": "number"}})
        for bad in (math.nan, math.inf, -math.inf):
            with pytest.raises(MCPInputValidationError):
                validate_arguments({"account": "a", "n": bad}, tool)

    def test_string_length_bounds(self) -> None:
        with pytest.raises(MCPInputValidationError):
            validate_arguments({"account": "", "message_id": "m"}, read_message_tool())
        with pytest.raises(MCPInputValidationError):
            validate_arguments(
                {"account": "a" * 65, "message_id": "m"}, read_message_tool()
            )

    def test_global_string_cap_even_without_schema_max(self) -> None:
        tool = _tool_with({"t": {"type": "string"}})
        with pytest.raises(MCPInputValidationError) as info:
            validate_arguments(
                {"account": "a", "t": "x" * (MAX_MCP_STRING_LENGTH + 1)}, tool
            )
        assert info.value.category in (
            MCPErrorCategory.INPUT_TOO_LARGE,
            MCPErrorCategory.INPUT_INVALID,
        )

    def test_enum_is_exact_and_type_strict(self) -> None:
        tool = _tool_with({"mode": {"type": "string", "enum": ["a", "b"]}})
        assert validate_arguments({"account": "x", "mode": "a"}, tool)
        with pytest.raises(MCPInputValidationError):
            validate_arguments({"account": "x", "mode": "c"}, tool)
        int_tool = _tool_with({"n": {"type": "integer", "enum": [1, 2]}})
        with pytest.raises(MCPInputValidationError):
            validate_arguments({"account": "x", "n": True}, int_tool)

    def test_array_items_validated_and_bounded(self) -> None:
        tool = _tool_with(
            {"tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2}}
        )
        assert validate_arguments({"account": "a", "tags": ["x", "y"]}, tool)
        with pytest.raises(MCPInputValidationError):
            validate_arguments({"account": "a", "tags": ["x", "y", "z"]}, tool)
        with pytest.raises(MCPInputValidationError):
            validate_arguments({"account": "a", "tags": [1]}, tool)

    def test_nested_object_validated(self) -> None:
        tool = _tool_with(
            {
                "opts": {
                    "type": "object",
                    "properties": {"k": {"type": "string"}},
                    "required": ["k"],
                }
            }
        )
        assert validate_arguments({"account": "a", "opts": {"k": "v"}}, tool)
        for bad in ({}, {"k": 1}, {"k": "v", "extra": 1}):
            with pytest.raises(MCPInputValidationError):
                validate_arguments({"account": "a", "opts": bad}, tool)

    def test_deeply_nested_json_rejected_without_recursion_error(self) -> None:
        node: Any = "leaf"
        for _ in range(5000):
            node = [node]
        with pytest.raises(MCPInputValidationError):
            validate_arguments({"account": "a", "x": node}, read_message_tool())

    def test_depth_limit_boundary(self) -> None:
        tool = _tool_with(
            {
                "x": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "string"}},
                }
            }
        )
        node: Any = ["s"]
        assert validate_arguments({"account": "a", "x": [node]}, tool)
        deep: Any = "s"
        for _ in range(MAX_MCP_ARGUMENT_DEPTH + 1):
            deep = [deep]
        with pytest.raises(MCPInputValidationError):
            validate_arguments({"account": "a", "x": deep}, tool)

    def test_too_many_items_rejected(self) -> None:
        tool = _tool_with({"x": {"type": "array", "items": {"type": "integer"}}})
        with pytest.raises(MCPInputValidationError):
            validate_arguments(
                {"account": "a", "x": list(range(MAX_MCP_ARGUMENT_ITEMS + 1))}, tool
            )

    def test_too_many_keys_rejected(self) -> None:
        many = {f"k{i}": 1 for i in range(MAX_MCP_ARGUMENT_ITEMS + 1)}
        with pytest.raises(MCPInputValidationError):
            validate_arguments(many, read_message_tool())

    def test_total_size_limit_uses_the_tools_own_smaller_limit(self) -> None:
        tool = send_message_tool(max_input_bytes=200)
        big = {
            "account": "a",
            "to": "t",
            "subject": "s",
            "body": "b" * 500,
        }
        with pytest.raises(MCPInputValidationError) as info:
            validate_arguments(big, tool)
        assert info.value.category is MCPErrorCategory.INPUT_TOO_LARGE

    def test_non_json_values_rejected(self) -> None:
        tool = _tool_with({"x": {"type": "string"}})
        for bad in (b"bytes", {1, 2}, object(), ("t",), 1 + 2j):
            with pytest.raises(MCPInputValidationError):
                validate_arguments({"account": "a", "x": bad}, tool)

    def test_non_string_keys_rejected(self) -> None:
        with pytest.raises(MCPInputValidationError):
            validate_arguments({1: "x"}, read_message_tool())

    def test_huge_integer_rejected(self) -> None:
        tool = _tool_with({"n": {"type": "integer"}})
        with pytest.raises(MCPInputValidationError):
            validate_arguments({"account": "a", "n": 10**50}, tool)

    @pytest.mark.parametrize(
        "secret",
        [
            "sk-ant-abcdefghijklmnopqrst",
            "-----BEGIN RSA PRIVATE KEY-----",
            "password is hunter2plus",
            "ghp_" + "a" * 24,
        ],
    )
    def test_secret_looking_argument_rejected(self, secret: str) -> None:
        with pytest.raises(MCPInputValidationError) as info:
            validate_arguments(
                {"account": "a", "message_id": secret}, read_message_tool()
            )
        assert info.value.category is MCPErrorCategory.SECRET_IN_INPUT
        assert secret not in str(info.value)

    def test_secret_looking_key_rejected(self) -> None:
        with pytest.raises(MCPInputValidationError):
            validate_arguments({"sk-ant-abcdefghijklmnopqrst": 1}, read_message_tool())

    def test_error_messages_never_echo_values(self) -> None:
        with pytest.raises(MCPInputValidationError) as info:
            validate_arguments(
                {"account": "TOPSECRETVALUE", "zzz": 1}, read_message_tool()
            )
        assert "TOPSECRETVALUE" not in str(info.value)


class TestOutput:
    def test_valid_output_returns_canonical_json_and_size(self) -> None:
        text, size = validate_output(
            {"b": 1, "a": [1, "x", None, True]}, read_message_tool()
        )
        assert text == '{"a":[1,"x",null,true],"b":1}'
        assert size == len(text)

    def test_scalar_and_list_outputs_are_allowed(self) -> None:
        for value in (1, "x", None, [1, 2], True):
            assert validate_output(value, read_message_tool())

    def test_oversized_output_rejected_not_truncated(self) -> None:
        tool = read_message_tool(max_output_bytes=100)
        with pytest.raises(MCPOutputValidationError) as info:
            validate_output({"data": "x" * 1000}, tool)
        assert info.value.category is MCPErrorCategory.OUTPUT_TOO_LARGE

    def test_deep_output_rejected(self) -> None:
        node: Any = 1
        for _ in range(MAX_MCP_RESULT_DEPTH + 5):
            node = {"a": node}
        with pytest.raises(MCPOutputValidationError):
            validate_output(node, read_message_tool())

    def test_pathologically_deep_output_does_not_overflow_the_stack(self) -> None:
        node: Any = 1
        for _ in range(20000):
            node = [node]
        with pytest.raises(MCPOutputValidationError):
            validate_output(node, read_message_tool())

    def test_non_json_output_rejected(self) -> None:
        for bad in (b"x", {1, 2}, object(), (1,), math.nan, {1: "x"}):
            with pytest.raises(MCPOutputValidationError):
                validate_output(bad, read_message_tool())

    def test_unicode_output_is_escaped_deterministically(self) -> None:
        text, _ = validate_output({"k": "é‮"}, read_message_tool())
        assert text.isascii()
