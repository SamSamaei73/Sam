"""Desktop-only ephemeral owner context; never an authentication cache.

The desktop verifies EVERY recording before constructing the contextual sink.
Only the gateway's eligible transcript reaches that sink. A conversation id is
just a lookup handle: no verification result, permission, grant, or biometric
is stored.
"""

from __future__ import annotations

import secrets
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import RLock, Timer
from typing import Protocol

from sam.agent.errors import AgentError
from sam.agent.models import (
    MAX_HISTORY_CHARS,
    MAX_HISTORY_MESSAGES,
    AgentRequest,
    AgentResponse,
    Message,
    MessageRole,
)
from sam.memory.sanitization import looks_like_secret
from sam.permissions.models import Principal
from sam.voice.models import utc_now

MAX_EXCHANGES = MAX_HISTORY_MESSAGES // 2
# The UI closes after its listening-idle limit. The server also removes text
# if a window crashes or never sends its close. This fallback allows a long
# reply to finish speaking; it is NOT a period of cached voice verification.
CONVERSATION_IDLE = timedelta(minutes=10)
CONVERSATION_MAX_AGE = timedelta(minutes=30)


class ConversationEnded(AgentError):
    error_code = "voice_conversation_ended"


class ConversationBusy(AgentError):
    error_code = "voice_conversation_busy"


@dataclass(repr=False)
class _Conversation:
    id: str
    principal: Principal
    started_at: datetime
    touched_at: datetime
    exchanges: list[tuple[str, str]] = field(default_factory=list)
    in_flight: str | None = None


@dataclass(frozen=True, repr=False)
class ConversationTurn:
    id: str
    generation: int
    turn: str
    history: tuple[Message, ...]


