"""The final gate every proactive notification passes before it is stored.

Privacy is decided by PROVENANCE, never inferred from text. Every notification
draft carries a trusted privacy class derived from the task (owner-set), the
observer's registration and the observation itself (all set by Sam's code). The
gate joins it with what the text detectors find, taking the STRICTEST: text can
only ever raise the class (a detected secret makes it SECRET, raw private-looking
material makes it PRIVATE) and nothing (not a model, a provider, a task
instruction or observer text) can lower it.

Then, with a deterministic, Sam-owned policy:

* SECRET: nothing from the draft is stored. The notification is a fixed
  generic notice; no fragment of any value survives, and the model is never
  asked to redact its own leak;
* PRIVATE: no raw observation, provider or task text is copied. The notification
  is a fixed template naming only the trusted source label ("An update requiring
  your attention was detected in <source>. Open Automations in Sam for
  details."); the owner opens the authorized view for the details;
* PUBLIC / NORMAL / PERSONAL: the bounded, cleaned text is used;
* the reason and the suggested next action are closed enums; anything else
  becomes "none".

Pattern detection (secrets; e-mail headers or addresses, phone numbers, long
account/card/ID digit runs) is defence in depth ON TOP of provenance: it can
raise a class, never replace it. Nothing rejected is ever written to the audit,
the history or the activity log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from sam.models.models import PrivacyClass
from sam.proactive.models import (
    HARD_MAX_TITLE,
    NotificationReason,
    ProposedAction,
    clean_text,
)
from sam.proactive.policy import ProactiveLimits, is_secret

GENERIC_TITLE = "Automation update"
PRIVATE_TITLE = "Private automation update"
WITHHELD_SECRET = (
    "Sam found an update for this automation but withheld all of its details "
    "because they looked like a secret."
)
MAX_LABEL = 64
_SAFE_LABEL = "automation"


def private_summary(source_label: str) -> str:
    """The fixed PRIVATE template. Only a trusted, Sam-owned label is inserted."""

    return (
        f"An update requiring your attention was detected in {source_label}. "
        "Open Automations in Sam for details."
    )


_PRIVATE_RAW = re.compile(
    r"(?im)^\s*(?:from|to|cc|bcc|subject|reply-to|sent|date)\s*:"  # e-mail headers
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"  # an e-mail address
    r"|(?<![\w.+])\+\d[\d\s().-]{7,}\d(?![\w.])"  # an international number
    r"|(?<![\w.])\(?0\d{2,4}\)?[\s.-]?\d{3}[\s.-]?\d{3,4}(?![\w.])"  # a local number
    r"|(?<![\w.])\d{8,}(?![\w.])"  # account, card or ID numbers
)


class Withheld(StrEnum):
    NONE = "none"
    SECRET = "secret"
    PRIVATE = "private"


def looks_private_raw(text: str) -> bool:
    return bool(_PRIVATE_RAW.search(text))


def effective_privacy(trusted: PrivacyClass, *texts: str) -> PrivacyClass:
    """Privacy monotonicity: the strictest of the trusted provenance class and
    what the detectors find in the text. Text can raise it, never lower it."""

    detected = PrivacyClass.PUBLIC
    if is_secret(*texts):
        detected = PrivacyClass.SECRET
    elif any(looks_private_raw(t) for t in texts):
        detected = PrivacyClass.PRIVATE
    return max(trusted, detected)


@dataclass(frozen=True)
class SafeNotification:
    title: str
    summary: str
    source_label: str
    reason: NotificationReason
    proposed_action: ProposedAction
    withheld: Withheld
    privacy: PrivacyClass


def _enum[E: StrEnum](kind: type[E], value: object, default: E) -> E:
    if not isinstance(value, str):
        return default
    try:
        return kind(value)
    except ValueError:
        return default


def safe_notification(
    *,
    title: str,
    summary: str,
    source_label: str,
    reason: object,
    proposed_action: object,
    privacy: PrivacyClass,
    limits: ProactiveLimits,
) -> SafeNotification:
    """``privacy`` is the TRUSTED provenance class of everything in the draft."""

    title = clean_text(title, min(limits.max_title_chars, HARD_MAX_TITLE))
    label = clean_text(source_label, MAX_LABEL)
    summary = clean_text(summary, limits.max_summary_chars)
    if not label or effective_privacy(PrivacyClass.PUBLIC, label) > PrivacyClass.NORMAL:
        label = _SAFE_LABEL  # the label is Sam-owned; anything odd is replaced
    level = effective_privacy(privacy, title, summary)
    if level is PrivacyClass.SECRET:
        title, summary, withheld = GENERIC_TITLE, WITHHELD_SECRET, Withheld.SECRET
    elif level >= PrivacyClass.PRIVATE:
        title, summary, withheld = (
            PRIVATE_TITLE,
            private_summary(label),
            Withheld.PRIVATE,
        )
    else:
        withheld = Withheld.NONE
    return SafeNotification(
        title=title or GENERIC_TITLE,
        summary=summary,
        source_label=label,
        reason=_enum(NotificationReason, reason, NotificationReason.REMINDER_DUE),
        proposed_action=_enum(ProposedAction, proposed_action, ProposedAction.NONE),
        withheld=withheld,
        privacy=level,
    )


__all__ = [
    "GENERIC_TITLE",
    "PRIVATE_TITLE",
    "WITHHELD_SECRET",
    "SafeNotification",
    "Withheld",
    "effective_privacy",
    "looks_private_raw",
    "private_summary",
    "safe_notification",
]
