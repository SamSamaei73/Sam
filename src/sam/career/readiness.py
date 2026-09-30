"""The gate in front of any REAL external Career action.

Durable persistence alone never enables a real SUBMIT or SEND adapter. A
consequential action is even considered only when EVERY condition holds:

* ``durable_store_ready``: the attempt ledger is on disk (not in memory);
* ``migrations_ready``: the schema is current and startup recovery ran;
* ``idempotency_ready``: the store enforces one blocking attempt per item;
* ``reconciliation_ready``: the adapter can look an attempt up (read-only);
* ``destination_validation_ready``: destination / redirect checks are active;
* ``adapter_configured``: a trusted, reviewed adapter is configured;
* ``permission_ready``: the owner has granted the action (a grant exists).

Even then, every action still needs its own permission check and a fresh,
manifest-bound confirmation. No probe means NOT ready (fail closed).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields

from sam.career.attempts import ExternalAction


@dataclass(frozen=True)
class ExternalActionReadiness:
    durable_store_ready: bool
    migrations_ready: bool
    idempotency_ready: bool
    reconciliation_ready: bool
    destination_validation_ready: bool
    adapter_configured: bool
    permission_ready: bool

    @property
    def ready(self) -> bool:
        return all(getattr(self, f.name) for f in fields(self))

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(f.name for f in fields(self) if not getattr(self, f.name))

    @classmethod
    def not_ready(cls, *, adapter_configured: bool = False) -> ExternalActionReadiness:
        return cls(
            durable_store_ready=False,
            migrations_ready=False,
            idempotency_ready=False,
            reconciliation_ready=False,
            destination_validation_ready=True,
            adapter_configured=adapter_configured,
            permission_ready=False,
        )


# (action, is an adapter configured?) -> readiness
ReadinessProbe = Callable[[ExternalAction, bool], ExternalActionReadiness]


__all__ = ["ExternalActionReadiness", "ReadinessProbe"]
