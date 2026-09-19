"""Typed, immutable domain models for computer control.

Depends only on the standard library, Pydantic, and
``sam.permissions.models.Principal`` (reused rather than duplicated, the
same choice ``sam.memory.models`` makes). Nothing here performs I/O or
touches an automation library — that lives entirely behind
``sam.computer.backend.ComputerBackend``.

Every action is a strongly typed, capability-specific model (never a
``dict[str, Any]`` parameter bag) so a caller cannot smuggle an
unvalidated field past construction. Coordinates are plain ``int`` —
never ``float`` — which by itself makes NaN/infinity impossible to
construct; explicit bounds additionally reject negative values and
absurd magnitudes before any backend or screen-size knowledge is
involved (see ``sam.computer.controller`` for the second, real-screen-
size-aware bounds check).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sam.permissions.models import Principal, RiskLevel

# A coordinate above this is rejected regardless of any real screen size —
# no display remotely approaches this resolution; it exists purely to
# reject absurd integers cheaply, before any backend call.
_MAX_COORDINATE = 20_000
_MAX_SCROLL_AMOUNT = 100
_MAX_TYPED_TEXT_LENGTH = 500
_MAX_WINDOW_TITLE_LENGTH = 300
_MAX_APPLICATION_NAME_LENGTH = 200

_CONTROL_CHARS_EXCEPT_TAB_NEWLINE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def utc_now() -> datetime:
    """The single clock function used throughout the computer-control domain."""

    return datetime.now(UTC)


class ComputerCapability(StrEnum):
    """The closed set of capabilities Phase 5 implements.

    Deliberately excludes EXECUTE_COMMAND / SHELL / TERMINAL / DELETE_FILE
    / UPLOAD_FILE / DOWNLOAD_FILE / INSTALL_APPLICATION and anything else
    that would make this a general-purpose automation or file-manipulation
    surface — those are explicitly out of Phase 5's scope.
    """

    SCREENSHOT = "screenshot"
    GET_ACTIVE_WINDOW = "get_active_window"
    GET_SCREEN_SIZE = "get_screen_size"
    MOUSE_MOVE = "mouse_move"
    MOUSE_CLICK = "mouse_click"
    MOUSE_DOUBLE_CLICK = "mouse_double_click"
    MOUSE_SCROLL = "mouse_scroll"
    KEY_PRESS = "key_press"
    KEY_COMBINATION = "key_combination"
    TYPE_TEXT = "type_text"


class MouseButton(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


class ScrollDirection(StrEnum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


class ModifierKey(StrEnum):
    """A closed set of modifier keys for ``KeyCombinationAction``."""

    CTRL = "ctrl"
    ALT = "alt"
    SHIFT = "shift"
    CMD = "cmd"


class NamedKey(StrEnum):
    """A closed set of non-printing / control key names.

    ``KeyPressAction``/``KeyCombinationAction`` accept either a member of
    this enum or a single printable ASCII character (see
    ``_validate_key_name``) — never an arbitrary string.
    """

    ENTER = "enter"
    TAB = "tab"
    ESCAPE = "escape"
    BACKSPACE = "backspace"
    DELETE = "delete"
    SPACE = "space"
    ARROW_UP = "arrow_up"
    ARROW_DOWN = "arrow_down"
    ARROW_LEFT = "arrow_left"
    ARROW_RIGHT = "arrow_right"
    HOME = "home"
    END = "end"
    PAGE_UP = "page_up"
    PAGE_DOWN = "page_down"
    F1 = "f1"
    F2 = "f2"
    F3 = "f3"
    F4 = "f4"
    F5 = "f5"
    F6 = "f6"
    F7 = "f7"
    F8 = "f8"
    F9 = "f9"
    F10 = "f10"
    F11 = "f11"
    F12 = "f12"


_NAMED_KEY_VALUES = frozenset(member.value for member in NamedKey)


def _validate_key_name(value: str) -> str:
    """A key is either a named/control key, or exactly one printable,
    non-whitespace-except-space ASCII character. Anything else — a
    malformed identifier, an empty string, a multi-character "word",
    control characters — is rejected."""

    if value in _NAMED_KEY_VALUES:
        return value
    if len(value) == 1 and value.isascii() and value.isprintable():
        return value
    raise ValueError(f"unsupported key name: {value!r}")


class ComputerActionBase(BaseModel):
    """Shared shape for every caller-proposed action.

    Deliberately carries no ``action_id`` or ``created_at`` — those are
    always assigned by ``ComputerController`` when it produces the
    execution record, the same discipline ``sam.permissions`` (grant id)
    and ``sam.memory`` (memory id) use: an identifier or timestamp a
    caller could supply is never trusted for audit purposes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: Principal


class ScreenshotAction(ComputerActionBase):
    capability: Literal[ComputerCapability.SCREENSHOT] = ComputerCapability.SCREENSHOT


