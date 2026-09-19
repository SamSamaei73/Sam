"""Secret protection for Knowledge ingestion.

A best-effort, documented-as-non-exhaustive second layer, the same
discipline ``sam.coding.policy`` applies to file reads: even a
permission-authorized ingestion must never persist a resource whose
filename, content, or metadata looks like a secret. Unlike
``sam.coding``'s read-time redaction, Knowledge's answer is unconditional
rejection — see ``docs/knowledge.md``'s "Secret handling" section: no
redacted copy is ever stored as if the original had been safely learned.

Reuses ``sam.memory.sanitization.looks_like_secret`` directly rather than
duplicating its pattern list. This is the only thing imported from
``sam.memory`` anywhere in this package, and it is a pure, stateless
function — importing it creates no dependency on Memory's storage,
policy, or engine, and no Knowledge content ever reaches
``sam.memory.engine``.
"""

from __future__ import annotations

import re

from sam.knowledge.models import DocumentMetadata
from sam.memory.sanitization import looks_like_secret as _content_looks_like_secret

_RESTRICTED_FILENAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\.env(\..+)?$", re.IGNORECASE),
    re.compile(r".*\.pem$", re.IGNORECASE),
    re.compile(r".*\.key$", re.IGNORECASE),
    re.compile(r"^credentials(\..+)?$", re.IGNORECASE),
    re.compile(r"^secrets?(\..+)?$", re.IGNORECASE),
    re.compile(r".*id_rsa.*", re.IGNORECASE),
    re.compile(r".*id_ed25519.*", re.IGNORECASE),
)


def is_restricted_filename(name: str) -> bool:
    """True if the final path segment of ``name`` matches a known
    secret-bearing filename pattern."""

    last_segment = name.rsplit("/", 1)[-1]
    return any(pattern.match(last_segment) for pattern in _RESTRICTED_FILENAME_PATTERNS)


def is_restricted_content(text: str) -> bool:
    """True if ``text`` matches a known secret-like content pattern."""

    return _content_looks_like_secret(text)


def is_restricted_metadata(metadata: DocumentMetadata) -> bool:
    """True if any metadata value (title, author, or a custom entry)
    looks like a secret. Metadata is display text, but the task is
    explicit: "Do not store secrets in metadata" — so it gets the same
    check as document content."""

    candidates = [metadata.title, metadata.author, *metadata.custom.values()]
    return any(
        value is not None and _content_looks_like_secret(value) for value in candidates
    )


__all__ = [
    "is_restricted_content",
    "is_restricted_filename",
    "is_restricted_metadata",
]
