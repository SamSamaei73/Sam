"""Declared-vs-actual resource type validation.

A caller's ``declared_resource_type`` (derived from a filename extension,
a form field, or similar) is never trusted on its own — see the Phase 7
task's "never assume a file extension is trustworthy" rule. This module
performs a light, honest content sniff and flags an unambiguous mismatch;
it does not attempt to *detect* the true type for formats with no
reliable magic bytes (TXT/Markdown/CSV are all plain text and are not
distinguishable from bytes alone) — only to catch a declaration that
contradicts what the content clearly is.
"""

from __future__ import annotations

from sam.knowledge.errors import UnsupportedResourceError
from sam.knowledge.models import ResourceType

_PDF_SIGNATURE = b"%PDF-"
_NULL_BYTE = b"\x00"


def sniff_conflicting_type(
    content: bytes, declared: ResourceType
) -> ResourceType | None:
    """Return the type the content unambiguously looks like, if that
    contradicts ``declared`` — otherwise ``None``.

    Only PDF has a reliable, cheap-to-check magic signature. Every other
    supported format is plain text and cannot be reliably fingerprinted
    from bytes alone, so this function only ever reports a conflict
    against PDF's signature — it never claims to positively identify
    TXT/Markdown/JSON/CSV from content.
    """

    looks_like_pdf = content.lstrip()[:1024].find(_PDF_SIGNATURE) != -1
    if looks_like_pdf and declared is not ResourceType.PDF:
        return ResourceType.PDF
    if declared is ResourceType.PDF and not looks_like_pdf:
        # A declared PDF whose bytes do not contain the signature within
        # a reasonable header window is not a PDF at all.
        raise UnsupportedResourceError(
            "declared resource type is pdf but content has no PDF signature"
        )
    return None


def validate_declared_type(content: bytes, declared: ResourceType) -> None:
    """Raise ``UnsupportedResourceError`` if ``content`` is unambiguously
    not what ``declared`` claims it is. Never silently reclassifies —
    only rejects."""

    if declared in (ResourceType.TXT, ResourceType.MARKDOWN, ResourceType.CSV):
        if _NULL_BYTE in content:
            raise UnsupportedResourceError(
                f"declared resource type {declared.value} must be text but "
                "content contains a null byte"
            )
    conflict = sniff_conflicting_type(content, declared)
    if conflict is not None:
        raise UnsupportedResourceError(
            f"declared resource type {declared.value} does not match content "
            f"(looks like {conflict.value})"
        )


__all__ = ["sniff_conflicting_type", "validate_declared_type"]
