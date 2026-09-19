"""Typed errors raised by the permission domain.

Named ``PermissionEngineError`` (not ``PermissionError``) deliberately: the
Python standard library already defines a built-in ``PermissionError`` (an
``OSError`` subclass raised for OS-level access failures). Reusing that name
here would shadow the builtin and make ``except PermissionError:`` ambiguous
throughout the codebase.
"""


class PermissionEngineError(Exception):
    """Base class for all permission-domain failures."""


class InvalidPermissionRequestError(PermissionEngineError):
    """A permission request, grant, or scope failed domain validation."""


class GrantNotFoundError(PermissionEngineError):
    """No grant exists for the given identifier."""


class DuplicateGrantError(PermissionEngineError):
    """A grant with the given identifier already exists."""


class PermissionStoreError(PermissionEngineError):
    """The permission store failed to complete an operation."""


class ConfirmationError(PermissionEngineError):
    """Base class for confirmation-flow failures."""


class ConfirmationNotFoundError(ConfirmationError):
    """No confirmation exists for the given identifier."""


class ConfirmationInvalidError(ConfirmationError):
    """A confirmation could not be consumed for this exact request.

    Raised for: unapproved status, expiry, or a context mismatch (a
    different principal, action, resource, scope, or target than the one
    the confirmation was issued for). The caller must never treat this as
    authorization — ``PermissionEngine`` always converts it into a safe
    ``DENY`` rather than propagating it as an ``ALLOW``.
    """
