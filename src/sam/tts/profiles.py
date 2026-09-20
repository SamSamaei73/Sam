"""The trusted voice-profile catalog.

Built once, by Sam's configuration code, from ``TrustedVoiceProfile`` values.
After construction it exposes only ``get``/``list_profiles``: there is no
register, replace, enable, or disable method, and the data is held behind a
``MappingProxyType``, so runtime code (the gateway, the boundary, AgentCore)
cannot alter it. A different voice is a different *configuration*, never a
runtime decision by text, an LLM, or a provider.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from sam.tts.errors import TTSProfileNotFoundError, TTSValidationError
from sam.tts.models import (
    MAX_TTS_PROFILES,
    TrustedVoiceProfile,
    TTSErrorCategory,
)


class TrustedVoiceProfiles:
    __slots__ = ("_profiles",)

    _profiles: Mapping[str, TrustedVoiceProfile]

    def __init__(self, profiles: Sequence[TrustedVoiceProfile]) -> None:
        if len(profiles) > MAX_TTS_PROFILES:
            raise ValueError("too many voice profiles")
        built: dict[str, TrustedVoiceProfile] = {}
        for profile in profiles:
            if profile.profile_id in built:
                raise ValueError("duplicate voice profile id")
            built[profile.profile_id] = profile
        object.__setattr__(self, "_profiles", MappingProxyType(built))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("the voice profile catalog is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("the voice profile catalog is read-only")

    def get(self, profile_id: str) -> TrustedVoiceProfile:
        """Resolve one *enabled* profile or raise. Unknown and disabled
        profiles are rejected before any permission check or network call."""

        profile = self._profiles.get(profile_id)
        if profile is None:
            raise TTSProfileNotFoundError("voice profile is not available")
        if not profile.enabled:
            raise TTSValidationError(
                "voice profile is not available",
                category=TTSErrorCategory.PROFILE_DISABLED,
            )
        return profile

    def list_profiles(self) -> tuple[TrustedVoiceProfile, ...]:
        return tuple(sorted(self._profiles.values(), key=lambda p: p.profile_id))

    def __repr__(self) -> str:
        return f"TrustedVoiceProfiles(count={len(self._profiles)})"


__all__ = ["TrustedVoiceProfiles"]
