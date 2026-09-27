"""Identifier scrubbing and sensitivity cues.

Two protections that run BEFORE anything is stored or sent anywhere:

* administrative identifiers (a student number, an account number, a national
  id, an email address, a phone number) are removed from every reference and
  attribute, and a line that carries one is dropped entirely, so a transcript
  never leaks the owner's student id into a claim, an audit event or a prompt;
* text about salary, visa/work authorization or security clearance is raised to
  PRIVATE, so the Phase 13 router never sends it to Gemini Free by default.

Secret detection is Knowledge's (``sam.knowledge.metadata``), reused rather than
duplicated; this package never imports ``sam.memory`` directly.
"""

from __future__ import annotations

import re

from sam.knowledge.metadata import is_restricted_content

_IDENTIFIER_LABEL = re.compile(
    r"(?i)\b(?:student|candidate|matriculation|registration|account|passport|"
    r"national\s+insurance|ssn|nino|customer|applicant|user)\s*"
    r"(?:id|no\.?|number|#|ref(?:erence)?)\b"
)
# The label AND the value that follows it: "Student ID: A1234567" removes both.
_IDENTIFIER_VALUE = re.compile(
    _IDENTIFIER_LABEL.pattern + r"\s*[:#-]?\s*[A-Za-z0-9][A-Za-z0-9-]{2,}",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)")
_LONG_DIGITS = re.compile(r"\b\d{7,}\b")
_PRIVATE_CUES = re.compile(
    r"(?i)\b(?:salary|salaries|compensation|expected\s+pay|day\s+rate|"
    r"visa|work\s+permit|work\s+authori[sz]ation|right\s+to\s+work|"
    r"sponsorship|security\s+clearance|clearance|sc\s+cleared|dv\s+cleared)\b"
)

REMOVED = "[removed]"


def is_identifier_line(line: str) -> bool:
    """True if the line labels an administrative identifier (``Student ID: ...``)."""

    return _IDENTIFIER_LABEL.search(line) is not None


def scrub(text: str) -> str:
    """Remove identifiers and contact details from ``text``."""

    cleaned = _IDENTIFIER_VALUE.sub(REMOVED, text)
    cleaned = _IDENTIFIER_LABEL.sub(REMOVED, cleaned)
    cleaned = _EMAIL.sub(REMOVED, cleaned)
    cleaned = _PHONE.sub(REMOVED, cleaned)
    return _LONG_DIGITS.sub(REMOVED, cleaned)


def has_private_cue(text: str) -> bool:
    return _PRIVATE_CUES.search(text) is not None


def contains_secret(text: str) -> bool:
    """Knowledge's secret detector: API keys, passwords, private keys, tokens,
    owner proof, step-up secrets, biometric/speaker material."""

    return is_restricted_content(text)


__all__ = [
    "REMOVED",
    "contains_secret",
    "has_private_cue",
    "is_identifier_line",
    "scrub",
]
