"""Permission grant storage abstraction.

``PermissionStore`` is deliberately dumb: it persists and queries grants by
identity/resource/action, but it never decides whether a grant *authorizes*
a request — that judgment (status, expiry, scope containment, precedence
among multiple matches) lives entirely in ``sam.permissions.engine`` so the
authorization logic stays in one auditable place.

``InMemoryPermissionStore`` is a Phase 3 implementation only: no database.
Each instance owns its own state (a plain dict behind a lock) — there is no
module-level singleton, so tests and future request-scoped composition get
fully isolated stores by simply constructing a new instance.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from threading import RLock
from typing import Protocol

from sam.permissions.errors import DuplicateGrantError, GrantNotFoundError
from sam.permissions.models import (
    GrantStatus,
    PermissionAction,
    PermissionGrant,
    PermissionResource,
    Principal,
)


class PermissionStore(Protocol):
    """Persistence contract the engine depends on."""

    def create_grant(self, grant: PermissionGrant) -> PermissionGrant:
        """Persist a new grant. Raises ``DuplicateGrantError`` on id reuse."""

    def get_grant(self, grant_id: str) -> PermissionGrant | None:
        """Return the grant by id, or ``None`` if it does not exist."""

    def list_grants(
        self,
        principal: Principal,
        *,
        resource: PermissionResource | None = None,
        action: PermissionAction | None = None,
    ) -> Sequence[PermissionGrant]:
        """List grants for a principal, optionally narrowed further.

        Returns grants in every status (active and revoked) — callers that
        need only currently-usable grants must filter with
        ``PermissionGrant.is_usable``.
        """

    def revoke_grant(self, grant_id: str, *, now: datetime) -> PermissionGrant:
        """Mark a grant revoked. Raises ``GrantNotFoundError`` if unknown.

        Revocation replaces the stored record with an updated, still
        immutable one (``status=REVOKED``, ``revoked_at=now``) rather than
        deleting it, preserving the grant for audit history.
        """


class InMemoryPermissionStore:
    """A process-local, lock-protected grant store for Phase 3.

    Not a persistence layer for production use — a real implementation
    (backed by PostgreSQL, per PROJECT_SPEC.json) is future-phase scope.
    This exists so the engine and its tests have a deterministic,
    dependency-free implementation to run against today.
    """

    def __init__(self) -> None:
        self._grants: dict[str, PermissionGrant] = {}
        self._lock = RLock()

    def create_grant(self, grant: PermissionGrant) -> PermissionGrant:
        with self._lock:
            if grant.grant_id in self._grants:
                raise DuplicateGrantError(
                    f"a grant with id {grant.grant_id!r} already exists"
                )
            self._grants[grant.grant_id] = grant
            return grant

    def get_grant(self, grant_id: str) -> PermissionGrant | None:
        with self._lock:
            return self._grants.get(grant_id)

    def list_grants(
        self,
        principal: Principal,
        *,
        resource: PermissionResource | None = None,
        action: PermissionAction | None = None,
    ) -> Sequence[PermissionGrant]:
        with self._lock:
            return tuple(
                grant
                for grant in self._grants.values()
                if grant.principal == principal
                and (resource is None or grant.resource is resource)
                and (action is None or grant.action is action)
            )

    def revoke_grant(self, grant_id: str, *, now: datetime) -> PermissionGrant:
        with self._lock:
            existing = self._grants.get(grant_id)
            if existing is None:
                raise GrantNotFoundError(f"no grant with id {grant_id!r}")
            revoked = existing.model_copy(
                update={
                    "status": GrantStatus.REVOKED,
                    "revoked_at": now,
                    "updated_at": now,
                }
            )
            self._grants[grant_id] = revoked
            return revoked
