"""Safe diagnostics for a non-2xx Gemini response.

A failing GenerateContent call returns a JSON error body. Sam extracts ONLY
allowlisted, bounded fields from it so the owner can tell an invalid request from
a key, region or billing prerequisite problem, without ever exposing the raw body:

* ``http_status``: the HTTP status Sam saw
* ``error_code``: ``error.code`` (an integer status, or a short allowlisted name)
* ``error_status``: ``error.status`` / a string ``error.code`` mapped through an
  allowlist (``INVALID_ARGUMENT``, ``FAILED_PRECONDITION`` ...), else ``UNKNOWN``
* ``reason``: an ``ErrorInfo.reason`` (``API_KEY_INVALID`` ...) ONLY when it is on
  the allowlist; ``error.details`` is never copied anywhere else
* ``message``: ``error.message`` sanitized (secrets, prompt text, URLs, project
  ids and token-like strings removed) and truncated

Nothing here is used for routing. The adapter still raises the same category and
short code it always did; this detail rides on the exception for the owner-run
smoke tool only and is never recorded by the router, audit log or UI.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

MAX_ERROR_BODY_BYTES = 16_384
MAX_MESSAGE_CHARS = 160
_MIN_REDACT_LEN = 4

# ``error.status`` (google.rpc.Code names) and the lowercase ``error.code`` names
# used by the current API error reference, normalized to upper case.
ALLOWED_STATUS = frozenset(
    {
        "INVALID_ARGUMENT",
        "FAILED_PRECONDITION",
        "OUT_OF_RANGE",
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
        "NOT_FOUND",
        "ABORTED",
        "RESOURCE_EXHAUSTED",
        "CANCELLED",
        "INTERNAL",
        "UNAVAILABLE",
        "DEADLINE_EXCEEDED",
        "INVALID_REQUEST",
        "RATE_LIMIT_EXCEEDED",
        "API_ERROR",
        "SERVICE_UNAVAILABLE",
    }
)
# google.rpc.ErrorInfo.reason values worth surfacing. Anything else is dropped.
ALLOWED_REASONS = frozenset(
    {
        "API_KEY_INVALID",
        "API_KEY_EXPIRED",
        "API_KEY_SERVICE_BLOCKED",
        "API_KEY_HTTP_REFERRER_BLOCKED",
        "API_KEY_IP_ADDRESS_BLOCKED",
        "SERVICE_DISABLED",
        "RATE_LIMIT_EXCEEDED",
        "RESOURCE_EXHAUSTED",
        "CONSUMER_SUSPENDED",
        "BILLING_DISABLED",
    }
)

_URL = re.compile(r"https?://\S+", re.I)
_PROJECT = re.compile(r"projects/[^\s/'\"]+", re.I)
_TOKENISH = re.compile(r"[A-Za-z0-9_\-]{20,}")
_LONG_DIGITS = re.compile(r"\d{6,}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE = re.compile(r"\s+")

SafeDetail = dict[str, str | int]


def sanitize_message(text: str, redact: Iterable[str] = ()) -> str:
    """Remove secrets and anything that could carry content, then truncate."""

    cleaned = text
    for secret in sorted(set(redact), key=len, reverse=True):
        if len(secret) >= _MIN_REDACT_LEN:
            cleaned = re.sub(re.escape(secret), "[redacted]", cleaned, flags=re.I)
    cleaned = _URL.sub("[url]", cleaned)
    cleaned = _PROJECT.sub("projects/[id]", cleaned)
    cleaned = _TOKENISH.sub("[redacted]", cleaned)
    cleaned = _LONG_DIGITS.sub("[n]", cleaned)
    cleaned = _SPACE.sub(" ", _CONTROL.sub(" ", cleaned)).strip()
    if len(cleaned) > MAX_MESSAGE_CHARS:
        cleaned = cleaned[: MAX_MESSAGE_CHARS - 1].rstrip() + "…"
    return cleaned


def _status_name(value: object) -> str | None:
    if isinstance(value, str) and value.strip().upper() in ALLOWED_STATUS:
        return value.strip().upper()
    return None


def _reason(details: object) -> str | None:
    if not isinstance(details, list):
        return None
    for item in details:
        if not isinstance(item, dict):
            continue
        kind = item.get("@type")
        reason = item.get("reason")
        if (
            isinstance(kind, str)
            and kind.endswith("google.rpc.ErrorInfo")
            and isinstance(reason, str)
            and reason in ALLOWED_REASONS
        ):
            return reason
    return None


def safe_error_detail(
    http_status: int, body: bytes | None, *, redact: Iterable[str] = ()
) -> SafeDetail:
    """Allowlisted fields from an error response. Never raises, never returns the
    raw body, ``error.details`` content or an unknown ``reason``."""

    detail: SafeDetail = {"http_status": http_status}
    if not body:
        detail["shape"] = "empty"
        return detail
    try:
        document: Any = json.loads(body)
    except ValueError:
        detail["shape"] = "not_json"
        return detail
    error = document.get("error") if isinstance(document, dict) else None
    if not isinstance(error, dict):
        detail["shape"] = "unrecognized"
        return detail
    code = error.get("code")
    status = _status_name(error.get("status"))
    if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599:
        detail["error_code"] = code
    elif isinstance(code, str):
        named = _status_name(code)
        detail["error_code"] = named or "UNKNOWN"
        status = status or named
    detail["error_status"] = status or "UNKNOWN"
    reason = _reason(error.get("details"))
    if reason is not None:
        detail["reason"] = reason
    message = error.get("message")
    if isinstance(message, str) and message.strip():
        detail["message"] = sanitize_message(message, redact)
    detail["shape"] = "google_rpc" if "status" in error else "simple"
    return detail


def format_detail(detail: SafeDetail) -> str:
    """One safe line for the owner-run smoke tool."""

    parts = [f"{key}={detail[key]!r}" for key in _ORDER if key in detail]
    return " | ".join(parts)


_ORDER = ("http_status", "error_code", "error_status", "reason", "shape", "message")

__all__ = [
    "ALLOWED_REASONS",
    "ALLOWED_STATUS",
    "MAX_ERROR_BODY_BYTES",
    "MAX_MESSAGE_CHARS",
    "SafeDetail",
    "format_detail",
    "safe_error_detail",
    "sanitize_message",
]
