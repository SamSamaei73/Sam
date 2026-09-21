"""Cost, privacy and personal-content policies.

Three separate concerns, deliberately not merged:

* ``CostPolicy``: what may ever be billed. ``PAID_FALLBACK = OFF``, always.
* ``PrivacyPolicy``: where content may be SENT, from trusted provenance.
* ``PersonalContentPolicy``: what topics Sam itself refuses (default: none).

None of them is authorization. A permissive content policy or a privacy
preference never grants a permission, approves a confirmation, or weakens the
PermissionEngine, Guest Mode, step-up authentication, or any tool policy.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from sam.memory.sanitization import looks_like_secret
from sam.models.models import (
    BillingMode,
    ClaudeImprovementState,
    GeminiFreeAttestation,
    PrivacyClass,
    ProviderId,
    ReasonCode,
)
from sam.models.registry import ProviderDescriptor

# Owner voice-identity / biometric / auth-secret material is SECRET no matter
# which provider is asked: an assignment-shaped value or an embedding-like run of
# decimals. (Prose that merely discusses these topics is not blocked.)
_RESTRICTED = re.compile(
    r"(?ix)\b(?:biometric|speaker|voice)[\s_-]*(?:embeddings?|templates?)\b"
    r"[\s\"']*(?:is|[:=])"
    r"|\bowner[\s_-]*proof\b[\s\"']*[:=]"
    r"|\bstep[\s_-]?up[\s_-]*secret\b[\s\"']*[:=]"
    r"|\bbridge[\s_-]*(?:token|secret)\b[\s\"']*[:=]"
    r"|(?:-?\d+\.\d+\s*[,;\s]\s*){7,}-?\d+\.\d+"
)

_ALLOWED_BILLING = frozenset(
    {BillingMode.SUBSCRIPTION_INCLUDED, BillingMode.FREE_TIER, BillingMode.LOCAL}
)


@dataclass(frozen=True)
class CostPolicy:
    """Every switch is fixed False. There is no code path that turns one on:
    a prompt, model output, provider failure or quota error cannot, and the
    constructor refuses a True value."""

    allow_paid_api: bool = False
    allow_paid_fallback: bool = False
    allow_auto_upgrade: bool = False
    allow_credit_purchase: bool = False

    def __post_init__(self) -> None:
        if (
            self.allow_paid_api
            or self.allow_paid_fallback
            or self.allow_auto_upgrade
            or self.allow_credit_purchase
        ):
            raise ValueError("paid usage cannot be enabled")

    def allows(self, billing_mode: BillingMode) -> bool:
        return billing_mode in _ALLOWED_BILLING


@dataclass(frozen=True)
class OwnerPreferences:
    """Trusted owner settings. Changed only through the Desktop settings route
    (step-up authenticated when they loosen privacy), never from a prompt."""

    preferred_provider: ProviderId | None = None
    allow_free_fallback: bool = True
    personal_to_free_tier: bool = False
    private_to_free_tier: bool = False
    claude_improvement_state: ClaudeImprovementState = ClaudeImprovementState.UNKNOWN
    private_to_claude_when_improvement_enabled: bool = False
    # Session-only: resets to UNKNOWN on restart (Gemini stays unavailable until
    # the owner attests again).
    gemini_attestation: GeminiFreeAttestation = GeminiFreeAttestation.UNKNOWN


class PrivacyPolicy:
    """Classifies by provenance and decides where a class may be sent."""

    def classify(
        self, texts: Iterable[str], declared: PrivacyClass = PrivacyClass.NORMAL
    ) -> PrivacyClass:
        """SECRET if any text looks like a credential (Sam's existing detector);
        otherwise the trusted, declared class. Topic inference is never used."""

        for text in texts:
            if looks_like_secret(text) or _RESTRICTED.search(text):
                return PrivacyClass.SECRET
        return declared

    def permits(
        self,
        privacy: PrivacyClass,
        descriptor: ProviderDescriptor,
        prefs: OwnerPreferences,
    ) -> ReasonCode | None:
        """``None`` if allowed, else the reason it is not."""

        if privacy is PrivacyClass.SECRET and descriptor.external_processing:
            return ReasonCode.SECRET_BLOCKED
        if not descriptor.external_processing:
            return None
        if privacy in (PrivacyClass.PUBLIC, PrivacyClass.NORMAL):
            return None
        if descriptor.free_tier_data_use:
            allowed = (
                prefs.personal_to_free_tier
                if privacy is PrivacyClass.PERSONAL
                else prefs.private_to_free_tier
            )
            return None if allowed else ReasonCode.PRIVACY_RESTRICTED
        if (
            privacy is PrivacyClass.PRIVATE
            and descriptor.provider_id is ProviderId.CLAUDE_SUBSCRIPTION
            and prefs.claude_improvement_state
            is ClaudeImprovementState.OWNER_REPORTS_ENABLED
            and not prefs.private_to_claude_when_improvement_enabled
        ):
            return ReasonCode.PRIVACY_RESTRICTED
        return None


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


@dataclass(frozen=True)
class PersonalContentPolicy:
    """Sam adds NO topic censorship of its own: ``topic_blocklist`` is empty by
    default and only the owner (through trusted settings) can add entries.

    This is not a way around any provider's safety policy and does nothing to
    it: a provider refusal is normalized and respected, never worked around.
    """

    mode: str = "permissive"
    topic_blocklist: tuple[str, ...] = field(default_factory=tuple)
    follow_user_tone: bool = True
    # Model routing and Memory persistence are separate decisions.
    private_content_auto_memory: bool = False

    def __post_init__(self) -> None:
        if len(self.topic_blocklist) > 64:
            raise ValueError("too many blocklist entries")
        for entry in self.topic_blocklist:
            if (
                not entry.strip()
                or len(entry) > 80
                or any(unicodedata.category(c).startswith("C") for c in entry)
            ):
                raise ValueError("blocklist entry is malformed")
        if self.private_content_auto_memory:
            raise ValueError("private content is never auto-saved to Memory")

    def blocked_by_owner(self, texts: Iterable[str]) -> bool:
        """Only the OWNER's own entries can block; nothing else ever does."""

        if not self.topic_blocklist:
            return False
        entries = [_normalize(e).strip() for e in self.topic_blocklist]
        haystack = _normalize("\n".join(texts))
        return any(entry and entry in haystack for entry in entries)


__all__ = [
    "CostPolicy",
    "OwnerPreferences",
    "PersonalContentPolicy",
    "PrivacyPolicy",
]
