"""Short-lived, signed, one-time authority objects.

Two things gate the sensitive voice-identity operations, and neither is a
voice match by itself:

* ``StepUpGrant`` — proof the desktop's backend-verified step-up secret was
  just presented (the Phase 11 authentication boundary). Needed to enroll,
  re-enroll, and delete the owner voice profile.
* ``OwnerProof`` — proof that ONE utterance was BOTH a speaker match for the
  owner AND spoke a fresh random challenge. Needed (together with a trusted
  confirmation/authentication step) to start Guest Mode.

Both are HMAC-signed with a per-instance key that never leaves the instance,
expire quickly, and can be consumed once. They cannot be forged, edited,
replayed, or moved between sessions. Neither confers any PermissionEngine
decision.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from sam.voice_identity.errors import AuthorizationError
from sam.voice_identity.models import utc_now
from sam.voice_identity.policy import (
    MAX_TRACKED_PROOFS,
    OWNER_PROOF_TTL,
    STEP_UP_GRANT_TTL,
)

STEP_UP_PURPOSES = frozenset({"enroll", "re_enroll", "delete_profile", "start_guest"})
PROOF_PURPOSES = frozenset({"start_guest"})


@dataclass(frozen=True)
class StepUpGrant:
    grant_id: str
    purpose: str
    issued_at: datetime
    signature: str


@dataclass(frozen=True)
class OwnerProof:
    proof_id: str
    session_id: str
    audio_digest: str
    purpose: str
    issued_at: datetime
    signature: str


def _sign(key: bytes, *parts: str) -> str:
    return hmac.new(key, "\x1f".join(parts).encode(), hashlib.sha256).hexdigest()


class _OneTimeLedger:
    def __init__(self) -> None:
        self._used: OrderedDict[str, None] = OrderedDict()
        self._lock = RLock()

    def consume(self, identifier: str) -> bool:
        with self._lock:
            if identifier in self._used:
                return False
            self._used[identifier] = None
            while len(self._used) > MAX_TRACKED_PROOFS:
                self._used.popitem(last=False)
            return True


class StepUpAuthority:
    """Minted only by trusted backend code after the step-up secret verified."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._key = secrets.token_bytes(32)
        self._clock = clock
        self._ledger = _OneTimeLedger()

    def mint(self, purpose: str) -> StepUpGrant:
        if purpose not in STEP_UP_PURPOSES:
            raise AuthorizationError("unknown step-up purpose")
        now = self._clock()
        grant_id = secrets.token_hex(16)
        return StepUpGrant(
            grant_id, purpose, now, _sign(self._key, grant_id, purpose, now.isoformat())
        )

    def consume(self, grant: StepUpGrant | None, purpose: str) -> None:
        if grant is None:
            raise AuthorizationError("step-up authentication is required")
        expected = _sign(
            self._key, grant.grant_id, grant.purpose, grant.issued_at.isoformat()
        )
        if (
            not hmac.compare_digest(expected, grant.signature)
            or grant.purpose != purpose
        ):
            raise AuthorizationError("step-up authentication is invalid")
        if self._clock() - grant.issued_at > STEP_UP_GRANT_TTL:
            raise AuthorizationError("step-up authentication expired")
        if not self._ledger.consume(grant.grant_id):
            raise AuthorizationError("step-up authentication was already used")


class OwnerProofAuthority:
    """Minted only by the coordinator after speaker match + fresh challenge."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._key = secrets.token_bytes(32)
        self._clock = clock
        self._ledger = _OneTimeLedger()

    def mint(self, *, session_id: str, audio_digest: str, purpose: str) -> OwnerProof:
        if purpose not in PROOF_PURPOSES:
            raise AuthorizationError("unknown proof purpose")
        now = self._clock()
        proof_id = secrets.token_hex(16)
        signature = _sign(
            self._key, proof_id, session_id, audio_digest, purpose, now.isoformat()
        )
        return OwnerProof(proof_id, session_id, audio_digest, purpose, now, signature)

    def consume(
        self, proof: OwnerProof | None, *, session_id: str, purpose: str
    ) -> None:
        if proof is None:
            raise AuthorizationError("owner verification is required")
        expected = _sign(
            self._key,
            proof.proof_id,
            proof.session_id,
            proof.audio_digest,
            proof.purpose,
            proof.issued_at.isoformat(),
        )
        if not hmac.compare_digest(expected, proof.signature):
            raise AuthorizationError("owner verification is invalid")
        if proof.purpose != purpose or proof.session_id != session_id:
            raise AuthorizationError("owner verification does not apply here")
        if self._clock() - proof.issued_at > OWNER_PROOF_TTL:
            raise AuthorizationError("owner verification expired")
        if not self._ledger.consume(proof.proof_id):
            raise AuthorizationError("owner verification was already used")


__all__ = [
    "OwnerProof",
    "OwnerProofAuthority",
    "StepUpAuthority",
    "StepUpGrant",
]
