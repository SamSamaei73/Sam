"""Typed, immutable domain models for the MCP gateway (Phase 8).

Trust model, in one paragraph: everything a *server* says about a tool
(its name, description, schema, claimed risk) is untrusted capability
metadata. Sam's security metadata — which permission a tool maps to, its
scope, its timeout, its size limits, its credential reference, whether it
needs verification — lives only in ``MCPToolDescriptor``, which is
constructed by Sam's own code and held by ``sam.mcp.registry``. Nothing in
this module lets a server, a tool description, a tool result, or an LLM
change any of it.

Every security-critical limit is a module constant here, in one place,
following the convention of the earlier phases (see
``sam.knowledge.models``).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sam.permissions.models import (
    PermissionAction,
    PermissionResource,
    Principal,
    RiskLevel,
)
from sam.permissions.policy import classify

# --------------------------------------------------------------------- #
# Limits — one place, explicit, never scattered
# --------------------------------------------------------------------- #

MAX_MCP_INPUT_SIZE = 64 * 1024  # serialized argument bytes
MAX_MCP_OUTPUT_SIZE = 256 * 1024  # serialized result bytes
MAX_MCP_ARGUMENT_DEPTH = 8
MAX_MCP_ARGUMENT_ITEMS = 100  # per object/array
MAX_MCP_STRING_LENGTH = 10_000
MAX_MCP_RESULT_DEPTH = 16
MAX_MCP_TOOL_NAME_LENGTH = 64
MAX_MCP_SERVER_ID_LENGTH = 48
MAX_MCP_TIMEOUT_SECONDS = 120.0
MAX_MCP_DESCRIPTION_LENGTH = 1_000
MAX_MCP_REASON_LENGTH = 500
MAX_MCP_SCOPE_SEGMENT_LENGTH = 128
MAX_MCP_SCOPE_ARGUMENTS = 4
MAX_MCP_REQUEST_ID_LENGTH = 100
MAX_MCP_TOOL_ID_LENGTH = MAX_MCP_SERVER_ID_LENGTH + 1 + MAX_MCP_TOOL_NAME_LENGTH
MAX_MCP_TOOLS_PER_SERVER = 256
MAX_MCP_SERVERS = 64
MAX_MCP_SCHEMA_DEPTH = 4
MAX_MCP_ENUM_VALUES = 100
MAX_TRACKED_REQUEST_IDS = 10_000
MAX_CREDENTIAL_LENGTH = 4_096

# Identifiers are ASCII-only and colon/slash-free so that the canonical
# tool id "server:tool" is unambiguous and cannot be confused by Unicode
# look-alikes, case tricks, or path separators.
_SERVER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SCOPE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@=-]*$")
_CREDENTIAL_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Resources an MCP tool may be bound to. Local, natively-managed domains
# (filesystem, terminal, computer control, coding, knowledge) are excluded
# on purpose: those have their own dedicated managers and grant scopes, and
# an external MCP server must never be able to act as a back door into a
# grant issued for one of them. Every tool scope is additionally prefixed
# with its server id and tool name (see ``sam.mcp.policy``), so a grant
# issued for a native tool can never match an MCP request either.
MCP_BINDABLE_RESOURCES: frozenset[PermissionResource] = frozenset(
    {
        PermissionResource.MCP,
        PermissionResource.GMAIL,
        PermissionResource.CALENDAR,
        PermissionResource.SOCIAL_MEDIA,
        PermissionResource.GIT,
        PermissionResource.BROWSER,
        PermissionResource.DATABASE,
        PermissionResource.FINANCIAL_SERVICE,
    }
)


def new_id() -> str:
    return uuid4().hex


def utc_now() -> datetime:
    return datetime.now(UTC)


def sanitize_display_text(value: str | None, *, max_length: int) -> str | None:
    """Strip control characters and bound length for untrusted display text
    (tool descriptions, reasons). Never used for any authorization or
    execution decision."""

    if value is None:
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()
    if not cleaned:
        return None
    return cleaned[:max_length]


def canonical_json(value: Any) -> str:
    """Deterministic JSON text; rejects NaN/Infinity and non-JSON types."""

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


# --------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------- #


class MCPErrorCategory(StrEnum):
    """Closed set of failure categories — never a raw exception message."""

    INVALID_TOOL_ID = "invalid_tool_id"
    UNKNOWN_TOOL = "unknown_tool"
    TOOL_DISABLED = "tool_disabled"
    REGISTRATION_ERROR = "registration_error"
    INPUT_INVALID = "input_invalid"
    INPUT_TOO_LARGE = "input_too_large"
    SECRET_IN_INPUT = "secret_in_input"
    POLICY_ERROR = "policy_error"
    PERMISSION_DENIED = "permission_denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_INVALID = "confirmation_invalid"
    CREDENTIAL_ERROR = "credential_error"
    DUPLICATE_REQUEST = "duplicate_request"
    TRANSPORT_ERROR = "transport_error"
    TIMEOUT = "timeout"
    OUTPUT_INVALID = "output_invalid"
    OUTPUT_TOO_LARGE = "output_too_large"
    CREDENTIAL_LEAK = "credential_leak"
    VERIFICATION_FAILED = "verification_failed"
    INTERNAL_ERROR = "internal_error"


class MCPExecutionStatus(StrEnum):
    """Final disposition of one gateway request.

    SUCCEEDED: executed, and either verified or verification not required.
    UNVERIFIED: executed, but a required verification could not confirm the
        side effect — never reported as success.
    REJECTED: stopped before the PermissionEngine (unknown/disabled tool,
        invalid input, duplicate request, policy error).
    DENIED: the PermissionEngine denied the request.
    CONFIRMATION_REQUIRED: the PermissionEngine requires a confirmation
        that has not yet been approved; nothing executed.
    FAILED: allowed but did not complete (credential, transport, timeout,
        output, verification, or internal failure).
    """

    SUCCEEDED = "succeeded"
    UNVERIFIED = "unverified"
    REJECTED = "rejected"
    DENIED = "denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    FAILED = "failed"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"
    NOT_REQUIRED = "not_required"


class PermissionOutcomeSummary(StrEnum):
    """A minimal echo of ``sam.permissions.models.DecisionOutcome`` — its
    own type for the same reason every prior phase keeps one."""

    ALLOW = "allow"
    CONFIRM_REQUIRED = "confirm_required"
    DENY = "deny"


# --------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------- #

MCPServerId = Annotated[
    str,
    Field(
        min_length=1, max_length=MAX_MCP_SERVER_ID_LENGTH, pattern=_SERVER_ID_RE.pattern
    ),
]
MCPToolName = Annotated[
    str,
    Field(
        min_length=1, max_length=MAX_MCP_TOOL_NAME_LENGTH, pattern=_TOOL_NAME_RE.pattern
    ),
]


class MCPToolId(BaseModel):
    """Canonical tool identity: ``server_id`` + ``tool_name``.

    Never an unqualified name, so two servers exposing the same tool name
    can never be confused. The text form is ``"server:tool"``; neither part
    can contain a colon.
    """

    model_config = ConfigDict(frozen=True)

    server_id: MCPServerId
    tool_name: MCPToolName

    @classmethod
    def parse(cls, text: str) -> Self:
        """Parse ``"server:tool"``. Raises ``ValueError`` for anything else."""

        if not isinstance(text, str) or len(text) > MAX_MCP_TOOL_ID_LENGTH:
            raise ValueError("malformed tool id")
        parts = text.split(":")
        if len(parts) != 2:
            raise ValueError("malformed tool id")
        return cls(server_id=parts[0], tool_name=parts[1])

    def __str__(self) -> str:
        return f"{self.server_id}:{self.tool_name}"


class CredentialReference(BaseModel):
    """A *reference* to a credential — never the credential itself.

    Bound to one server: a provider must refuse to resolve it for any other
    server (see ``sam.mcp.credentials``). It is held by the trusted
    registry; the LLM and the request never supply it.
    """

    model_config = ConfigDict(frozen=True)

    server_id: MCPServerId
    credential_id: str = Field(
        min_length=1, max_length=64, pattern=_CREDENTIAL_ID_RE.pattern
    )


# --------------------------------------------------------------------- #
# Input schema — a deliberately small, strict subset of JSON Schema
# --------------------------------------------------------------------- #


class MCPSchemaType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


_EnumValue = str | int | float | bool


class MCPSchemaProperty(BaseModel):
    """One constrained value. Unknown fields are always rejected at
    validation time (``additionalProperties`` is fixed to false)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: MCPSchemaType
    enum: tuple[_EnumValue, ...] | None = Field(
        default=None, max_length=MAX_MCP_ENUM_VALUES
    )
    min_length: int | None = Field(default=None, ge=0, le=MAX_MCP_STRING_LENGTH)
    max_length: int | None = Field(default=None, ge=0, le=MAX_MCP_STRING_LENGTH)
    minimum: float | None = None
    maximum: float | None = None
    items: MCPSchemaProperty | None = None
    max_items: int | None = Field(default=None, ge=0, le=MAX_MCP_ARGUMENT_ITEMS)
    properties: tuple[MCPSchemaField, ...] = Field(
        default=(), max_length=MAX_MCP_ARGUMENT_ITEMS
    )
    required: tuple[str, ...] = Field(default=(), max_length=MAX_MCP_ARGUMENT_ITEMS)

    @field_validator("minimum", "maximum")
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("numeric bounds must be finite")
        return value

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.type is MCPSchemaType.ARRAY and self.items is None:
            raise ValueError("an array schema requires items")
        if self.type is not MCPSchemaType.ARRAY and (
            self.items is not None or self.max_items is not None
        ):
            raise ValueError("items/max_items only apply to arrays")
        if self.type is not MCPSchemaType.OBJECT and (self.properties or self.required):
            raise ValueError("properties/required only apply to objects")
        if self.type is not MCPSchemaType.STRING and (
            self.min_length is not None or self.max_length is not None
        ):
            raise ValueError("min_length/max_length only apply to strings")
        numeric = self.type in (MCPSchemaType.INTEGER, MCPSchemaType.NUMBER)
        if not numeric and (self.minimum is not None or self.maximum is not None):
            raise ValueError("minimum/maximum only apply to numbers")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length must not exceed max_length")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("minimum must not exceed maximum")
        names = [field.name for field in self.properties]
        if len(set(names)) != len(names):
            raise ValueError("duplicate property names")
        if not set(self.required) <= set(names):
            raise ValueError("required names must be declared properties")
        return self

    def depth(self) -> int:
        child = 0
        if self.items is not None:
            child = max(child, self.items.depth())
        for field in self.properties:
            child = max(child, field.spec.depth())
        return 1 + child


