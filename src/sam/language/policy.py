"""Explicit language policy.

Persian is a first-class Sam language. Rather than hoping a model happens to
answer in the right language, Sam derives the response language itself and
gives the model a fixed, reviewed instruction. Language state is presentation
and instruction only: it is NEVER authorization, and it can never change a
permission, a confirmation, a principal or a scope.

    input text / STT language tag
            -> LanguagePolicy.resolve(preference, ...)
            -> LanguageDecision (response language, direction, TTS language)
            -> AgentRequest.response_language -> a trusted system instruction

Detection is script-based (Persian vs Latin letters). It is deliberately
simple and deterministic; a speech recognizer's own language tag is preferred
when available.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

Direction = Literal["rtl", "ltr"]
ResponseLanguage = Literal["fa", "en"]


class LanguagePreference(StrEnum):
    AUTO = "auto"
    FA = "fa"
    EN = "en"


# Reviewed static instructions (trusted; never built from user/model text).
INSTRUCTIONS: dict[str, str] = {
    "fa": (
        "Respond in Persian (Farsi) using Persian script. Keep technical terms, "
        "code, URLs and product names (for example API, Docker, FastAPI, GitHub, "
        "Claude, MCP, Python) in their original Latin form inside Persian "
        "sentences. Do not transliterate Persian into Latin letters."
    ),
    "en": (
        "Respond in English. If the user includes Persian words, keep answering "
        "in English unless they ask for Persian."
    ),
}


def _is_persian_letter(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x0600 <= cp <= 0x06FF
        or 0x0750 <= cp <= 0x077F
        or 0xFB50 <= cp <= 0xFDFF
        or 0xFE70 <= cp <= 0xFEFF
    ) and unicodedata.category(ch).startswith("L")


def _is_latin_letter(ch: str) -> bool:
    return ch.isascii() and ch.isalpha()


def persian_share(text: str) -> float:
    """Fraction of Persian-script letters among Persian + Latin letters."""

    persian = sum(1 for c in text if _is_persian_letter(c))
    latin = sum(1 for c in text if _is_latin_letter(c))
    total = persian + latin
    return 0.0 if total == 0 else persian / total


def _word_language(word: str) -> ResponseLanguage | None:
    persian = sum(1 for c in word if _is_persian_letter(c))
    latin = sum(1 for c in word if _is_latin_letter(c))
    if persian == 0 and latin == 0:
        return None
    return "fa" if persian >= latin else "en"


def detect_language(text: str) -> ResponseLanguage | None:
    """The language carrying the sentence, by word count.

    Persian words outnumber Latin words -> ``fa`` (so a Persian sentence with
    English technical terms such as "API" or "Docker" is Persian). Latin words
    outnumber Persian -> ``en``. A tie follows the first word. ``None`` if
    there are no letters at all (digits/emoji only)."""

    words = [w for w in (_word_language(t) for t in text.split()) if w is not None]
    if not words:
        return None
    fa, en = words.count("fa"), words.count("en")
    if fa == en:
        return words[0]
    return "fa" if fa > en else "en"


@dataclass(frozen=True)
class LanguageDecision:
    user_language: ResponseLanguage | None
    response_language: ResponseLanguage
    direction: Direction
    tts_language: ResponseLanguage
    mixed: bool = False

    @property
    def instruction(self) -> str:
        return INSTRUCTIONS[self.response_language]


class LanguagePolicy:
    def __init__(self, default: ResponseLanguage = "en") -> None:
        self._default = default

    def resolve(
        self,
        preference: LanguagePreference | str = LanguagePreference.AUTO,
        text: str = "",
        *,
        stt_language: str | None = None,
    ) -> LanguageDecision:
        pref = LanguagePreference(preference)
        heard = self._from_tag(stt_language) or detect_language(text)
        if pref is LanguagePreference.FA:
            response: ResponseLanguage = "fa"
        elif pref is LanguagePreference.EN:
            response = "en"
        else:  # follow the speaker
            response = heard or self._default
        share = persian_share(text)
        mixed = 0.0 < share < 1.0 and any(_is_latin_letter(c) for c in text)
        return LanguageDecision(
            user_language=heard,
            response_language=response,
            direction="rtl" if response == "fa" else "ltr",
            tts_language=response,
            mixed=mixed,
        )

    @staticmethod
    def _from_tag(tag: str | None) -> ResponseLanguage | None:
        if not tag:
            return None
        primary = tag.lower().replace("_", "-").split("-")[0]
        if primary in {"fa", "fas", "per", "persian", "farsi"}:
            return "fa"
        if primary in {"en", "eng", "english"}:
            return "en"
        return None


__all__ = [
    "INSTRUCTIONS",
    "Direction",
    "LanguageDecision",
    "LanguagePolicy",
    "LanguagePreference",
    "ResponseLanguage",
    "detect_language",
    "persian_share",
]
