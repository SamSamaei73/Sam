"""Typed, immutable domain models for the permission engine.

These models depend on nothing outside the standard library and Pydantic:
no FastAPI, no Anthropic SDK, no database driver. That keeps the
authorization boundary provider-neutral and testable in isolation.

Design note on closed sets: ``PermissionAction`` and ``PermissionResource``
are enums, not free strings. An enum member that was never registered in
``sam.permissions.policy`` is still a *valid* value (it can be constructed)
but is treated as an unclassified combination by the engine and denied —
see ``sam.permissions.policy.classify``. A string that is not a member of
either enum cannot be constructed into a request at all; Pydantic rejects
it before the engine ever sees it. Both paths fail closed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_MAX_TEXT_LENGTH = 500
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_display_text(value: str | None) -> str | None:
    """Strip control characters and bound length for untrusted display text.

    Used for fields that may be influenced by the LLM or another untrusted
    caller (``reason``, ``target``) and are only ever shown to a human for
    confirmation — never used in an authorization decision.
    """

    if value is None:
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()
    if not cleaned:
        return None
    return cleaned[:_MAX_TEXT_LENGTH]


class PermissionAction(StrEnum):
    """Authorization concepts only — no action here performs I/O itself."""

    READ = "read"
    WRITE = "write"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    EXECUTE = "execute"
    SEND = "send"
    PUBLISH = "publish"
    APPROVE = "approve"


class PermissionResource(StrEnum):
    """Resource categories a future tool/integration will identify as.

    No resource here is wired to a real integration in Phase 3.
    """

    FILESYSTEM = "filesystem"
    TERMINAL = "terminal"
    GIT = "git"
    GMAIL = "gmail"
    CALENDAR = "calendar"
    BROWSER = "browser"
    SOCIAL_MEDIA = "social_media"
    MCP = "mcp"
    DATABASE = "database"
    FINANCIAL_SERVICE = "financial_service"
    COMPUTER = "computer"
    CODE = "code"
    KNOWLEDGE = "knowledge"
    VOICE = "voice"


class RiskLevel(StrEnum):
    """Deterministic risk classification, never chosen by the LLM.

    LOW: safe, read-only, non-sensitive operations.
    MEDIUM: state-changing but generally reversible operations.
    HIGH: sensitive external or destructive operations.
    CRITICAL: irreversible, financial, security-sensitive, or highly
    consequential operations. CRITICAL always requires confirmation —
    see ``PermissionEngine``; no grant can waive that.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DecisionOutcome(StrEnum):
    """The three, unambiguous results of one evaluation."""

    ALLOW = "allow"
    DENY = "deny"
    CONFIRM_REQUIRED = "confirm_required"


class DenialReason(StrEnum):
    """Closed set of denial causes — informative without leaking internals."""

    NO_MATCHING_GRANT = "no_matching_grant"
    GRANT_REVOKED = "grant_revoked"
    GRANT_EXPIRED = "grant_expired"
    UNCLASSIFIED_ACTION = "unclassified_action"
    CONFIRMATION_INVALID = "confirmation_invalid"
    SYSTEM_ERROR = "system_error"