class GetActiveWindowAction(ComputerActionBase):
    capability: Literal[ComputerCapability.GET_ACTIVE_WINDOW] = (
        ComputerCapability.GET_ACTIVE_WINDOW
    )


class GetScreenSizeAction(ComputerActionBase):
    capability: Literal[ComputerCapability.GET_SCREEN_SIZE] = (
        ComputerCapability.GET_SCREEN_SIZE
    )


class MouseMoveAction(ComputerActionBase):
    capability: Literal[ComputerCapability.MOUSE_MOVE] = ComputerCapability.MOUSE_MOVE
    x: int = Field(ge=0, le=_MAX_COORDINATE)
    y: int = Field(ge=0, le=_MAX_COORDINATE)


class MouseClickAction(ComputerActionBase):
    capability: Literal[ComputerCapability.MOUSE_CLICK] = ComputerCapability.MOUSE_CLICK
    x: int = Field(ge=0, le=_MAX_COORDINATE)
    y: int = Field(ge=0, le=_MAX_COORDINATE)
    button: MouseButton = MouseButton.LEFT


class MouseDoubleClickAction(ComputerActionBase):
    capability: Literal[ComputerCapability.MOUSE_DOUBLE_CLICK] = (
        ComputerCapability.MOUSE_DOUBLE_CLICK
    )
    x: int = Field(ge=0, le=_MAX_COORDINATE)
    y: int = Field(ge=0, le=_MAX_COORDINATE)
    button: MouseButton = MouseButton.LEFT


class MouseScrollAction(ComputerActionBase):
    capability: Literal[ComputerCapability.MOUSE_SCROLL] = (
        ComputerCapability.MOUSE_SCROLL
    )
    x: int = Field(ge=0, le=_MAX_COORDINATE)
    y: int = Field(ge=0, le=_MAX_COORDINATE)
    direction: ScrollDirection
    amount: int = Field(gt=0, le=_MAX_SCROLL_AMOUNT)


