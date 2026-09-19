"""Typed errors raised by the computer-control domain.

No name here shadows a Python builtin (compare ``PermissionEngineError``
in ``sam.permissions.errors`` and ``MemoryEngineError`` in
``sam.memory.errors``, both chosen for the same reason).
"""


class ComputerControlError(Exception):
    """Base class for all computer-control domain failures."""


class InvalidComputerActionError(ComputerControlError):
    """An action or sequence failed domain validation."""


class BackendError(ComputerControlError):
    """Base class for backend execution failures."""


class BackendUnavailableError(BackendError):
    """The backend cannot perform this operation in the current
    environment — see ``sam.computer.backend.UnimplementedComputerBackend``
    and ``docs/computer-control.md`` for why Phase 5 ships no real OS
    backend by default."""


class OutOfBoundsCoordinateError(ComputerControlError):
    """A coordinate falls outside the backend-reported screen bounds."""


class ActionSequenceTooLongError(InvalidComputerActionError):
    """A proposed action sequence exceeds the configured bound."""