class MCPSchemaField(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=_FIELD_NAME_RE.pattern)
    spec: MCPSchemaProperty


MCPSchemaProperty.model_rebuild()


class MCPToolSchema(BaseModel):
    """The argument object a tool accepts — always an object, never
    accepting unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    properties: tuple[MCPSchemaField, ...] = Field(
        default=(), max_length=MAX_MCP_ARGUMENT_ITEMS
    )
    required: tuple[str, ...] = Field(default=(), max_length=MAX_MCP_ARGUMENT_ITEMS)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        names = [field.name for field in self.properties]
        if len(set(names)) != len(names):
            raise ValueError("duplicate property names")
        if not set(self.required) <= set(names):
            raise ValueError("required names must be declared properties")
        if any(field.spec.depth() > MAX_MCP_SCHEMA_DEPTH for field in self.properties):
            raise ValueError("schema is nested too deeply")
        return self

    def field(self, name: str) -> MCPSchemaProperty | None:
        for candidate in self.properties:
            if candidate.name == name:
                return candidate.spec
        return None

    @classmethod
    def from_untrusted(cls, raw: object) -> Self:
        """Parse a JSON-Schema-shaped mapping (typically advertised by an
        MCP server) into the strict subset. Raises ``ValueError`` for
        anything outside the subset — an unsupported keyword is treated as
        an incompatible schema, never silently ignored (only the purely
        descriptive ``description``/``title`` keys are dropped)."""

        if not isinstance(raw, Mapping):
            raise ValueError("schema must be an object")
        allowed = {"type", "properties", "required", "additionalProperties"}
        allowed |= {"description", "title"}
        if set(raw) - allowed:
            raise ValueError("unsupported schema keyword")
        if raw.get("type") != "object":
            raise ValueError("top-level schema type must be object")
        if raw.get("additionalProperties", False) is not False:
            raise ValueError("additionalProperties must be false")
        fields, required = _parse_object_members(raw, depth=1)
        return cls(properties=fields, required=required)


def _parse_object_members(
    raw: Mapping[str, Any], *, depth: int
) -> tuple[tuple[MCPSchemaField, ...], tuple[str, ...]]:
    if depth > MAX_MCP_SCHEMA_DEPTH:
        raise ValueError("schema is nested too deeply")
    props = raw.get("properties", {})
    if not isinstance(props, Mapping) or len(props) > MAX_MCP_ARGUMENT_ITEMS:
        raise ValueError("invalid properties")
    fields: list[MCPSchemaField] = []
    for name in sorted(props):
        if not isinstance(name, str):
            raise ValueError("property names must be strings")
        fields.append(
            MCPSchemaField(name=name, spec=_parse_property(props[name], depth + 1))
        )
    required = raw.get("required", [])
    if not isinstance(required, list | tuple) or not all(
        isinstance(item, str) for item in required
    ):
        raise ValueError("invalid required list")
    return tuple(fields), tuple(sorted(set(required)))


def _parse_property(raw: object, depth: int) -> MCPSchemaProperty:
    if not isinstance(raw, Mapping):
        raise ValueError("property schema must be an object")
    allowed = {
        "type",
        "enum",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "items",
        "maxItems",
        "properties",
        "required",
        "additionalProperties",
        "description",
        "title",
    }
    if set(raw) - allowed:
        raise ValueError("unsupported schema keyword")
    if raw.get("additionalProperties", False) is not False:
        raise ValueError("additionalProperties must be false")
    try:
        kind = MCPSchemaType(str(raw.get("type")))
    except ValueError:
        raise ValueError("unsupported property type") from None
    enum = raw.get("enum")
    if enum is not None and not isinstance(enum, list | tuple):
        raise ValueError("enum must be a list")
    fields: tuple[MCPSchemaField, ...] = ()
    required: tuple[str, ...] = ()
    if kind is MCPSchemaType.OBJECT:
        fields, required = _parse_object_members(raw, depth=depth)
    items = None
    if kind is MCPSchemaType.ARRAY:
        if depth + 1 > MAX_MCP_SCHEMA_DEPTH:
            raise ValueError("schema is nested too deeply")
        items = _parse_property(raw.get("items"), depth + 1)
    return MCPSchemaProperty(
        type=kind,
        enum=tuple(enum) if enum is not None else None,
        min_length=raw.get("minLength"),
        max_length=raw.get("maxLength"),
        minimum=raw.get("minimum"),
        maximum=raw.get("maximum"),
        items=items,
        max_items=raw.get("maxItems"),
        properties=fields,
        required=required,
    )


# --------------------------------------------------------------------- #
# Trusted registry entries
# --------------------------------------------------------------------- #


class MCPServerDescriptor(BaseModel):
    """Sam's trusted record that a server exists. Holds no security
    metadata a server could influence and no credentials."""

    model_config = ConfigDict(frozen=True)

    server_id: MCPServerId
    display_name: str = Field(min_length=1, max_length=100)

    @field_validator("display_name", mode="before")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = sanitize_display_text(value, max_length=100)
        if cleaned is None:
            raise ValueError("display_name must not be blank")
        return cleaned


class MCPToolPermissionBinding(BaseModel):
    """Which existing ``(PermissionResource, PermissionAction)`` a tool maps
    to, and which validated arguments contribute to its scope.

    Chosen by Sam's own code at registration, never inferred from a tool's
    description and never accepted from a server. The pair must already be
    classified in ``sam.permissions.policy`` (an unclassified pair would be
    denied by the engine anyway — rejecting it at registration just makes
    the mistake loud) and the resource must be one MCP tools may bind to.
    """

    model_config = ConfigDict(frozen=True)

    resource: PermissionResource
    action: PermissionAction
    scope_arguments: tuple[str, ...] = Field(
        default=(), max_length=MAX_MCP_SCOPE_ARGUMENTS
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.resource not in MCP_BINDABLE_RESOURCES:
            raise ValueError("resource is not bindable by an MCP tool")
        if classify(self.resource, self.action) is None:
            raise ValueError("resource/action pair is not classified by policy")
        if len(set(self.scope_arguments)) != len(self.scope_arguments):
            raise ValueError("scope_arguments must be unique")
        for name in self.scope_arguments:
            if not _FIELD_NAME_RE.match(name):
                raise ValueError("scope_arguments must be argument names")
        return self


class MCPToolDescriptor(BaseModel):
    """One trusted registry entry — Sam's complete view of one tool."""

    model_config = ConfigDict(frozen=True)

    server_id: MCPServerId
    tool_name: MCPToolName
    description: str = Field(default="", max_length=MAX_MCP_DESCRIPTION_LENGTH)
    input_schema: MCPToolSchema
    binding: MCPToolPermissionBinding
    credential: CredentialReference | None = None
    timeout_seconds: float = Field(default=10.0, gt=0, le=MAX_MCP_TIMEOUT_SECONDS)
    max_input_bytes: int = Field(
        default=MAX_MCP_INPUT_SIZE, ge=2, le=MAX_MCP_INPUT_SIZE
    )
    max_output_bytes: int = Field(
        default=MAX_MCP_OUTPUT_SIZE, ge=2, le=MAX_MCP_OUTPUT_SIZE
    )
    verification_required: bool = False
    enabled: bool = True

    @field_validator("description", mode="before")
    @classmethod
    def _clean_description(cls, value: str | None) -> str:
        # Display text only: never read by policy, scope, risk,
        # confirmation, credential selection, timeout, or any limit.
        return sanitize_display_text(value, max_length=MAX_MCP_DESCRIPTION_LENGTH) or ""

    @field_validator("timeout_seconds")
    @classmethod
    def _finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("timeout must be finite")
        return value

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.credential is not None and self.credential.server_id != self.server_id:
            raise ValueError("credential reference belongs to a different server")
        for name in self.binding.scope_arguments:
            spec = self.input_schema.field(name)
            if spec is None or name not in self.input_schema.required:
                raise ValueError("scope argument must be a required schema property")
            if spec.type not in (MCPSchemaType.STRING, MCPSchemaType.INTEGER):
                raise ValueError("scope argument must be a string or integer")
        return self

    @property
    def tool_id(self) -> MCPToolId:
        return MCPToolId(server_id=self.server_id, tool_name=self.tool_name)


