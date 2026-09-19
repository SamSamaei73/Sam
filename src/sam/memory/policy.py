"""The deterministic Memory Policy.

Answers "should this candidate be persisted?" — never the LLM. The flow
this module implements:

    LLM / caller
       │
       ▼
    MemoryCandidate
       │
       ▼
    MemoryPolicy.decide()  →  STORE / REJECT / REQUIRES_REVIEW
       │
       ▼
    MemoryEngine  (only STORE ever reaches MemoryStore)

The LLM can *propose* a memory candidate; it cannot make that candidate
persist merely by proposing it, and it cannot choose its own outcome —
every field of the candidate that could plausibly be attacker/LLM
influenced (``content``, ``source``, ``confidence``, ``reason``) is only
ever read here to make the policy *more* conservative, never less.

Decision rules, in order:

1. A ``WORKING``-typed candidate is rejected outright — working memory
   never goes through this policy; it is written directly via
   ``MemoryEngine.remember_working`` / ``WorkingMemoryStore``, a distinct
   type that structurally cannot reach a long-term store.
2. Content that matches a known secret-like pattern
   (``sam.memory.sanitization.looks_like_secret``) is rejected. This is a
   best-effort heuristic — see that module's docstring for its limits —
   applied *before* any trust decision, so even an explicit, high-trust
   user statement containing something that looks like a credential is
   never persisted.
3. Content that is empty or trivially short after normalization is
   rejected — there is nothing meaningful to remember.
4. Only ``USER_EXPLICIT`` (the user directly asked Sam to remember this)
   and ``SYSTEM`` (Sam recording its own verified internal event, e.g. "a
   permission grant was approved") sources are eligible for automatic
   storage. Every other source — ordinary conversation, agent inference,
   a tool, an import — requires review: it is never silently promoted to
   permanent memory. This is the concrete mechanism behind "the LLM must
   not have unrestricted authority to permanently store anything" and
   "AGENT-generated memory must not automatically become an authoritative
   user preference".
5. Otherwise: STORE.
"""

from __future__ import annotations

from datetime import datetime

from sam.memory.models import (
    MemoryCandidate,
    MemorySource,
    MemoryType,
    PolicyDecision,
    PolicyOutcome,
    PolicyReason,
    utc_now,
)
from sam.memory.sanitization import looks_like_secret

_MIN_MEANINGFUL_CONTENT_LENGTH = 3

_AUTO_STORE_SOURCES = frozenset({MemorySource.USER_EXPLICIT, MemorySource.SYSTEM})


class MemoryPolicy:
    """A pure, deterministic policy — no I/O, no storage, no randomness."""

    def decide(
        self, candidate: MemoryCandidate, *, now: datetime | None = None
    ) -> PolicyDecision:
        """Evaluate one candidate. Never raises for a well-formed candidate
        (the candidate's own construction already validated its shape)."""

        decided_at = now if now is not None else utc_now()

        if candidate.memory_type is MemoryType.WORKING:
            return PolicyDecision(
                outcome=PolicyOutcome.REJECT,
                candidate=candidate,
                decided_at=decided_at,
                reason=PolicyReason.INVALID_CANDIDATE,
            )

        if looks_like_secret(candidate.content):
            return PolicyDecision(
                outcome=PolicyOutcome.REJECT,
                candidate=candidate,
                decided_at=decided_at,
                reason=PolicyReason.SECRET_LIKE_CONTENT,
            )

        if len(candidate.normalized_content()) < _MIN_MEANINGFUL_CONTENT_LENGTH:
            return PolicyDecision(
                outcome=PolicyOutcome.REJECT,
                candidate=candidate,
                decided_at=decided_at,
                reason=PolicyReason.CONTENT_TOO_SHORT,
            )

        if candidate.source not in _AUTO_STORE_SOURCES:
            return PolicyDecision(
                outcome=PolicyOutcome.REQUIRES_REVIEW,
                candidate=candidate,
                decided_at=decided_at,
                reason=PolicyReason.NOT_EXPLICITLY_CONFIRMED,
            )

        return PolicyDecision(
            outcome=PolicyOutcome.STORE, candidate=candidate, decided_at=decided_at
        )
