"""Owner-approved Sam speech; bounded, single-use tickets contain trusted text.

The webview cannot submit text, a provider, a principal or a grant to this path.
Only a fixed preview/greeting or a verified owner's AgentCore reply is queued.
Native playback uses Sam's existing macOS voice; cloud output uses TTSGateway.
"""

from __future__ import annotations

import base64
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from sam.desktop.models import OperationResult, SpeakResponse
from sam.language.catalog import LanguageCode
from sam.models.models import PrivacyClass
from sam.models.policies import PrivacyPolicy
from sam.permissions.models import (
    PermissionAction,
    PermissionGrant,
    PermissionRequest,
    PermissionResource,
    PermissionScope,
)
from sam.system.local_voice import InstalledVoiceCatalog
from sam.tts.models import SynthesisRequest, TTSStatus
from sam.tts.validation import validate_text

if TYPE_CHECKING:
    from sam.desktop.runtime import DesktopRuntime

PREVIEW = {
    "en": "Hi, I'm Sam. I'm ready to help.",
    "fa": "سلام، من سام هستم. آماده‌ام کمکت کنم.",
}
GREETING = {"en": "Hi, I'm listening.", "fa": "سلام، گوش می‌دم."}


class SamVoiceStatus(BaseModel):
    state: Literal[
        "SAM_VOICE_READY",
        "SAM_VOICE_NOT_CONFIGURED",
        "SAM_VOICE_PROVIDER_UNAVAILABLE",
        "SAM_VOICE_PERMISSION_REQUIRED",
    ]
    reason_code: str | None = None
    enabled: bool
    external_approved: bool
    provider: str | None = None
    profile: str | None = None
    voice_name: str | None = None
    language: LanguageCode
    external: bool = False


class SpeechTicketResponse(OperationResult):
    speech_id: str | None = None


class SpeechPlaybackResponse(SpeakResponse):
    kind: Literal["local", "audio"] | None = None
    # Returned only to the trusted native shell, which consumes it for playback.
    text: str | None = Field(default=None, repr=False)
    language: LanguageCode | None = None


@dataclass(repr=False)
class _Ticket:
    text: str
    language: LanguageCode
    provider: str
    profile: str
    external: bool
    expires: float
    privacy: PrivacyClass