class VoiceConversationStore:
    """Process-memory text only, with principal binding and stale-work guards."""

    def __init__(
        self, *, clock: Callable[[], datetime] = utc_now, background_expiry: bool = True
    ) -> None:
        self._clock = clock
        self._background_expiry = background_expiry
        self._active: _Conversation | None = None
        self._generation = 0
        self._lock = RLock()
        self._timer: Timer | None = None

    def __del__(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def generation(self) -> int:
        """Capture before verification/STT, so a concurrent close wins."""
        with self._lock:
            self._expire_locked()
            return self._generation

    def begin(
        self, principal: Principal, conversation_id: str | None, generation: int
    ) -> ConversationTurn:
        with self._lock:
            self._expire_locked()
            if generation != self._generation:
                raise ConversationEnded("voice conversation ended")
            if self._active is not None and self._active.principal != principal:
                raise ConversationEnded("voice conversation ended")
            if conversation_id is None:
                self._clear_locked()
                now = self._clock()
                self._active = _Conversation(secrets.token_hex(16), principal, now, now)
            active = self._active
            if (
                active is None
                or active.principal != principal
                or (conversation_id is not None and conversation_id != active.id)
            ):
                raise ConversationEnded("voice conversation ended")
            if active.in_flight is not None:
                raise ConversationBusy("voice conversation is busy")
            active.in_flight = secrets.token_hex(8)
            active.touched_at = self._clock()
            self._arm_locked()
            history = tuple(
                Message(role=role, content=text)
                for user, assistant in active.exchanges
                for role, text in (
                    (MessageRole.USER, user),
                    (MessageRole.ASSISTANT, assistant),
                )
            )
            return ConversationTurn(
                active.id, self._generation, active.in_flight, history
            )

    def complete(
        self, principal: Principal, turn: ConversationTurn, user: str, assistant: str
    ) -> str | None:
        with self._lock:
            active = self._require_turn_locked(principal, turn)
            # Never retain credentials, even if a model produced one. Do not
            # redact and resend a changed version of a private conversation.
            if (
                not user.strip()
                or not assistant.strip()
                or looks_like_secret(user)
                or looks_like_secret(assistant)
                or (len(user) + len(assistant) > MAX_HISTORY_CHARS)
            ):
                self._clear_locked()
                return None
            active.exchanges.append((user, assistant))
            while (
                len(active.exchanges) > MAX_EXCHANGES
                or sum(len(a) + len(b) for a, b in active.exchanges) > MAX_HISTORY_CHARS
            ):
                active.exchanges.pop(0)
            active.in_flight = None
            active.touched_at = self._clock()
            self._arm_locked()
            return active.id

    def abandon(self, principal: Principal, turn: ConversationTurn) -> None:
        with self._lock:
            try:
                active = self._require_turn_locked(principal, turn)
            except ConversationEnded:
                return
            active.in_flight = None

    def end(self, principal: Principal, conversation_id: str | None = None) -> None:
        """Idempotent erasure. No id also cancels an in-flight first turn."""
        with self._lock:
            active = self._active
            if active is not None and active.principal != principal:
                return
            if conversation_id is not None and (
                active is None or active.id != conversation_id
            ):
                return
            self._clear_locked()

    def _require_turn_locked(
        self, principal: Principal, turn: ConversationTurn
    ) -> _Conversation:
        self._expire_locked()
        active = self._active
        if (
            active is None
            or self._generation != turn.generation
            or (
                active.id != turn.id
                or active.principal != principal
                or active.in_flight != turn.turn
            )
        ):
            raise ConversationEnded("voice conversation ended")
        return active

    def _expire_locked(self) -> None:
        active = self._active
        if active is not None and (
            self._clock() - active.touched_at >= CONVERSATION_IDLE
            or self._clock() - active.started_at >= CONVERSATION_MAX_AGE
        ):
            self._clear_locked()

    def _clear_locked(self) -> None:
        self._active = None
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _arm_locked(self) -> None:
        if not self._background_expiry or self._active is None:
            return
        if self._timer is not None:
            self._timer.cancel()
        deadline = min(
            self._active.touched_at + CONVERSATION_IDLE,
            self._active.started_at + CONVERSATION_MAX_AGE,
        )
        self._timer = Timer(
            max(0.01, (deadline - self._clock()).total_seconds()),
            _expire_store,
            (weakref.ref(self), self._generation),
        )
        self._timer.daemon = True
        self._timer.start()


def _expire_store(
    reference: weakref.ReferenceType[VoiceConversationStore], generation: int
) -> None:
    store = reference()
    if store is None:
        return
    with store._lock:
        if store._generation == generation:
            store._expire_locked()
            store._arm_locked()


class ConversationAgent(Protocol):
    def execute(
        self,
        request: AgentRequest,
        execution_id: str = "direct",
        *,
        response_language: str | None = None,
        history: Sequence[Message] = (),
    ) -> AgentResponse: ...


class ContextualVoiceAgent:
    """Trusted wrapper used ONLY after the current owner recording verifies."""

    def __init__(
        self,
        agent: ConversationAgent,
        store: VoiceConversationStore,
        principal: Principal,
        conversation_id: str | None,
        generation: int,
    ) -> None:
        self._agent = agent
        self._store = store
        self._principal = principal
        self._requested_id = conversation_id
        self._generation = generation
        self.conversation_id: str | None = None
        self.error_code: str | None = None

    def execute(
        self,
        request: AgentRequest,
        execution_id: str = "direct",
        *,
        response_language: str | None = None,
    ) -> AgentResponse:
        turn: ConversationTurn | None = None
        try:
            turn = self._store.begin(
                self._principal, self._requested_id, self._generation
            )
            if turn.history:
                response = self._agent.execute(
                    request,
                    execution_id,
                    response_language=response_language,
                    history=turn.history,
                )
            else:
                response = self._agent.execute(
                    request,
                    execution_id,
                    response_language=response_language,
                )
            self.conversation_id = self._store.complete(
                self._principal,
                turn,
                request.message,
                response.message,
            )
            return response
        except (ConversationEnded, ConversationBusy) as error:
            self.error_code = error.error_code
            raise
        except Exception:
            if turn is not None:
                self._store.abandon(self._principal, turn)
            raise