class KeyPressAction(ComputerActionBase):
    capability: Literal[ComputerCapability.KEY_PRESS] = ComputerCapability.KEY_PRESS
    key: str

    @field_validator("key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        return _validate_key_name(value)


class KeyCombinationAction(ComputerActionBase):
    capability: Literal[ComputerCapability.KEY_COMBINATION] = (
        ComputerCapability.KEY_COMBINATION
    )
    modifiers: tuple[ModifierKey, ...] = Field(min_length=1, max_length=4)
    key: str

    @field_validator("modifiers")
    @classmethod
    def _validate_modifiers_unique(
        cls, value: tuple[ModifierKey, ...]
    ) -> tuple[ModifierKey, ...]:
        if len(set(value)) != len(value):
            raise ValueError("modifiers must not repeat")
        return value

    @field_validator("key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        return _validate_key_name(value)


class TypeTextAction(ComputerActionBase):
    """Typing is the most sensitive capability: bounded length, no null
    bytes, no control characters other than tab/newline (explicitly kept
    because they are ordinary, legitimate typed input — a form field or a
    multi-line note — while every other control character is rejected
    outright rather than silently stripped, since silently altering typed
    text could change its meaning without the caller noticing)."""

    capability: Literal[ComputerCapability.TYPE_TEXT] = ComputerCapability.TYPE_TEXT
    text: str = Field(min_length=1, max_length=_MAX_TYPED_TEXT_LENGTH)

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("typed text must not contain null bytes")
        if _CONTROL_CHARS_EXCEPT_TAB_NEWLINE.search(value):
            raise ValueError(
                "typed text must not contain control characters "
                "other than tab/newline"
            )
        return value


ComputerAction = (
    ScreenshotAction
    | GetActiveWindowAction
    | GetScreenSizeAction
    | MouseMoveAction
    | MouseClickAction
    | MouseDoubleClickAction
    | MouseScrollAction
    | KeyPressAction
    | KeyCombinationAction
    | TypeTextAction
)


# --------------------------------------------------------------------- #
# Result models — controlled, bounded, never raw/unbounded payloads.
# --------------------------------------------------------------------- #


class ScreenSize(BaseModel):
    model_config = ConfigDict(frozen=True)

    width: int = Field(gt=0, le=_MAX_COORDINATE)
    height: int = Field(gt=0, le=_MAX_COORDINATE)


class WindowBounds(BaseModel):
    model_config = ConfigDict(frozen=True)

    x: int
    y: int
    width: int = Field(gt=0, le=_MAX_COORDINATE)
    height: int = Field(gt=0, le=_MAX_COORDINATE)


class ActiveWindow(BaseModel):
    """Structured, minimal window information — never clipboard, keystroke
    history, browser history, or unrelated-window data. See
    ``docs/computer-control.md``."""

    model_config = ConfigDict(frozen=True)

    application: str = Field(min_length=1, max_length=_MAX_APPLICATION_NAME_LENGTH)
    title: str = Field(max_length=_MAX_WINDOW_TITLE_LENGTH)
    bounds: WindowBounds


class ScreenshotResult(BaseModel):
    """A controlled result object, not a raw byte stream handed around
    freely. ``data`` is PNG-encoded image bytes; see
    ``docs/computer-control.md`` for its lifecycle — it is never persisted,
    never logged, never included in an audit record, and never written to
    Memory automatically."""

    model_config = ConfigDict(frozen=True)

    width: int = Field(gt=0, le=_MAX_COORDINATE)
    height: int = Field(gt=0, le=_MAX_COORDINATE)
    format: Literal["png"] = "png"
    data: bytes
    captured_at: datetime

    @field_validator("captured_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        return value


class PermissionOutcomeSummary(StrEnum):
    """A minimal echo of ``sam.permissions.models.DecisionOutcome`` used on
    the result — kept as its own closed enum (not a re-export) so this
    module never has to import ``DecisionOutcome`` just to describe three
    words back to a caller."""

    ALLOW = "allow"
    CONFIRM_REQUIRED = "confirm_required"
    DENY = "deny"


class ExecutionOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class ErrorCategory(StrEnum):
    """Closed set of failure categories — never a raw exception message."""

    PERMISSION_DENIED = "permission_denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_INVALID = "confirmation_invalid"
    VALIDATION_ERROR = "validation_error"
    OUT_OF_BOUNDS = "out_of_bounds"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    BACKEND_ERROR = "backend_error"
    SEQUENCE_TOO_LONG = "sequence_too_long"
    INTERNAL_ERROR = "internal_error"


class ComputerActionResult(BaseModel):
    """What ``ComputerController.execute`` always returns. Never claims
    ``SUCCESS`` unless the backend actually confirmed execution."""

    model_config = ConfigDict(frozen=True)

    action_id: str = Field(min_length=1, max_length=100)
    capability: ComputerCapability
    principal: Principal
    outcome: ExecutionOutcome
    permission_outcome: PermissionOutcomeSummary
    created_at: datetime
    error_category: ErrorCategory | None = None
    confirmation_id: str | None = None
    screen_size: ScreenSize | None = None
    active_window: ActiveWindow | None = None
    screenshot: ScreenshotResult | None = None

    @field_validator("created_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.outcome is ExecutionOutcome.FAILED and self.error_category is None:
            raise ValueError("a FAILED result must carry an error_category")
        if self.outcome is ExecutionOutcome.SUCCESS and self.error_category is not None:
            raise ValueError("a SUCCESS result must not carry an error_category")
        return self


class ComputerAuditOutcome(StrEnum):
    """Mirrors ``ExecutionOutcome``/``PermissionOutcomeSummary`` for the
    audit record, kept distinct for the same reason
    ``sam.memory.models.MemoryAuditOutcome`` is its own type rather than a
    permission-decision outcome."""

    SUCCESS = "success"
    DENIED = "denied"
    CONFIRM_REQUIRED = "confirm_required"
    FAILED = "failed"


class ComputerAuditEvent(BaseModel):
    """One immutable, content-free record of a computer-control action.

    Never carries typed text, screenshot bytes, coordinates from a typed
    field's content, or any raw exception message — only closed enums,
    ids, and bounded metadata. See ``docs/computer-control.md``.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=100)
    occurred_at: datetime
    action_id: str = Field(min_length=1, max_length=100)
    principal: Principal
    capability: ComputerCapability
    risk: RiskLevel
    permission_outcome: PermissionOutcomeSummary
    confirmation_outcome: ComputerAuditOutcome | None = None
    execution_outcome: ExecutionOutcome | None = None
    error_category: ErrorCategory | None = None
    correlation_id: str | None = Field(default=None, max_length=100)

    @field_validator("occurred_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("audit timestamps must be timezone-aware")
        return value


__all__ = [
    "ActiveWindow",
    "ComputerAction",
    "ComputerActionBase",
    "ComputerActionResult",
    "ComputerAuditEvent",
    "ComputerAuditOutcome",
    "ComputerCapability",
    "ErrorCategory",
    "ExecutionOutcome",
    "GetActiveWindowAction",
    "GetScreenSizeAction",
    "KeyCombinationAction",
    "KeyPressAction",
    "ModifierKey",
    "MouseButton",
    "MouseClickAction",
    "MouseDoubleClickAction",
    "MouseMoveAction",
    "MouseScrollAction",
    "NamedKey",
    "PermissionOutcomeSummary",
    "Principal",
    "RiskLevel",
    "ScreenSize",
    "ScreenshotAction",
    "ScreenshotResult",
    "ScrollDirection",
    "TypeTextAction",
    "WindowBounds",
    "utc_now",
]
