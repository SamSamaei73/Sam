"""Hands-free voice activation: the wake word ("Sam") and conversation stop
phrases. Pure, deterministic text logic plus a bounded rate limiter.

The audio side is deliberately elsewhere and deliberately small:

* the desktop app runs a local voice-activity detector and sends only short,
  speech-like segments (a few seconds at most) to the LOCAL backend;
* the backend transcribes such a segment with the LOCAL recognizer only and
  answers one bit ("was that Sam's name?"). The text is discarded at once: it
  is never returned, logged, audited, stored, or sent to any provider.

Nothing here decides authorization. Waking grants nothing: the first real
turn is still classified by owner voice identity, and every action is still
authorized by the PermissionEngine.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

WAKE_WORDS = frozenset({"sam", "سام"})
# Words people naturally put before a name ("hey Sam", "ok Sam").
_LEAD_INS = frozenset(
    {"hey", "hi", "hello", "ok", "okay", "yo", "oh", "ای", "هی", "سلام"}
)
# The wake word must be among the first few words: a name mentioned later in
# ordinary speech ("I told Sam...") is not a wake.
_WAKE_POSITION_LIMIT = 2
# More than this many words after the name means the wake segment already
# carries a request ("Sam, what's the time?").
_FOLLOW_MIN_WORDS = 2

# Whole-utterance stop phrases (after an optional leading "Sam"). Matching is
# exact on the normalized utterance, never a substring: "stop the timer" is a
# request, not a stop.
STOP_PHRASES = frozenset(
    {
        "stop",
        "stop listening",
        "go to sleep",
        "sleep",
        "thats all",
        "that is all",
        "thats it",
        "thank you thats all",
        "thanks thats all",
        "goodbye",
        "bye",
        "never mind",
        "بسه",
        "کافیه",
        "کافی است",
        "خداحافظ",
        "برو بخواب",
        "بخواب",
    }
)

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")

MAX_WAKE_SECONDS = 4.0
MIN_WAKE_SECONDS = 0.25


def normalize(text: str) -> list[str]:
    """Lowercase words without punctuation ("That's all." -> thats all)."""

    cleaned = _PUNCTUATION.sub("", text.replace("’", "'").replace("'", "").lower())
    return [w for w in _SPACES.split(cleaned.strip()) if w]


@dataclass(frozen=True)
class WakeMatch:
    wake: bool
    followed: bool = False


def detect_wake(text: str) -> WakeMatch:
    words = normalize(text)
    while words and words[0] in _LEAD_INS:
        words = words[1:]
    for index, word in enumerate(words[:_WAKE_POSITION_LIMIT]):
        if word in WAKE_WORDS:
            rest = words[index + 1 :]
            return WakeMatch(True, len(rest) >= _FOLLOW_MIN_WORDS)
    return WakeMatch(False)


def is_stop_phrase(text: str) -> bool:
    words = normalize(text)
    while words and (words[0] in WAKE_WORDS or words[0] in _LEAD_INS):
        words = words[1:]
    return bool(words) and " ".join(words) in STOP_PHRASES


class WakeRateLimiter:
    """At most ``limit`` wake checks per ``window`` seconds (room noise can
    never turn into an unbounded stream of recognizer runs)."""

    def __init__(
        self,
        limit: int = 30,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = self._clock()
        with self._lock:
            while self._times and now - self._times[0] > self._window:
                self._times.popleft()
            if len(self._times) >= self._limit:
                return False
            self._times.append(now)
            return True


__all__ = [
    "MAX_WAKE_SECONDS",
    "MIN_WAKE_SECONDS",
    "STOP_PHRASES",
    "WAKE_WORDS",
    "WakeMatch",
    "WakeRateLimiter",
    "detect_wake",
    "is_stop_phrase",
    "normalize",
]