class ConfirmationStatus(StrEnum):
    """Lifecycle states for one confirmation record."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class GrantStatus(StrEnum):
    """Lifecycle state of a persistent grant. Revocation, never deletion."""

    ACTIVE = "active"
    REVOKED = "revoked"


class PrincipalKind(StrEnum):
    """The kind of identity a permission can belong to.

    Phase 3 implements no authentication; this only keeps the model from
    hard-coding a single identity concept so later phases (real user
    accounts, service credentials, third-party integrations) can be
    represented without a breaking change.
    """

    USER = "user"
    SAM = "sam"
    SERVICE = "service"
    INTEGRATION = "integration"


class Principal(BaseModel):
    """An identity that grants belong to and requests act on behalf of."""

    model_config = ConfigDict(frozen=True)

    kind: PrincipalKind
    id: str = Field(min_length=1, max_length=200)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("principal id must not be blank")
        return stripped


class PermissionScope(BaseModel):
    """A deterministic, hierarchical scope identifier.

    Scopes are a tuple of already-normalized segments. Matching is always
    exact-segment based — never raw substring or string-prefix matching —
    so a grant for ``("Projects", "Sam")`` never matches a request for
    ``("Projects", "Sam-Secret")``: the second segment differs as a whole
    unit, regardless of any shared characters.

    Use ``from_path`` for hierarchical, filesystem-like resources (this
    resolves ``.``/``..`` deterministically and rejects traversal above the
    scope's own root). Use ``identifier`` for a single, non-hierarchical
    resource name (a repository id, a mailbox, a service account).
    """

    model_config = ConfigDict(frozen=True)

    segments: tuple[str, ...] = Field(min_length=1)

    @field_validator("segments")
    @classmethod
    def _validate_segments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for segment in value:
            if not segment or not segment.strip():
                raise ValueError("scope segments must be non-empty")
            if segment in (".", ".."):
                raise ValueError("scope segments must already be normalized")
        return value

    @classmethod
    def from_path(cls, raw: str) -> Self:
        """Build a normalized scope from a filesystem-like path string.

        Resolves ``.`` and ``..`` segments deterministically and raises
        ``ValueError`` if the path would traverse above its own root — it
        never silently clamps to root, since that could make an intended
        escape attempt look like a harmless, matchable scope.
        """

        if not raw or not raw.strip():
            raise ValueError("scope path must be a non-empty string")
        normalized: list[str] = []
        for part in raw.replace("\\", "/").split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not normalized:
                    raise ValueError("scope path traverses above its root")
                normalized.pop()
                continue
            normalized.append(part)
        if not normalized:
            raise ValueError("scope path must resolve to at least one segment")
        return cls(segments=tuple(normalized))

    @classmethod
    def identifier(cls, value: str) -> Self:
        """Build a single-segment scope for a non-hierarchical resource."""

        if not value or not value.strip():
            raise ValueError("scope identifier must be a non-empty string")
        return cls(segments=(value.strip(),))

    def contains(self, other: PermissionScope) -> bool:
        """True if ``other`` is this scope exactly, or a strict descendant.

        Comparison is always whole-segment: a shorter ``other`` never
        matches, and a longer ``other`` matches only if this scope's full
        segment tuple is a prefix of it, segment by segment.
        """

        if len(other.segments) < len(self.segments):
            return False
        return other.segments[: len(self.segments)] == self.segments

    def as_text(self) -> str:
        """A safe, bounded string form for audit records and display."""

        return "/".join(self.segments)


class PermissionRequest(BaseModel):
    """One immutable request: "may this principal do this, here, now?"

    Constructing an invalid request (unknown action/resource string,
    malformed scope) raises a Pydantic ``ValidationError`` before the
    engine is ever involved — a caller that cannot construct a valid
    request must treat that failure as a denial, never proceed anyway.
    """

    model_config = ConfigDict(frozen=True)

    principal: Principal
    action: PermissionAction
    resource: PermissionResource
    scope: PermissionScope
    reason: str | None = Field(default=None, max_length=_MAX_TEXT_LENGTH)
    target: str | None = Field(default=None, max_length=_MAX_TEXT_LENGTH)
    correlation_id: str | None = Field(default=None, max_length=100)

    @field_validator("reason", "target", mode="before")
    @classmethod
    def _sanitize(cls, value: str | None) -> str | None:
        return _sanitize_display_text(value)


class PermissionGrant(BaseModel):
    """A persistent authorization for one principal/resource/action/scope.

    A grant answers "is this within the user's granted authority?" — not
    "should this specific execution proceed right now?". See
    ``always_require_confirmation``: a grant can only *add* a confirmation
    requirement on top of policy, never waive one policy already requires.
    There is deliberately no field that lets a grant skip confirmation.
    """

    model_config = ConfigDict(frozen=True)

    grant_id: str = Field(min_length=1, max_length=100)
    principal: Principal
    resource: PermissionResource
    action: PermissionAction
    scope: PermissionScope
    always_require_confirmation: bool = False
    status: GrantStatus = GrantStatus.ACTIVE
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None = None
    expires_at: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at", "revoked_at", "expires_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("grant timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> Self:
        if self.status is GrantStatus.REVOKED and self.revoked_at is None:
            raise ValueError("a revoked grant must record revoked_at")
        if self.status is GrantStatus.ACTIVE and self.revoked_at is not None:
            raise ValueError("an active grant must not record revoked_at")
        return self

    def is_usable(self, *, now: datetime) -> bool:
        """True only if active and not expired as of ``now``."""

        if self.status is not GrantStatus.ACTIVE:
            return False
        if self.expires_at is not None and self.expires_at <= now:
            return False
        return True


class ConfirmationRecord(BaseModel):
    """One confirmation's full, immutable-by-replacement lifecycle state.

    Confirmations are one-time and context-bound: ``consume`` (see
    ``sam.permissions.confirmation``) verifies the exact principal, action,
    resource, scope, and target match, then transitions the record to
    ``CONSUMED`` so it can never authorize a second action — see the
    engine's replay-protection tests.
    """

    model_config = ConfigDict(frozen=True)

    confirmation_id: str = Field(min_length=1, max_length=100)
    principal: Principal
    action: PermissionAction
    resource: PermissionResource
    scope: PermissionScope
    target: str | None = None
    reason: str | None = None
    risk: RiskLevel
    status: ConfirmationStatus
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    correlation_id: str | None = None

    @field_validator("created_at", "expires_at", "decided_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("confirmation timestamps must be timezone-aware")
        return value

    def matches(self, request: PermissionRequest) -> bool:
        """True if this confirmation was issued for exactly this request."""

        return (
            self.principal == request.principal
            and self.action == request.action
            and self.resource == request.resource
            and self.scope == request.scope
            and self.target == request.target
        )


class PermissionDecision(BaseModel):
    """The engine's answer: always one explicit outcome, never a bare bool."""

    model_config = ConfigDict(frozen=True)

    outcome: DecisionOutcome
    request: PermissionRequest
    risk: RiskLevel
    decided_at: datetime
    grant_id: str | None = None
    reason: DenialReason | None = None
    confirmation: ConfirmationRecord | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.outcome is DecisionOutcome.DENY and self.reason is None:
            raise ValueError("a DENY decision must carry a DenialReason")
        if self.outcome is DecisionOutcome.ALLOW and self.reason is not None:
            raise ValueError("an ALLOW decision must not carry a DenialReason")
        needs_confirmation = self.outcome is DecisionOutcome.CONFIRM_REQUIRED
        if needs_confirmation and self.confirmation is None:
            raise ValueError("a CONFIRM_REQUIRED decision must carry a confirmation")
        return self


class AuditEvent(BaseModel):
    """One immutable, secret-free record of a permission evaluation.

    Only closed-enum fields and bounded/sanitized text are recorded. Full
    request payloads, credentials, and hidden prompts are never captured —
    see ``sam.permissions.audit`` for the sink abstraction.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=100)
    occurred_at: datetime
    principal: Principal
    action: PermissionAction
    resource: PermissionResource
    scope_summary: str = Field(max_length=_MAX_TEXT_LENGTH)
    risk: RiskLevel
    outcome: DecisionOutcome
    reason: DenialReason | None = None
    grant_id: str | None = None
    confirmation_id: str | None = None
    confirmation_status: ConfirmationStatus | None = None
    correlation_id: str | None = None

    @field_validator("occurred_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("audit timestamps must be timezone-aware")
        return value


def utc_now() -> datetime:
    """The single clock function used throughout the permission domain."""

    return datetime.now(UTC)
