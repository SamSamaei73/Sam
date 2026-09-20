"""Ties speaker verification, challenges and Guest Mode together.

The coordinator sits IN FRONT of the Phase 9 voice gateway: an utterance is
classified (owner / guest / blocked) BEFORE it is transcribed or reaches
AgentCore, so a non-owner's speech is never turned into text for the agent
while Sam is owner-only.

Nothing here produces a PermissionEngine decision, an approved confirmation,
or a CRITICAL authentication. ``SpeakerDecision`` has no such field.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from sam.voice.audio import validate_audio
from sam.voice.errors import VoiceError
from sam.voice.models import AudioInput, TranscriptionRequest, ValidatedAudio
from sam.voice.transcription import TranscriptionProvider
from sam.voice.validation import validate_transcription_result
from sam.voice_identity.audit import VoiceIdentityAuditSink
from sam.voice_identity.authority import OwnerProof, OwnerProofAuthority
from sam.voice_identity.challenge import Challenge, ChallengeService
from sam.voice_identity.errors import ChallengeError
from sam.voice_identity.guest import GuestSession, GuestSessionManager
from sam.voice_identity.models import utc_now
from sam.voice_identity.policy import (
    IdentityMode,
    SpeakerClass,
    SpeakerResult,
    classify_speaker,
)
from sam.voice_identity.verification import SpeakerVerifier


@dataclass(frozen=True)
class SpeakerDecision:
    """Safe, normalized outcome for the UI: no score, no biometric data."""

    result: SpeakerResult
    speaker_class: SpeakerClass
    reason_code: str
    mode: IdentityMode
    audio_digest: str | None = None
    guest: GuestSession | None = None


class VoiceIdentityCoordinator:
    def __init__(
        self,
        *,
        verifier: SpeakerVerifier,
        challenges: ChallengeService,
        guests: GuestSessionManager,
        proofs: OwnerProofAuthority,
        audit: VoiceIdentityAuditSink,
        transcriber: TranscriptionProvider | None = None,
        clock: Callable[[], datetime] = utc_now,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._verifier = verifier
        self._challenges = challenges
        self._guests = guests
        self._proofs = proofs
        self._audit = audit
        self._transcriber = transcriber
        self._clock = clock
        self._timeout = timeout_seconds

    # ------------------------------------------------------ classification

    def decide(self, audio: AudioInput | ValidatedAudio) -> SpeakerDecision:
        """Classify ONE utterance. Never raises; any defect -> blocked."""

        guest = self._guests.current()
        mode = IdentityMode.GUEST_MODE if guest is not None else IdentityMode.OWNER_ONLY
        validated = self._validated(audio)
        if validated is None:
            return SpeakerDecision(
                SpeakerResult.INSUFFICIENT_AUDIO,
                SpeakerClass.BLOCKED,
                "audio_invalid",
                mode,
            )
        verification = self._verifier.verify(validated)
        result = SpeakerResult(verification.result)
        speaker_class = classify_speaker(result, guest_active=guest is not None)
        self._audit_decision(result, speaker_class)
        return SpeakerDecision(
            result,
            speaker_class,
            verification.reason_code,
            mode,
            verification.audio_digest,
            guest if speaker_class is SpeakerClass.GUEST else None,
        )

    # ----------------------------------------------------------- challenge

    def issue_challenge(self, session_id: str) -> Challenge:
        challenge = self._challenges.issue(session_id)
        self._audit.record("challenge_issued", "ok")
        return challenge

    def prove_owner(
        self,
        *,
        session_id: str,
        challenge_id: str,
        audio: AudioInput | ValidatedAudio,
        purpose: str = "start_guest",
    ) -> OwnerProof | None:
        """One utterance must be BOTH the owner's voice AND the fresh
        challenge. On any failure the caller learns only that verification
        failed (no oracle for which half failed). The challenge is consumed
        either way."""

        validated = self._validated(audio)
        spoke_challenge = False
        speaker_ok = False
        digest = ""
        if validated is not None:
            digest = validated.metadata.digest_sha256
            speaker_ok = (
                self._verifier.verify(validated).result == SpeakerResult.OWNER_VERIFIED
            )
            transcript = self._transcribe(validated, session_id)
            try:
                spoke_challenge = transcript is not None and self._challenges.evaluate(
                    challenge_id, session_id, transcript
                )
            except ChallengeError:
                spoke_challenge = False
        else:
            try:  # burn the challenge even for unusable audio
                self._challenges.evaluate(challenge_id, session_id, "")
            except ChallengeError:
                pass
        if speaker_ok and spoke_challenge:
            self._audit.record("challenge_passed", "ok")
            return self._proofs.mint(
                session_id=session_id, audio_digest=digest, purpose=purpose
            )
        self._audit.record("challenge_failed", "verification_failed")
        return None

    # ------------------------------------------------------------ internals

    def _validated(self, audio: AudioInput | ValidatedAudio) -> ValidatedAudio | None:
        if isinstance(audio, ValidatedAudio):
            return audio
        try:
            return validate_audio(audio)
        except (VoiceError, ValidationError, ValueError):
            return None

    def _transcribe(self, audio: ValidatedAudio, session_id: str) -> str | None:
        if self._transcriber is None:
            return None
        try:
            request = TranscriptionRequest(
                session_id=session_id, utterance_id=uuid.uuid4().hex, audio=audio
            )
            raw = self._transcriber.transcribe(request, timeout_seconds=self._timeout)
            return validate_transcription_result(
                raw, self._transcriber.provider_id
            ).text
        except Exception:
            return None

    def _audit_decision(
        self, result: SpeakerResult, speaker_class: SpeakerClass
    ) -> None:
        if result is SpeakerResult.OWNER_VERIFIED:
            self._audit.record("owner_voice_verified", "speaker_match")
        elif result is SpeakerResult.OWNER_NOT_VERIFIED:
            self._audit.record("owner_voice_not_verified", "speaker_mismatch")
        if speaker_class is SpeakerClass.BLOCKED:
            self._audit.record("speaker_blocked", result.value)


__all__ = ["SpeakerDecision", "VoiceIdentityCoordinator"]