class MCPDiscoveredTool(BaseModel):
    """What a server *claims* it offers. Untrusted; never executable by
    itself and never merged into the registry."""

    model_config = ConfigDict(frozen=True)

    name: MCPToolName
    description: str = Field(default="", max_length=MAX_MCP_DESCRIPTION_LENGTH)
    input_schema: MCPToolSchema | None = None  # None = did not parse as our subset
    ignored_key_count: int = Field(default=0, ge=0)


class MCPDiscoveryResult(BaseModel):
    """Bounded, frozen, data-only outcome of comparing a server's advertised
    tools to the trusted registry. It carries no handle to the registry and
    grants nothing; see ``sam.mcp.discovery``."""

    model_config = ConfigDict(frozen=True)

    server_id: MCPServerId
    matched: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()  # advertised, not registered: stays unavailable
    schema_mismatch: tuple[str, ...] = ()  # registered tools now disabled
    not_advertised: tuple[str, ...] = ()
    malformed_count: int = Field(default=0, ge=0)


# --------------------------------------------------------------------- #
# Request / call / context
# --------------------------------------------------------------------- #


class MCPToolRequest(BaseModel):
    """What a caller (ultimately an LLM proposal) asks for. Carries no
    credentials, no permission data, no confirmation id, and no server
    metadata — only a canonical tool id and structured arguments, both
    untrusted until the gateway validates them."""

    model_config = ConfigDict(frozen=True)

    principal: Principal
    tool_id: str = Field(min_length=1, max_length=MAX_MCP_TOOL_ID_LENGTH)
    arguments: dict[str, Any] = Field(default_factory=dict)
    request_id: str = Field(
        default_factory=new_id,
        min_length=1,
        max_length=MAX_MCP_REQUEST_ID_LENGTH,
        pattern=_REQUEST_ID_RE.pattern,
    )
    reason: str | None = Field(default=None, max_length=MAX_MCP_REASON_LENGTH)

    @field_validator("reason", mode="before")
    @classmethod
    def _clean_reason(cls, value: str | None) -> str | None:
        return sanitize_display_text(value, max_length=MAX_MCP_REASON_LENGTH)


