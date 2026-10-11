"""Trusted Whisper language metadata, independent of model and user output.

Codes match faster-whisper 1.2.1's tokenizer and OpenAI Whisper's catalog:
https://github.com/openai/whisper/blob/main/whisper/tokenizer.py
The installed model determines which of these languages it can recognize.
Names used in system instructions come only from this reviewed table.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import AfterValidator

LANGUAGE_NAMES = MappingProxyType(
    {
        "af": "Afrikaans",
        "am": "Amharic",
        "ar": "Arabic",
        "as": "Assamese",
        "az": "Azerbaijani",
        "ba": "Bashkir",
        "be": "Belarusian",
        "bg": "Bulgarian",
        "bn": "Bengali",
        "bo": "Tibetan",
        "br": "Breton",
        "bs": "Bosnian",
        "ca": "Catalan",
        "cs": "Czech",
        "cy": "Welsh",
        "da": "Danish",
        "de": "German",
        "el": "Greek",
        "en": "English",
        "es": "Spanish",
        "et": "Estonian",
        "eu": "Basque",
        "fa": "Persian",
        "fi": "Finnish",
        "fo": "Faroese",
        "fr": "French",
        "gl": "Galician",
        "gu": "Gujarati",
        "ha": "Hausa",
        "haw": "Hawaiian",
        "he": "Hebrew",
        "hi": "Hindi",
        "hr": "Croatian",
        "ht": "Haitian Creole",
        "hu": "Hungarian",
        "hy": "Armenian",
        "id": "Indonesian",
        "is": "Icelandic",
        "it": "Italian",
        "ja": "Japanese",
        "jw": "Javanese",
        "ka": "Georgian",
        "kk": "Kazakh",
        "km": "Khmer",
        "kn": "Kannada",
        "ko": "Korean",
        "la": "Latin",
        "lb": "Luxembourgish",
        "ln": "Lingala",
        "lo": "Lao",
        "lt": "Lithuanian",
        "lv": "Latvian",
        "mg": "Malagasy",
        "mi": "Maori",
        "mk": "Macedonian",
        "ml": "Malayalam",
        "mn": "Mongolian",
        "mr": "Marathi",
        "ms": "Malay",
        "mt": "Maltese",
        "my": "Burmese",
        "ne": "Nepali",
        "nl": "Dutch",
        "nn": "Nynorsk",
        "no": "Norwegian",
        "oc": "Occitan",
        "pa": "Punjabi",
        "pl": "Polish",
        "ps": "Pashto",
        "pt": "Portuguese",
        "ro": "Romanian",
        "ru": "Russian",
        "sa": "Sanskrit",
        "sd": "Sindhi",
        "si": "Sinhala",
        "sk": "Slovak",
        "sl": "Slovenian",
        "sn": "Shona",
        "so": "Somali",
        "sq": "Albanian",
        "sr": "Serbian",
        "su": "Sundanese",
        "sv": "Swedish",
        "sw": "Swahili",
        "ta": "Tamil",
        "te": "Telugu",
        "tg": "Tajik",
        "th": "Thai",
        "tk": "Turkmen",
        "tl": "Tagalog",
        "tr": "Turkish",
        "tt": "Tatar",
        "uk": "Ukrainian",
        "ur": "Urdu",
        "uz": "Uzbek",
        "vi": "Vietnamese",
        "yi": "Yiddish",
        "yo": "Yoruba",
        "zh": "Chinese",
        "yue": "Cantonese",
    }
)
SUPPORTED_LANGUAGE_CODES = frozenset(LANGUAGE_NAMES)
RTL_LANGUAGE_CODES = frozenset({"ar", "fa", "he", "ps", "sd", "ur", "yi"})
Direction = Literal["rtl", "ltr"]
_TAG = re.compile(r"^[a-z]{2,3}(?:[-_][a-z0-9]{2,8})*$")
_ALIASES = {
    **{name.lower(): code for code, name in LANGUAGE_NAMES.items()},
    "eng": "en",
    "fas": "fa",
    "per": "fa",
    "farsi": "fa",
    "iw": "he",
    "jv": "jw",
    "fil": "tl",
    "myanmar": "my",
    "mandarin": "zh",
    "castilian": "es",
    "pushto": "ps",
}


def normalize_language_tag(tag: str | None) -> str | None:
    """Canonicalize bounded, known tags; unknown text never enters a prompt."""

    if not isinstance(tag, str) or not 1 <= len(tag) <= 32:
        return None
    value = tag.lower()
    if value in _ALIASES:
        return _ALIASES[value]
    if not _TAG.fullmatch(value):
        return None
    primary = value.replace("_", "-").split("-", 1)[0]
    canonical = _ALIASES.get(primary, primary)
    return canonical if canonical in SUPPORTED_LANGUAGE_CODES else None


def validate_language_code(value: str) -> str:
    """Validate an already canonical wire/model language code."""

    if value not in SUPPORTED_LANGUAGE_CODES:
        raise ValueError("language is not supported")
    return value


LanguageCode = Annotated[str, AfterValidator(validate_language_code)]


def language_direction(code: str) -> Direction:
    validate_language_code(code)
    return "rtl" if code in RTL_LANGUAGE_CODES else "ltr"


__all__ = [
    "Direction",
    "LANGUAGE_NAMES",
    "LanguageCode",
    "RTL_LANGUAGE_CODES",
    "SUPPORTED_LANGUAGE_CODES",
    "language_direction",
    "normalize_language_tag",
    "validate_language_code",
]
