"""The voice gateway: one explicit, finite, fail-closed path from supplied
audio to a normalized transcript.

    Voice input != authorization.   Voice identity != authorization.
    Transcript != permission.       Transcript != confirmation.

Lifecycle of ``process`` (each stage can only stop the request):

    1. session exists, is open, and belongs to the principal
    2. utterance id not already used                       -> REJECTED
    3. validate audio (bounded, structural, no codecs)     -> REJECTED
    4. build the PermissionRequest (policy: builds, never decides)
    5. PermissionEngine.evaluate()      <- the ONLY authorization authority
         DENY -> DENIED;  CONFIRM_REQUIRED -> CONFIRMATION_REQUIRED
         (nothing below runs, the provider is never called)
    6. verify + one-time-consume a presented identity signal (if any)
    7. reserve the utterance slot (bounded, at-most-once)
    8. AT MOST ONE transcription call, Sam-owned timeout, contained errors
    9. validate the untrusted transcript (rejected, never truncated)
   10. optional identity assessment -> a signal (never an authorization)
   11. emit ONE content-free audit event
   12. return a normalized ``VoiceProcessingResult``

The gateway returns *input*. It never acts on a transcript: it holds no MCP,
coding, computer, Memory, or Knowledge capability, and there is no code path
from a transcript or an identity result to an allow or an approved
confirmation. ``process`` never raises and never retries.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sam.memory.sanitization import looks_like_secret
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    DecisionOutcome,
    DenialReason,
    PermissionDecision,
    PermissionRequest,
    Principal,
    RiskLevel,
)
from sam.voice import policy
from sam.voice.audio import validate_audio
from sam.voice.audit import VoiceAuditSink
from sam.voice.errors import (
    VoiceError,
    VoiceIdentityError,
    VoicePolicyError,
)
from sam.voice.identity import VoiceIdentityService
from sam.voice.models import (
    DEFAULT_VOICE_PROVIDER_TIMEOUT_SECONDS,
    MAX_VOICE_PROVIDER_TIMEOUT_SECONDS,
    AudioMetadata,
    EndSessionRequest,
    PermissionOutcomeSummary,
    StartSessionRequest,
    TranscriptionRequest,
    ValidatedAudio,
    VoiceAuditEvent,
    VoiceErrorCategory,
    VoiceForwardingDecision,
    VoiceIdentityStatus,
    VoiceOperation,
    VoiceProcessingRequest,
    VoiceProcessingResult,
    VoiceProcessingStatus,
    VoiceSessionResult,
    new_id,
    utc_now,
)
from sam.voice.session import VoiceSessionManager
from sam.voice.transcription import (
    TranscriptionProvider,
    require_valid_provider_id,
    transcribe,
)

_S = VoiceProcessingStatus
_C = VoiceErrorCategory


@dataclass
class _Run:
    started: float
    session_id: str | None = None
    utterance_id: str | None = None
    audio_bytes: int | None = None
    audio: AudioMetadata | None = None
    risk: RiskLevel | None = None
    permission_request: PermissionRequest | None = None
    permission_outcome: PermissionOutcomeSummary | None = None
    provider_id: str | None = None
    identity_status: VoiceIdentityStatus = VoiceIdentityStatus.NOT_CHECKED
    identity_checked: bool = False
    identity_reason: str | None = None
    attempted: bool = False


class VoiceGateway:
    def __init__(
        self,
        *,
        permission_engine: PermissionEngine,
        transcription_provider: TranscriptionProvider,
        identity_service: VoiceIdentityService | None = None,
        audit_sink: VoiceAuditSink | None = None,
        clock: Callable[[], datetime] = utc_now,
        provider_timeout_seconds: float = DEFAULT_VOICE_PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        if not 0 < provider_timeout_seconds <= MAX_VOICE_PROVIDER_TIMEOUT_SECONDS:
            raise ValueError("timeout must be within the voice provider bound")
        self._permission_engine = permission_engine
        self._provider = transcription_provider
        self._provider_id = require_valid_provider_id(
            transcription_provider.provider_id
        )
        self._identity = identity_service
        self._audit = audit_sink
        self._clock = clock
        self._timeout = provider_timeout_seconds
        # Encapsulated: never injected, never handed out.
        self._sessions = VoiceSessionManager(clock)

    # ------------------------------------------------------------------ #
    # sessions
    # ------------------------------------------------------------------ #

    def start_session(
        self, request: StartSessionRequest, *, confirmation_id: str | None = None
    ) -> VoiceSessionResult:
        return self._session_op(
            VoiceOperation.START_SESSION, request, None, confirmation_id
        )

    def end_session(
        self, request: EndSessionRequest, *, confirmation_id: str | None = None
    ) -> VoiceSessionResult:
        return self._session_op(
            VoiceOperation.END_SESSION, request, request.session_id, confirmation_id
        )

    def _session_op(
        self,
        operation: VoiceOperation,
        request: StartSessionRequest | EndSessionRequest,
        session_id: str | None,
        confirmation_id: str | None,
    ) -> VoiceSessionResult:
        now = self._clock()
        run = _Run(started=time.monotonic(), session_id=session_id)
        principal = request.principal

        def result(
            status: VoiceProcessingStatus,
            category: VoiceErrorCategory | None = None,
            *,
            sid: str | None = None,
            confirmation: str | None = None,
        ) -> VoiceSessionResult:
            return VoiceSessionResult(
                operation=operation,
                principal=principal,
                status=status,
                session_id=sid if sid is not None else session_id,
                permission_outcome=run.permission_outcome,
                error_category=category,
                confirmation_id=confirmation,
                created_at=now,
            )

        try:
            if operation is VoiceOperation.END_SESSION and session_id is not None:
                self._sessions.require_open(session_id, principal)
            perm = policy.build_permission_request(
                operation, principal, session_id=session_id, reason=request.reason
            )
            run.permission_request = perm
            run.risk = policy.risk_for(operation)
            decision = self._permission_engine.evaluate(
                perm, confirmation_id=confirmation_id
            )
            run.risk = decision.risk
            gate = self._gate(decision, run)
            if gate is not None:
                status, category, pending = gate
                out = result(status, category, confirmation=pending)
            elif operation is VoiceOperation.START_SESSION:
                out = result(_S.SUCCEEDED, sid=self._sessions.open(principal))
            else:
                assert session_id is not None
                self._sessions.close(session_id, principal)
                out = result(_S.SUCCEEDED)
        except VoiceError as error:
            out = result(_S.REJECTED, error.category)
        except Exception:
            out = result(_S.FAILED, _C.INTERNAL_ERROR)
        self._record(
            operation,
            principal,
            now,
            run,
            status=out.status,
            category=out.error_category,
            session_id=out.session_id,
        )
        return out

    # ------------------------------------------------------------------ #
    # process one utterance
    # ------------------------------------------------------------------ #

    def process(
        self, request: VoiceProcessingRequest, *, confirmation_id: str | None = None
    ) -> VoiceProcessingResult:
        now = self._clock()
        run = _Run(
            started=time.monotonic(),
            session_id=request.session_id,
            utterance_id=request.utterance_id,
            audio_bytes=len(request.audio.content),
        )
        try:
            result = self._process_unsafe(
                request, confirmation_id=confirmation_id, run=run, now=now
            )
        except Exception:
            result = self._failure(request, run, now, _S.FAILED, _C.INTERNAL_ERROR)
        self._record(
            VoiceOperation.PROCESS_UTTERANCE,
            request.principal,
            now,
            run,
            status=result.status,
            category=result.error_category,
            session_id=request.session_id,
        )
        return result

    def _process_unsafe(
        self,
        request: VoiceProcessingRequest,
        *,
        confirmation_id: str | None,
        run: _Run,
        now: datetime,
    ) -> VoiceProcessingResult:
        principal = request.principal
        rejected = _S.REJECTED

        # 1-2. session ownership, duplicate utterance id (peek only)
        try:
            if self._sessions.has_utterance(
                request.session_id, principal, request.utterance_id
            ):
                return self._failure(
                    request, run, now, rejected, _C.DUPLICATE_UTTERANCE
                )
        except VoiceError as error:
            return self._failure(request, run, now, rejected, error.category)

        # 3. audio validation (before permission, pure)
        try:
            validated = validate_audio(request.audio)
        except VoiceError as error:
            return self._failure(request, run, now, rejected, error.category)
        run.audio = validated.metadata

        # 4-5. policy builds; the PermissionEngine decides
        try:
            perm = policy.build_permission_request(
                VoiceOperation.PROCESS_UTTERANCE,
                principal,
                session_id=request.session_id,
                utterance_id=request.utterance_id,
                audio_digest=validated.metadata.digest_sha256,
                reason=request.reason,
            )
        except VoicePolicyError as error:
            return self._failure(request, run, now, rejected, error.category)
        run.permission_request = perm
        decision = self._permission_engine.evaluate(
            perm, confirmation_id=confirmation_id
        )
        run.risk = decision.risk
        gate = self._gate(decision, run)
        if gate is not None:
            status, category, pending = gate
            return self._failure(
                request, run, now, status, category, confirmation=pending
            )

        # 6. a presented identity signal must match THIS session/utterance/audio
        presented = request.identity_signal
        if presented is not None:
            if self._identity is None:
                return self._failure(
                    request, run, now, rejected, _C.IDENTITY_SIGNAL_INVALID
                )
            try:
                self._identity.verify_and_consume(
                    presented,
                    session_id=request.session_id,
                    utterance_id=request.utterance_id,
                    audio_digest=validated.metadata.digest_sha256,
                )
            except VoiceIdentityError as error:
                return self._failure(request, run, now, rejected, error.category)

        # 7. reserve the utterance slot (bounded, at-most-once)
        try:
            self._sessions.reserve_utterance(
                request.session_id, principal, request.utterance_id
            )
        except VoiceError as error:
            return self._failure(request, run, now, rejected, error.category)

        # 8-9. exactly one provider call; validated, never retried
        run.attempted = True
        run.provider_id = self._provider_id
        transcription_request = TranscriptionRequest(
            session_id=request.session_id,
            utterance_id=request.utterance_id,
            audio=validated,
        )
        try:
            transcript = transcribe(
                self._provider, transcription_request, timeout_seconds=self._timeout
            )
        except VoiceError as error:
            return self._failure(request, run, now, _S.FAILED, error.category)

        # 9b. secret-looking transcripts are WITHHELD, not redacted-and-sent:
        # the result carries no transcript at all, so nothing downstream can
        # forward it to AgentCore or an LLM provider. Identity status can
        # never override this (identity is not even assessed).
        if looks_like_secret(transcript.text):
            return self._failure(
                request,
                run,
                now,
                _S.FAILED,
                _C.SECRET_DETECTED,
                secret_withheld=True,
            )

        # 10. identity: an authentication signal only
        if presented is not None:
            run.identity_checked = True
            run.identity_status = presented.status
        elif self._identity is not None:
            run.identity_checked = True
            run.identity_status, run.identity_reason = self._assess_identity(
                request, validated
            )

        return VoiceProcessingResult(
            session_id=request.session_id,
            utterance_id=request.utterance_id,
            principal=principal,
            status=_S.SUCCEEDED,
            permission_outcome=PermissionOutcomeSummary.ALLOW,
            transcript=transcript.text,
            transcript_secret_like=False,
            forwarding=VoiceForwardingDecision.ELIGIBLE,
            provider_id=transcript.provider_id,
            language=transcript.language,
            identity_status=run.identity_status,
            identity_checked=run.identity_checked,
            identity_reason_code=run.identity_reason,
            audio=validated.metadata,
            transcription_attempted=True,
            processed_at=now,
            duration_ms=self._elapsed(run),
        )

    def _assess_identity(
        self, request: VoiceProcessingRequest, validated: ValidatedAudio
    ) -> tuple[VoiceIdentityStatus, str | None]:
        """A failing/hostile identity provider degrades to UNKNOWN (no
        assurance) — it never blocks transcription and never authorizes."""

        assert self._identity is not None
        try:
            signal = self._identity.assess(
                request.session_id, request.utterance_id, validated
            )
        except VoiceIdentityError:
            return VoiceIdentityStatus.UNKNOWN, "identity_error"
        return signal.status, None

    def _gate(
        self, decision: PermissionDecision, run: _Run
    ) -> tuple[VoiceProcessingStatus, VoiceErrorCategory, str | None] | None:
        if decision.outcome is DecisionOutcome.DENY:
            run.permission_outcome = PermissionOutcomeSummary.DENY
            category = (
                _C.CONFIRMATION_INVALID
                if decision.reason is DenialReason.CONFIRMATION_INVALID
                else _C.PERMISSION_DENIED
            )
            return _S.DENIED, category, None
        if decision.outcome is DecisionOutcome.CONFIRM_REQUIRED:
            run.permission_outcome = PermissionOutcomeSummary.CONFIRM_REQUIRED
            pending = (
                decision.confirmation.confirmation_id if decision.confirmation else None
            )
            return _S.CONFIRMATION_REQUIRED, _C.CONFIRMATION_REQUIRED, pending
        run.permission_outcome = PermissionOutcomeSummary.ALLOW
        return None

    def _elapsed(self, run: _Run) -> int:
        return int((time.monotonic() - run.started) * 1000)

    def _failure(
        self,
        request: VoiceProcessingRequest,
        run: _Run,
        now: datetime,
        status: VoiceProcessingStatus,
        category: VoiceErrorCategory,
        *,
        confirmation: str | None = None,
        secret_withheld: bool = False,
    ) -> VoiceProcessingResult:
        return VoiceProcessingResult(
            session_id=request.session_id,
            utterance_id=request.utterance_id,
            principal=request.principal,
            status=status,
            permission_outcome=run.permission_outcome,
            error_category=(None if status is _S.CONFIRMATION_REQUIRED else category),
            confirmation_id=confirmation,
            transcript_secret_like=secret_withheld,
            forwarding=(
                VoiceForwardingDecision.WITHHELD_SECRET_DETECTED
                if secret_withheld
                else VoiceForwardingDecision.NOT_APPLICABLE
            ),
            provider_id=run.provider_id,
            identity_status=run.identity_status,
            identity_checked=run.identity_checked,
            identity_reason_code=run.identity_reason,
            audio=run.audio,
            transcription_attempted=run.attempted,
            processed_at=now,
            duration_ms=self._elapsed(run),
        )

    def _record(
        self,
        operation: VoiceOperation,
        principal: Principal,
        now: datetime,
        run: _Run,
        *,
        status: VoiceProcessingStatus,
        category: VoiceErrorCategory | None,
        session_id: str | None,
    ) -> None:
        if self._audit is None:
            return
        perm = run.permission_request
        try:
            event = VoiceAuditEvent(
                event_id=new_id(),
                occurred_at=now,
                operation=operation,
                principal=principal,
                session_id=session_id,
                utterance_id=run.utterance_id,
                permission_action=perm.action if perm else None,
                permission_resource=perm.resource if perm else None,
                risk=run.risk,
                authorization_outcome=run.permission_outcome,
                status=status,
                error_category=category,
                audio_format=run.audio.audio_format if run.audio else None,
                audio_bytes=run.audio_bytes,
                audio_duration_seconds=(
                    run.audio.duration_seconds if run.audio else None
                ),
                provider_id=run.provider_id,
                identity_status=(run.identity_status if run.identity_checked else None),
                transcription_attempted=run.attempted,
                duration_ms=self._elapsed(run),
            )
            self._audit.record(event)
        except Exception:
            # Best-effort observability, never an authorization gate.
            return


__all__ = ["VoiceGateway"]
