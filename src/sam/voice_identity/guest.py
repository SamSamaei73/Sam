"""Owner-authorized, temporary, restricted Guest Mode.

Guest Mode is OFF by default and can only be started with an ``OwnerProof``
(owner speaker match + fresh challenge in ONE utterance) plus a trusted
step-up. It is time-bounded (default 15 min, hard max 30), revocable at any
moment, default-deny, and conversation-only. A guest can never extend itself,
start another guest, or become the owner: those operations require an
``OwnerProof``, which a non-owner speaker can never produce.

The guest has its own principal and its own in-memory context. Nothing from a
guest session is written to Memory or Knowledge, the guest principal holds no
PermissionEngine grants (so every engine call it could make is default-deny),
and everything is discarded on expiry or revocation.
"""

from __future__ import annotations

import secrets
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from threading import RLock

from sam.permissions.models import Principal, PrincipalKind
from sam.voice_identity.audit import VoiceIdentityAuditSink
from sam.voice_identity.authority import (
    OwnerProof,
    OwnerProofAuthority,
    StepUpAuthority,
    StepUpGrant,
)
from sam.voice_identity.errors import GuestModeError
from sam.voice_identity.models import utc_now
from sam.voice_identity.policy import (
    GUEST_DEFAULT_MINUTES,
    GUEST_MAX_MINUTES,
    GUEST_MIN_MINUTES,
    MAX_GUEST_CONTEXT_TURNS,
)


class GuestCapability(StrEnum):
    CONVERSATION = "conversation"


# Everything a guest is explicitly NOT allowed to do (documentation + tests).
GUEST_DENIED = frozenset(
    {
        "personal_memory",
        "private_knowledge",
        "gmail",
        "calendar",
        "drive",
        "mcp_tools",
        "coding_agent",
        "computer_control",
        "repositories",
        "social_publishing",
        "permission_management",
        "settings",
        "credentials",
        "owner_history",
        "owner_biometric_settings",
        "send",
        "publish",
        "delete",
        "execute",
        "approve",
        "grant_permissions",
        "extend_session",
        "enable_another_guest",
        "change_providers",
        "reenroll_owner",
        "delete_owner_profile",
    }
)


@dataclass(frozen=True)
class GuestSession:
    guest_id: str
    principal: Principal
    started_at: datetime
    expires_at: datetime
    capabilities: frozenset[GuestCapability] = frozenset({GuestCapability.CONVERSATION})


@dataclass(frozen=True)
class GuestStatus:
    active: bool
    seconds_remaining: int = 0


class GuestSessionManager:
    def __init__(
        self,
        *,
        proofs: OwnerProofAuthority,
        step_up: StepUpAuthority,
        audit: VoiceIdentityAuditSink,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._proofs = proofs
        self._step_up = step_up
        self._audit = audit
        self._clock = clock
        self._session: GuestSession | None = None
        self._context: deque[tuple[str, str]] = deque(maxlen=MAX_GUEST_CONTEXT_TURNS)
        self._lock = RLock()

    def activate(
        self,
        proof: OwnerProof | None,
        grant: StepUpGrant | None,
        *,
        session_id: str,
        minutes: int = GUEST_DEFAULT_MINUTES,
    ) -> GuestSession:
        if (
            isinstance(minutes, bool)
            or not isinstance(minutes, int)
            or not GUEST_MIN_MINUTES <= minutes <= GUEST_MAX_MINUTES
        ):
            raise GuestModeError("guest duration is outside the allowed range")
        with self._lock:
            self._expire_locked()
            if self._session is not None:
                # No extension, no second guest: end the current one first.
                raise GuestModeError("guest mode is already active")
            self._proofs.consume(proof, session_id=session_id, purpose="start_guest")
            self._step_up.consume(grant, "start_guest")
            now = self._clock()
            guest_id = secrets.token_hex(8)
            session = GuestSession(
                guest_id=guest_id,
                principal=Principal(kind=PrincipalKind.USER, id=f"guest-{guest_id}"),
                started_at=now,
                expires_at=now + timedelta(minutes=minutes),
            )
            self._session = session
            self._context.clear()
        self._audit.record("guest_mode_started", "ok")
        return session

    def current(self) -> GuestSession | None:
        with self._lock:
            self._expire_locked()
            return self._session

    def status(self) -> GuestStatus:
        session = self.current()
        if session is None:
            return GuestStatus(False)
        remaining = int((session.expires_at - self._clock()).total_seconds())
        return GuestStatus(True, max(0, remaining))

    def revoke(self) -> bool:
        """Owner-initiated end. Always allowed and immediate."""

        with self._lock:
            had = self._session is not None
            self._destroy_locked()
        if had:
            self._audit.record("guest_mode_revoked", "ok")
        return had

    def remember_turn(self, role: str, text: str) -> None:
        """Session-local guest context; erased with the session."""

        with self._lock:
            self._expire_locked()
            if self._session is not None:
                self._context.append((role, text))

    def context_size(self) -> int:
        with self._lock:
            self._expire_locked()  # an expired guest's context must not linger
            return len(self._context)

    def _expire_locked(self) -> None:
        session = self._session
        if session is not None and self._clock() >= session.expires_at:
            self._destroy_locked()
            self._audit.record("guest_mode_expired", "ok")

    def _destroy_locked(self) -> None:
        self._session = None
        self._context.clear()


__all__ = [
    "GUEST_DENIED",
    "GuestCapability",
    "GuestSession",
    "GuestSessionManager",
    "GuestStatus",
]
