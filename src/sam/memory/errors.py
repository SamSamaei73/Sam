"""Typed errors raised by the memory domain.

Named ``MemoryEngineError`` (not ``MemoryError``) deliberately: Python
already defines a built-in ``MemoryError`` (raised on allocation failure).
Reusing that name here would shadow the builtin, exactly the mistake
``sam.permissions.errors`` avoids with ``PermissionEngineError``.
"""


class MemoryEngineError(Exception):
    """Base class for all memory-domain failures."""


class InvalidMemoryCandidateError(MemoryEngineError):
    """A memory candidate or query failed domain validation."""


class MemoryNotFoundError(MemoryEngineError):
    """No memory exists for the given id, or it does not belong to the
    requesting principal — the two cases are deliberately indistinguishable
    to a caller, so a lookup can never be used to probe another principal's
    memory ids."""


class DuplicateMemoryError(MemoryEngineError):
    """A memory with the given identifier already exists."""


class MemoryStoreError(MemoryEngineError):
    """The memory store failed to complete an operation."""


class MemoryPermissionDeniedError(MemoryEngineError):
    """A configured ``PermissionChecker`` denied this operation."""


class WorkingMemoryFullError(MemoryEngineError):
    """A principal's working memory is at its bounded slot capacity."""
