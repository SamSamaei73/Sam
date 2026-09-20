"""Random, short-lived, one-time spoken challenges (anti-replay).

A Sam-generated phrase of random digits plus a random colour word, shown on
screen for the owner to speak. It is unpredictable before issuance, bound to
one session, expires quickly, and is consumed on its FIRST evaluation whether
or not it passed (so a wrong attempt cannot be retried against the same
challenge). Evaluation compares a normalized transcript, in English or
Persian (digits in any script, spelled digits, and colour words).

Limitation: challenge-response defeats replay of a *previous* recording. It
does not stop a real-time voice clone or a sophisticated synthetic speaker.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from sam.voice_identity.errors import ChallengeError
from sam.voice_identity.models import utc_now
from sam.voice_identity.policy import CHALLENGE_TTL, MAX_TRACKED_CHALLENGES

DIGIT_COUNT = 4

# (id, English, Persian)
_WORDS: tuple[tuple[str, str, str], ...] = (
    ("blue", "blue", "آبی"),
    ("green", "green", "سبز"),
    ("red", "red", "قرمز"),
    ("white", "white", "سفید"),
    ("black", "black", "سیاه"),
    ("yellow", "yellow", "زرد"),
    ("orange", "orange", "نارنجی"),
    ("gold", "gold", "طلایی"),
)
_EN_DIGITS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}  # fmt: skip
_FA_DIGITS = {
    "صفر": "0", "یک": "1", "دو": "2", "سه": "3", "چهار": "4",
    "پنج": "5", "شش": "6", "هفت": "7", "هشت": "8", "نه": "9",
}  # fmt: skip
_WORD_LOOKUP = {w: wid for wid, en, fa in _WORDS for w in (en, fa)}
_DIGIT_LOOKUP = {**_EN_DIGITS, **_FA_DIGITS}
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("ي", "ی").replace("ك", "ک").replace("‌", "")
    text = "".join(c for c in text if not unicodedata.category(c).startswith("M"))
    return text.lower()


def extract_digits_and_words(transcript: str) -> tuple[str, frozenset[str]]:
    """Digits in spoken order (any script/spelling) and challenge-word ids."""

    digits: list[str] = []
    words: set[str] = set()
    for token in _TOKEN.findall(_normalize_text(transcript)):
        if token.isdigit():
            digits.extend(str(unicodedata.digit(ch)) for ch in token)
        elif token in _DIGIT_LOOKUP:
            digits.append(_DIGIT_LOOKUP[token])
        elif token in _WORD_LOOKUP:
            words.add(_WORD_LOOKUP[token])
    return "".join(digits), frozenset(words)


@dataclass(frozen=True)
class Challenge:
    challenge_id: str
    session_id: str
    digits: str
    word_id: str
    issued_at: datetime
    expires_at: datetime

    def display(self, language: str = "en") -> str:
        word = next(
            fa if language == "fa" else en
            for wid, en, fa in _WORDS
            if wid == self.word_id
        )
        prompt = "بگویید: " if language == "fa" else "Please say: "
        return prompt + " ".join(self.digits) + " " + word


class ChallengeService:
    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._pending: OrderedDict[str, Challenge] = OrderedDict()
        self._lock = RLock()

    def issue(self, session_id: str) -> Challenge:
        now = self._clock()
        challenge = Challenge(
            challenge_id=secrets.token_hex(16),
            session_id=session_id,
            digits="".join(str(secrets.randbelow(10)) for _ in range(DIGIT_COUNT)),
            word_id=secrets.choice(_WORDS)[0],
            issued_at=now,
            expires_at=now + CHALLENGE_TTL,
        )
        with self._lock:
            self._pending[challenge.challenge_id] = challenge
            while len(self._pending) > MAX_TRACKED_CHALLENGES:
                self._pending.popitem(last=False)
        return challenge

    def evaluate(self, challenge_id: str, session_id: str, transcript: str) -> bool:
        """Consume the challenge (pass or fail). ``True`` only if it exists, is
        unexpired, belongs to ``session_id``, and the transcript contains the
        exact digit sequence and the challenge word."""

        with self._lock:
            challenge = self._pending.pop(challenge_id, None)
        if challenge is None:
            raise ChallengeError("challenge is invalid")
        if challenge.session_id != session_id or self._clock() > challenge.expires_at:
            raise ChallengeError("challenge is invalid")
        digits, words = extract_digits_and_words(transcript)
        return digits == challenge.digits and challenge.word_id in words

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


__all__ = ["Challenge", "ChallengeService", "extract_digits_and_words"]