class SamSpeechService:
    def __init__(self, runtime: DesktopRuntime) -> None:
        self.runtime = runtime
        self._lock = RLock()
        self._tickets: OrderedDict[str, _Ticket] = OrderedDict()
        self._grants: dict[tuple[str, str], str] = {}
        self._catalog = InstalledVoiceCatalog()
        self._generation = 0

    def local_voice(self, language: str) -> str | None:
        return self._catalog.voice(language)

    def status(self, language: LanguageCode) -> SamVoiceStatus:
        settings = self.runtime.owner_settings
        voice = self.local_voice(language)
        provider = "macos-system" if voice else None
        profile = f"sam-local-{language}" if voice else None
        external = False
        if voice is None and self.runtime.speech_profiles is not None:
            for candidate in self.runtime.speech_profiles.list_profiles():
                if (
                    candidate.enabled
                    and language
                    in self.runtime.speech_profile_languages.get(
                        candidate.profile_id, frozenset()
                    )
                ):
                    provider, profile = candidate.provider_id, candidate.profile_id
                    voice, external = candidate.profile_id, True
                    break
        state: SamVoiceStatus = SamVoiceStatus(
            state="SAM_VOICE_READY",
            enabled=settings.voice_responses(),
            external_approved=settings.voice_external(),
            provider=provider,
            profile=profile,
            voice_name=voice,
            language=language,
            external=external,
        )
        if provider is None:
            state.state = (
                "SAM_VOICE_PROVIDER_UNAVAILABLE"
                if self._catalog.failed
                else "SAM_VOICE_NOT_CONFIGURED"
            )
            state.reason_code = (
                "local_voice_unavailable"
                if self._catalog.failed
                else "speech_language_unavailable"
            )
        elif not state.enabled or (external and not state.external_approved):
            state.state = "SAM_VOICE_PERMISSION_REQUIRED"
            state.reason_code = (
                "external_speech_consent_required"
                if external and not state.external_approved
                else "automatic_speech_off"
            )
        return state

    def configure(self, enabled: bool, external_approved: bool) -> None:
        with self._lock:
            self._generation += 1
            self.runtime.owner_settings.set_voice_responses(enabled)
            self.runtime.owner_settings.set_voice_external(external_approved)
            self._tickets.clear()
            for grant_id in self._grants.values():
                self.runtime.grants.revoke_grant(grant_id, now=self.runtime.clock())
            self._grants.clear()
        self.runtime.activity.add(
            "speech", "Automatic Sam speech", "on" if enabled else "off"
        )

    def queue(
        self,
        text: str,
        language: LanguageCode,
        *,
        privacy: PrivacyClass = PrivacyClass.NORMAL,
    ) -> SpeechTicketResponse:
        with self._lock:
            status = self.status(language)
            if status.state != "SAM_VOICE_READY":
                return SpeechTicketResponse(
                    status="denied", reason_code=status.reason_code
                )
            if (
                self.runtime.identity is not None
                and self.runtime.identity.guests.current() is not None
            ):
                return SpeechTicketResponse(
                    status="denied", reason_code="guest_not_allowed"
                )
            try:
                clean = validate_text(text)
                if len(clean) > 8_000:
                    raise ValueError("speech length")
            except Exception:
                return SpeechTicketResponse(
                    status="denied", reason_code="speech_content_withheld"
                )
            privacy = PrivacyPolicy().classify((clean,), declared=privacy)
            if status.external and privacy >= PrivacyClass.PRIVATE:
                return SpeechTicketResponse(
                    status="denied", reason_code="private_speech_withheld"
                )
            assert status.provider is not None and status.profile is not None
            speech_id = uuid4().hex
            self._tickets[speech_id] = _Ticket(
                clean,
                language,
                status.provider,
                status.profile,
                status.external,
                time.monotonic() + 60,
                privacy,
            )
            while len(self._tickets) > 32:
                self._tickets.popitem(last=False)
            return SpeechTicketResponse(status="ok", speech_id=speech_id)

    def play(self, speech_id: str) -> SpeechPlaybackResponse:
        with self._lock:
            generation = self._generation
            if (
                self.runtime.identity is not None
                and self.runtime.identity.guests.current() is not None
            ):
                return SpeechPlaybackResponse(
                    status="denied", reason_code="guest_not_allowed"
                )
            ticket = self._tickets.pop(speech_id, None)
            if ticket is None or ticket.expires < time.monotonic():
                return SpeechPlaybackResponse(
                    status="denied", reason_code="speech_session_invalid"
                )
            status = self.status(ticket.language)
            if status.state != "SAM_VOICE_READY":
                return SpeechPlaybackResponse(
                    status="denied", reason_code=status.reason_code
                )
            if (status.provider, status.profile) != (ticket.provider, ticket.profile):
                return SpeechPlaybackResponse(
                    status="denied", reason_code="speech_profile_changed"
                )
            try:
                validate_text(ticket.text)  # privacy is re-evaluated per response
            except Exception:
                return SpeechPlaybackResponse(
                    status="denied", reason_code="speech_content_withheld"
                )
            privacy = PrivacyPolicy().classify((ticket.text,), declared=ticket.privacy)
            if ticket.external and privacy >= PrivacyClass.PRIVATE:
                return SpeechPlaybackResponse(
                    status="denied", reason_code="private_speech_withheld"
                )
            key = (ticket.provider, ticket.profile)
            scope = PermissionScope(segments=("assistant", *key))
            if key not in self._grants:
                grant_id = "sam-speech-" + uuid4().hex
                now = self.runtime.clock()
                self.runtime.grants.create_grant(
                    PermissionGrant(
                        grant_id=grant_id,
                        principal=self.runtime.principal,
                        resource=PermissionResource.SPEECH_SYNTHESIS,
                        action=PermissionAction.SEND,
                        scope=scope,
                        created_at=now,
                        updated_at=now,
                        metadata={"origin": "owner_speech_mode"},
                    )
                )
                self._grants[key] = grant_id
            if not ticket.external:
                decision = self.runtime.permissions.evaluate(
                    PermissionRequest(
                        principal=self.runtime.principal,
                        resource=PermissionResource.SPEECH_SYNTHESIS,
                        action=PermissionAction.SEND,
                        scope=scope,
                        target="Sam voice output",
                        correlation_id=speech_id,
                    )
                )
                if decision.outcome.value != "allow":
                    return SpeechPlaybackResponse(
                        status="denied", reason_code="speech_permission_denied"
                    )
                self.runtime.activity.add("speech", "Sam speech authorized", "local")
                return SpeechPlaybackResponse(
                    status="ok",
                    kind="local",
                    text=ticket.text,
                    language=ticket.language,
                )
            gateway = self.runtime.automatic_speech_gateway
            if gateway is None:
                return SpeechPlaybackResponse(
                    status="failed", reason_code="speech_provider_unavailable"
                )
        # The owner can turn speech off while an external request is running.
        # PermissionEngine still re-evaluates immediately before dispatch.
        result = gateway.synthesize(
            SynthesisRequest(
                principal=self.runtime.principal,
                text=ticket.text,
                trusted_voice_profile=ticket.profile,
                request_id=speech_id,
            )
        )
        with self._lock:
            if (
                generation != self._generation
                or not self.runtime.owner_settings.voice_responses()
            ):
                return SpeechPlaybackResponse(
                    status="denied", reason_code="automatic_speech_off"
                )
            if result.status is not TTSStatus.SUCCEEDED or result.audio is None:
                reason = (
                    result.error_category.value
                    if result.error_category
                    else "speech_provider_unavailable"
                )
                return SpeechPlaybackResponse(status="failed", reason_code=reason)
            return SpeechPlaybackResponse(
                status="ok",
                kind="audio",
                language=ticket.language,
                audio_base64=base64.b64encode(result.audio.audio_bytes).decode("ascii"),
                audio_format=result.audio.format.value,
                byte_length=result.audio.byte_length,
            )
