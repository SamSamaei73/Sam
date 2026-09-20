"""Pure input and output validation. No I/O, no transport, no permission.

Inputs (tool arguments) and outputs (tool results) are both untrusted JSON.
Both are walked with explicit depth, item-count, string-length and total
size bounds *before* being serialized or interpreted, so a hostile value
cannot exhaust memory or the stack. Non-JSON values (bytes, sets, objects,
tuples, NaN/Infinity) are rejected outright — nothing is ever deserialized
into an arbitrary Python object.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from typing import Any

from sam.mcp.errors import MCPError, MCPInputValidationError, MCPOutputValidationError
from sam.mcp.models import (
    MAX_MCP_ARGUMENT_DEPTH,
    MAX_MCP_ARGUMENT_ITEMS,
    MAX_MCP_INPUT_SIZE,
    MAX_MCP_OUTPUT_SIZE,
    MAX_MCP_RESULT_DEPTH,
    MAX_MCP_STRING_LENGTH,
    MCPErrorCategory,
    MCPSchemaProperty,
    MCPSchemaType,
    MCPToolDescriptor,
    canonical_json,
)
from sam.memory.sanitization import looks_like_secret

_ErrorFactory = Callable[..., MCPError]


class _Budget:
    """Running size estimate so a walk can stop early instead of first
    materializing an enormous serialization."""

    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def spend(
        self, amount: int, *, too_large: MCPErrorCategory, error: _ErrorFactory
    ) -> None:
        self.remaining -= amount
        if self.remaining < 0:
            raise error("value is too large", category=too_large)


def _walk_json(
    value: Any,
    *,
    depth: int,
    max_depth: int,
    max_items: int | None,
    max_string: int | None,
    budget: _Budget,
    error: _ErrorFactory,
    invalid: MCPErrorCategory,
    too_large: MCPErrorCategory,
    check_secrets: bool,
) -> None:
    """Reject anything that is not bounded, JSON-compatible data."""

    if depth > max_depth:
        raise error("value is nested too deeply", category=invalid)
    if value is None or isinstance(value, bool):
        budget.spend(5, too_large=too_large, error=error)
        return
    if isinstance(value, int):
        if abs(value) >= 10**30:
            raise error("integer is out of range", category=invalid)
        budget.spend(len(str(value)), too_large=too_large, error=error)
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise error("number is not finite", category=invalid)
        budget.spend(24, too_large=too_large, error=error)
        return
    if isinstance(value, str):
        if max_string is not None and len(value) > max_string:
            raise error("string is too long", category=too_large)
        budget.spend(len(value) + 2, too_large=too_large, error=error)
        if check_secrets and looks_like_secret(value):
            raise error(
                "value looks like a secret", category=MCPErrorCategory.SECRET_IN_INPUT
            )
        return
    if isinstance(value, list):
        if max_items is not None and len(value) > max_items:
            raise error("collection is too large", category=too_large)
        budget.spend(2, too_large=too_large, error=error)
        for item in value:
            _walk_json(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
                budget=budget,
                error=error,
                invalid=invalid,
                too_large=too_large,
                check_secrets=check_secrets,
            )
        return
    if isinstance(value, dict):
        if max_items is not None and len(value) > max_items:
            raise error("collection is too large", category=too_large)
        budget.spend(2, too_large=too_large, error=error)
        for key, item in value.items():
            if not isinstance(key, str):
                raise error("object keys must be strings", category=invalid)
            _walk_json(
                key,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
                budget=budget,
                error=error,
                invalid=invalid,
                too_large=too_large,
                check_secrets=check_secrets,
            )
            _walk_json(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
                budget=budget,
                error=error,
                invalid=invalid,
                too_large=too_large,
                check_secrets=check_secrets,
            )
        return
    raise error("value is not JSON-compatible", category=invalid)


# --------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------- #


def validate_arguments(
    arguments: object, descriptor: MCPToolDescriptor
) -> dict[str, Any]:
    """Validate untrusted tool arguments against Sam's trusted schema.

    Returns a deep copy (via canonical JSON) so later mutation of the
    caller's object can never change what is authorized, digested, or
    executed. Raises ``MCPInputValidationError`` (fail closed).
    """

    if not isinstance(arguments, dict):
        raise MCPInputValidationError("arguments must be an object")
    limit = min(descriptor.max_input_bytes, MAX_MCP_INPUT_SIZE)
    _walk_json(
        arguments,
        depth=1,
        max_depth=MAX_MCP_ARGUMENT_DEPTH,
        max_items=MAX_MCP_ARGUMENT_ITEMS,
        max_string=MAX_MCP_STRING_LENGTH,
        budget=_Budget(limit),
        error=MCPInputValidationError,
        invalid=MCPErrorCategory.INPUT_INVALID,
        too_large=MCPErrorCategory.INPUT_TOO_LARGE,
        check_secrets=True,
    )
    try:
        text = canonical_json(arguments)
    except (TypeError, ValueError):
        raise MCPInputValidationError("arguments are not JSON-compatible") from None
    if len(text.encode("utf-8")) > limit:
        raise MCPInputValidationError(
            "arguments are too large", category=MCPErrorCategory.INPUT_TOO_LARGE
        )
    copy: dict[str, Any] = json.loads(text)
    schema = descriptor.input_schema
    _validate_object(
        copy,
        {field.name: field.spec for field in schema.properties},
        set(schema.required),
        depth=1,
    )
    return copy


def _validate_object(
    value: Mapping[str, Any],
    fields: Mapping[str, MCPSchemaProperty],
    required: set[str],
    *,
    depth: int,
) -> None:
    unknown = set(value) - set(fields)
    if unknown:
        raise MCPInputValidationError("arguments contain unknown fields")
    missing = required - set(value)
    if missing:
        raise MCPInputValidationError("arguments are missing required fields")
    for name, item in value.items():
        _validate_value(item, fields[name], depth=depth + 1)


def _validate_value(value: Any, spec: MCPSchemaProperty, *, depth: int) -> None:
    kind = spec.type
    if kind is MCPSchemaType.STRING:
        if not isinstance(value, str):
            raise MCPInputValidationError("argument has the wrong type")
        if spec.min_length is not None and len(value) < spec.min_length:
            raise MCPInputValidationError("string argument is too short")
        limit = (
            spec.max_length if spec.max_length is not None else MAX_MCP_STRING_LENGTH
        )
        if len(value) > limit:
            raise MCPInputValidationError("string argument is too long")
    elif kind is MCPSchemaType.BOOLEAN:
        if not isinstance(value, bool):
            raise MCPInputValidationError("argument has the wrong type")
    elif kind in (MCPSchemaType.INTEGER, MCPSchemaType.NUMBER):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise MCPInputValidationError("argument has the wrong type")
        if kind is MCPSchemaType.INTEGER and not isinstance(value, int):
            raise MCPInputValidationError("argument has the wrong type")
        if spec.minimum is not None and value < spec.minimum:
            raise MCPInputValidationError("numeric argument is below its minimum")
        if spec.maximum is not None and value > spec.maximum:
            raise MCPInputValidationError("numeric argument is above its maximum")
    elif kind is MCPSchemaType.ARRAY:
        if not isinstance(value, list):
            raise MCPInputValidationError("argument has the wrong type")
        limit_items = (
            spec.max_items if spec.max_items is not None else MAX_MCP_ARGUMENT_ITEMS
        )
        if len(value) > limit_items:
            raise MCPInputValidationError("array argument is too long")
        assert spec.items is not None
        for element in value:
            _validate_value(element, spec.items, depth=depth + 1)
    elif kind is MCPSchemaType.OBJECT:
        if not isinstance(value, dict):
            raise MCPInputValidationError("argument has the wrong type")
        _validate_object(
            value,
            {field.name: field.spec for field in spec.properties},
            set(spec.required),
            depth=depth,
        )
    if spec.enum is not None and not any(
        type(value) is type(option) and value == option for option in spec.enum
    ):
        raise MCPInputValidationError("argument is not an allowed value")


# --------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------- #


def validate_output(raw: object, descriptor: MCPToolDescriptor) -> tuple[str, int]:
    """Validate an untrusted tool result and return ``(content_json, size)``.

    Policy: a result that is oversized, too deep, or not plain JSON is
    *rejected*, never truncated — a truncated JSON document is a different,
    possibly misleading document. Raises ``MCPOutputValidationError``.
    """

    limit = min(descriptor.max_output_bytes, MAX_MCP_OUTPUT_SIZE)
    _walk_json(
        raw,
        depth=1,
        max_depth=MAX_MCP_RESULT_DEPTH,
        max_items=None,
        max_string=None,
        budget=_Budget(limit),
        error=MCPOutputValidationError,
        invalid=MCPErrorCategory.OUTPUT_INVALID,
        too_large=MCPErrorCategory.OUTPUT_TOO_LARGE,
        check_secrets=False,
    )
    try:
        text = canonical_json(raw)
    except (TypeError, ValueError):
        raise MCPOutputValidationError("tool output is not JSON-compatible") from None
    size = len(text.encode("utf-8"))
    if size > limit:
        raise MCPOutputValidationError(
            "tool output is too large", category=MCPErrorCategory.OUTPUT_TOO_LARGE
        )
    return text, size


__all__ = ["validate_arguments", "validate_output"]
