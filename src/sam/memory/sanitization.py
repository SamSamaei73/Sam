"""Best-effort secret-pattern detection and redaction for memory content.

This is deliberately **not** a comprehensive secret scanner. It catches a
handful of common, high-signal patterns (API key prefixes, bearer tokens,
PEM private key blocks, a few well-known cloud/service token shapes, and
explicit "password is ..." / "api key is ..." phrasing) using simple
regular expressions. It will miss creatively obfuscated, base64-wrapped,
or otherwise disguised secrets, and may occasionally flag ordinary
long random-looking text as secret-like.

``sam.memory.policy`` treats any detection as a hard ``REJECT`` — Phase 4
never stores a redacted-but-still-persisted version of flagged content;
see ``docs/memory.md`` for the rationale. ``redact`` exists only so a
caller can safely *display* why something was rejected (an error message,
a future audit/UI surface) without ever repeating the raw secret text.

Callers must not treat a clean result from ``looks_like_secret`` as a
guarantee that content contains no sensitive data — it is one layer of
defense, not a substitute for the "do not store what you don't need"
policy default.
"""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# Each pattern is intentionally narrow and documented; broadening one
# should be a deliberate, reviewed decision, not an incidental regex tweak.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Anthropic API keys.
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    # Generic "sk-..." style API keys (OpenAI and similar).
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    # AWS access key ids.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # GitHub personal access / OAuth / app tokens.
    re.compile(r"\bgh[poasru]_[A-Za-z0-9]{20,}\b"),
    # Slack tokens.
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # Bearer authorization headers/phrases.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{10,}"),
    # PEM-encoded private key blocks.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # JWT-shaped strings (three dot-separated base64url segments).
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    # Explicit "password/passwd/pwd is/=/: ..." phrasing.
    re.compile(r"(?i)\b(password|passwd|pwd)\b\s*(?:is|[:=])\s*\S+"),
    # Explicit "api key / access token / secret key is/=/: ..." phrasing.
    re.compile(
        r"(?i)\b(api[_ -]?key|access[_ -]?token|secret[_ -]?key)\b"
        r"\s*(?:is|[:=])\s*\S+"
    ),
)


def looks_like_secret(text: str) -> bool:
    """True if ``text`` matches any known secret-like pattern."""

    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def redact(text: str) -> str:
    """Replace every detected secret-like span with a fixed placeholder.

    For safe display of *why* content was rejected — never used to
    produce a version of the content that gets stored.
    """

    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted
