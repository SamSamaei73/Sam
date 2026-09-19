"""Sam's Computer Control foundation (Phase 5).

Provides a secure, modular abstraction for screen inspection and bounded
mouse/keyboard interaction — never an unrestricted autonomous computer
agent. Every action passes through ``sam.permissions.engine.PermissionEngine``
(reused, not duplicated) before ``sam.computer.controller.ComputerController``
ever calls a ``sam.computer.backend.ComputerBackend`` method. No real OS
backend ships in Phase 5 — see ``docs/computer-control.md`` for why and
what a future one only has to implement.

No shell, terminal, filesystem, browser, or arbitrary-command execution
capability exists anywhere in this package.
"""
