"""Typed errors raised by the Knowledge domain.

No name here shadows a Python builtin. Every error is caught at the
``KnowledgeEngine`` boundary (see ``sam.knowledge.engine``) and converted
into a closed ``KnowledgeErrorCategory`` — an exception's own message is
never surfaced to a caller, since it may carry filesystem, parser, or
library-internal details.
"""


class KnowledgeError(Exception):
    """Base class for all Knowledge domain failures."""


class UnsupportedResourceError(KnowledgeError):
    """The declared resource type has no supporting parser, or the
    declared type does not match what the content actually looks like."""


class ResourceTooLargeError(KnowledgeError):
    """Raw content, extracted text, or chunk count exceeds a configured
    bound."""


class ParsingError(KnowledgeError):
    """The parser could not interpret the content as a structurally valid
    document of its declared type (malformed PDF, invalid JSON, ...)."""


class ExtractionError(KnowledgeError):
    """Parsing succeeded structurally but produced no usable text — for
    example a scanned/image-only PDF with no extractable text layer.
    Never silently treated as success."""


class ChunkingError(KnowledgeError):
    """Chunking configuration was invalid, or chunking this document
    would exceed a bound (chunk count, chunk size)."""


class ResourceNotFoundError(KnowledgeError):
    """No resource exists at the requested id within the requested
    collection."""


class IndexingError(KnowledgeError):
    """The index could not be updated for a resource's chunks."""


class RetrievalError(KnowledgeError):
    """A retrieval request could not be completed."""


class SecretDetectedError(KnowledgeError):
    """Content or metadata looks like a secret. The caller must reject
    the resource outright — never store a redacted copy, never index it,
    never send it to an embedding provider."""


class KnowledgePolicyError(KnowledgeError):
    """An internal policy/permission-mapping invariant was violated."""
