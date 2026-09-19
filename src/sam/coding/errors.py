"""Typed errors raised by the coding-agent domain.

No name here shadows a Python builtin (compare ``PermissionEngineError``
in ``sam.permissions.errors``, ``MemoryEngineError`` in
``sam.memory.errors``: ``PermissionDeniedError`` is distinct from the
builtin ``PermissionError``).
"""


class CodingEngineError(Exception):
    """Base class for all coding-agent domain failures."""


class InvalidCodingRequestError(CodingEngineError):
    """A task, plan, or operation proposal failed domain validation."""


class RepositoryBoundaryError(CodingEngineError):
    """A path escapes the repository root, is malformed, or is otherwise
    structurally unsafe (traversal, null byte, absolute path, symlink
    escape, excessive length)."""


class UnsafeOperationError(CodingEngineError):
    """An operation is not in the closed, allowlisted operation/command
    set — never a shell string, never free-form arguments."""


class PermissionDeniedError(CodingEngineError):
    """The Permission Engine denied this operation, or required a
    confirmation that was not supplied/valid."""


class ProviderError(CodingEngineError):
    """A ``CodingProvider`` failed to produce a usable proposal."""


class VerificationError(CodingEngineError):
    """The verification pipeline could not be completed as requested."""


class CommandPolicyError(CodingEngineError):
    """A command could not be resolved or invoked safely — an unknown
    operation, an executable that could not be trusted, or a malformed
    internal command specification (never a caller-supplied string)."""


class CommandTimeoutError(CodingEngineError):
    """A command did not complete within its bounded timeout. The
    operation's execution outcome is FAILED — a timeout means there is
    no valid result to report, distinct from a command that completed
    but failed its own check (that is a successful *execution* carrying
    a failed *verification* status — see ``sam.coding.verifier``)."""


class ConcurrentModificationError(CodingEngineError):
    """A file changed between being read and being written — the
    optimistic-concurrency hash check failed. The write never proceeds."""


class RepositoryNotFoundError(CodingEngineError):
    """No file/path exists at the requested location within the
    repository."""
