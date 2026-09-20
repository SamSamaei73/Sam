"""Bounded, in-memory voice sessions.

A session records only *identifiers*: who owns it, which utterance ids were
used, and whether it is open. It never stores audio, transcripts, identity
material, or any conversation history — so nothing can leak between
sessions and nothing is persisted. State is owned by the manager instance
(no globals) and lives only in process memory (a database/Redis-backed
manager is future work). There is no background processing: a session does
nothing on its own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock

from sam.permissions.models import Principal
from sam.voice.errors import DuplicateUtteranceError, VoiceLimitError, VoiceSessionError
from sam.voice.models import (
    MAX_OPEN_VOICE_SESSIONS,
    MAX_VOICE_SESSION_UTTERANCES,
    new_id,
    utc_now,
)


@dataclass
class _Session:
    session_id: str
    owner: Principal
    created_at: datetime
    utterances: dict[str, None] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceSessionInfo:
    session_id: str
    created_at: datetime
    utterance_count: int


class VoiceSessionManager:
    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self._sessions: dict[str, _Session] = {}
        self._clock = clock
        self._lock = RLock()

    def open(self, principal: Principal) -> str:
        with self._lock:
            if len(self._sessions) >= MAX_OPEN_VOICE_SESSIONS:
                raise VoiceLimitError("too many open voice sessions")
            session_id = new_id()
            self._sessions[session_id] = _Session(
                session_id=session_id, owner=principal, created_at=self._clock()
            )
            return session_id

    def _get(self, session_id: str, principal: Principal) -> _Session:
        session = self._sessions.get(session_id)
        # One generic error for unknown, closed, and someone-else's session,
        # so a principal cannot probe which sessions exist.
        if session is None or session.owner != principal:
            raise VoiceSessionError("voice session is not available")
        return session

    def require_open(self, session_id: str, principal: Principal) -> None:
        with self._lock:
            self._get(session_id, principal)

    def has_utterance(
        self, session_id: str, principal: Principal, utterance_id: str
    ) -> bool:
        with self._lock:
            return utterance_id in self._get(session_id, principal).utterances

    def reserve_utterance(
        self, session_id: str, principal: Principal, utterance_id: str
    ) -> None:
        """Atomically claim one utterance slot. Duplicate ids and the
        per-session bound are enforced here."""

        with self._lock:
            session = self._get(session_id, principal)
            if utterance_id in session.utterances:
                raise DuplicateUtteranceError("utterance id was already used")
            if len(session.utterances) >= MAX_VOICE_SESSION_UTTERANCES:
                raise VoiceLimitError("voice session utterance limit reached")
            session.utterances[utterance_id] = None

    def close(self, session_id: str, principal: Principal) -> None:
        with self._lock:
            self._get(session_id, principal)
            del self._sessions[session_id]

    def info(self, session_id: str, principal: Principal) -> VoiceSessionInfo:
        with self._lock:
            session = self._get(session_id, principal)
            return VoiceSessionInfo(
                session_id=session.session_id,
                created_at=session.created_at,
                utterance_count=len(session.utterances),
            )

    def open_count(self) -> int:
        with self._lock:
            return len(self._sessions)


__all__ = ["VoiceSessionInfo", "VoiceSessionManager"]