class MCPToolCall(BaseModel):
    """The validated, resolved call handed to a transport. The credential is
    deliberately not a field: it is passed separately, only to the one
    transport registered for this server."""

    model_config = ConfigDict(frozen=True)

    server_id: MCPServerId
    tool_name: MCPToolName
    arguments: dict[str, Any]


class MCPExecutionContext(BaseModel):
    """Bounded execution metadata preserved across authorization,
    execution, verification, and audit. Holds no credential."""

    model_config = ConfigDict(frozen=True)

    request_id: str = Field(min_length=1, max_length=MAX_MCP_REQUEST_ID_LENGTH)
    execution_id: str = Field(min_length=1, max_length=100)
    principal: Principal
    timeout_seconds: float = Field(gt=0, le=MAX_MCP_TIMEOUT_SECONDS)


# --------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------- #


class MCPToolResult(BaseModel):
    """A validated tool result with provenance. ``untrusted_external_content``
    is always ``True``: the content is data from a third party, may contain
    prompt injection, and must never be read as an instruction or as
    authorization."""

    model_config = ConfigDict(frozen=True)

    server_id: MCPServerId
    tool_id: str = Field(max_length=MAX_MCP_TOOL_ID_LENGTH)
    execution_id: str = Field(min_length=1, max_length=100)
    content_json: str
    size_bytes: int = Field(ge=0)
    untrusted_external_content: bool = True

    @field_validator("untrusted_external_content")
    @classmethod
    def _always_untrusted(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("MCP results are always untrusted external content")
        return value

    @property
    def content(self) -> Any:
        """A fresh parsed copy of the (validated JSON) content."""

        return json.loads(self.content_json)


class MCPVerificationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: VerificationStatus
    reason_code: str | None = Field(default=None, max_length=64)


class MCPExecutionResult(BaseModel):
    """What ``MCPGateway.execute`` always returns."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    execution_id: str
    principal: Principal
    tool_id: str | None = None
    status: MCPExecutionStatus
    permission_outcome: PermissionOutcomeSummary | None = None
    error_category: MCPErrorCategory | None = None
    confirmation_id: str | None = None
    result: MCPToolResult | None = None
    verification: MCPVerificationResult | None = None
    execution_attempted: bool = False
    duration_ms: int = Field(default=0, ge=0)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _shape(self) -> Self:
        carries_result = self.status in (
            MCPExecutionStatus.SUCCEEDED,
            MCPExecutionStatus.UNVERIFIED,
        )
        if carries_result != (self.result is not None):
            raise ValueError("only SUCCEEDED/UNVERIFIED results carry a result")
        if (
            not carries_result
            and self.status is not MCPExecutionStatus.CONFIRMATION_REQUIRED
        ):
            if self.error_category is None:
                raise ValueError("a failed/rejected/denied result needs a category")
        if carries_result and self.error_category is not None:
            raise ValueError("a successful result must not carry an error category")
        if self.status is MCPExecutionStatus.SUCCEEDED and (
            self.verification is None
            or self.verification.status
            not in (VerificationStatus.VERIFIED, VerificationStatus.NOT_REQUIRED)
        ):
            raise ValueError("SUCCEEDED requires VERIFIED or NOT_REQUIRED")
        return self


class MCPAuditEvent(BaseModel):
    """One immutable, content-free record of a gateway request. Carries only
    closed enums, identifiers, sizes, and durations — there is no field
    that could hold an argument value, a result body, or a credential."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=100)
    occurred_at: datetime
    request_id: str
    execution_id: str
    principal: Principal
    server_id: str | None = Field(default=None, max_length=MAX_MCP_SERVER_ID_LENGTH)
    tool_id: str | None = Field(default=None, max_length=MAX_MCP_TOOL_ID_LENGTH)
    permission_action: PermissionAction | None = None
    permission_resource: PermissionResource | None = None
    risk: RiskLevel | None = None
    scope: str | None = Field(default=None, max_length=500)
    authorization_outcome: PermissionOutcomeSummary | None = None
    execution_status: MCPExecutionStatus
    verification_status: VerificationStatus | None = None
    error_category: MCPErrorCategory | None = None
    execution_attempted: bool = False
    duration_ms: int = Field(default=0, ge=0)
    input_size: int | None = Field(default=None, ge=0)
    output_size: int | None = Field(default=None, ge=0)

    @field_validator("occurred_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("audit timestamps must be timezone-aware")
        return value


__all__ = [
    "MAX_CREDENTIAL_LENGTH",
    "MAX_MCP_ARGUMENT_DEPTH",
    "MAX_MCP_ARGUMENT_ITEMS",
    "MAX_MCP_INPUT_SIZE",
    "MAX_MCP_OUTPUT_SIZE",
    "MAX_MCP_RESULT_DEPTH",
    "MAX_MCP_SERVER_ID_LENGTH",
    "MAX_MCP_STRING_LENGTH",
    "MAX_MCP_TIMEOUT_SECONDS",
    "MAX_MCP_TOOL_NAME_LENGTH",
    "MAX_TRACKED_REQUEST_IDS",
    "MCP_BINDABLE_RESOURCES",
    "CredentialReference",
    "MCPAuditEvent",
    "MCPDiscoveredTool",
    "MCPDiscoveryResult",
    "MCPErrorCategory",
    "MCPExecutionContext",
    "MCPExecutionResult",
    "MCPExecutionStatus",
    "MCPServerDescriptor",
    "MCPServerId",
    "MCPToolCall",
    "MCPToolDescriptor",
    "MCPToolId",
    "MCPToolName",
    "MCPToolPermissionBinding",
    "MCPToolRequest",
    "MCPToolResult",
    "MCPToolSchema",
    "MCPVerificationResult",
    "PermissionOutcomeSummary",
    "VerificationStatus",
    "canonical_json",
    "new_id",
    "sanitize_display_text",
    "utc_now",
]
