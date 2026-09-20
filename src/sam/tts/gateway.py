"""The speech-synthesis gateway: one explicit, finite, fail-closed path from
text to validated audio.

    Text != provider configuration     Fish Audio != security boundary
    Text != voice/model selection      TTS output != proof of action
    Text != authorization

Lifecycle of ``synthesize`` (each stage can only stop the request):

    1. resolve the TRUSTED voice profile (unknown/disabled -> REJECTED)
    2. validate the text (size, UTF-8, control chars)      -> REJECTED
    3. secret detection: a secret-looking text is WITHHELD -> REJECTED,
       and nothing below runs (no permission record, no credential, no network)
    4. duplicate request id?                                -> REJECTED
    5. policy builds the PermissionRequest (scope from the trusted profile)
    6. PermissionEngine.evaluate()        <- the ONLY authorization authority
         DENY -> DENIED;  CONFIRM_REQUIRED -> CONFIRMATION_REQUIRED
         (the provider is never called; a failure also fails closed)
    7. reserve the request id (bounded, at-most-once)
    8. AT MOST ONE provider call, Sam-owned timeout, contained errors
    9. validate the untrusted audio (size, signature); digest computed by Sam
   10. emit ONE content-free audit event
   11. return a normalized ``SynthesisResult`` (audio held in memory only)

The gateway holds no credential (the provider resolves its own key at its
transport boundary), no Memory/Knowledge capability, and starts no thread.
Synthesis happens only from an explicit request; nothing a provider returns
can trigger another. ``synthesize`` never raises and never retries.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    DecisionOutcome,
    DenialReason,
    PermissionDecision,
    PermissionRequest,
    RiskLevel,
)
from sam.tts import policy
from sam.tts.audio import validate_audio_bytes
from sam.tts.audit import TTSAuditSink
from sam.tts.errors import TTSError, TTSSecretDetectedError
from sam.tts.models import (
    DEFAULT_TTS_TIMEOUT_SECONDS,
    MAX_TRACKED_TTS_REQUEST_IDS,
    MAX_TTS_TIMEOUT_SECONDS,
    PermissionOutcomeSummary,
    ProviderSynthesisRequest,
    ProviderSynthesisResult,
    SynthesisRequest,
    SynthesisResult,
    SynthesizedAudio,
    TrustedVoiceProfile,
    TTSAuditEvent,
    TTSErrorCategory,
    TTSStatus,
    new_id,
    utc_now,
)
from sam.tts.profiles import TrustedVoiceProfiles
from sam.tts.provider import SpeechSynthesisProvider, require_valid_provider_id
from sam.tts.validation import validate_text

_S = TTSStatus
_C = TTSErrorCategory


@dataclass
class _Run:
    started: float
    synthesis_id: str
    profile: TrustedVoiceProfile | None = None
    text_length: int | None = None
    perm: PermissionRequest | None = None
    risk: RiskLevel | None = None
    outcome: PermissionOutcomeSummary | None = None
    attempted: bool = False
    output_size: int | None = None


class TTSGateway:
    def __init__(
        self,
        *,
        permission_engine: PermissionEngine,
        provider: SpeechSynthesisProvider,
        profiles: TrustedVoiceProfiles,
        audit_sink: TTSAuditSink | None = None,
        clock: Callable[[], datetime] = utc_now,
        provider_timeout_seconds: float = DEFAULT_TTS_TIMEOUT_SECONDS,
    ) -> None:
        if not 0 < provider_timeout_seconds <= MAX_TTS_TIMEOUT_SECONDS:
            raise ValueError("timeout must be within the speech-synthesis bound")
        self._engine = permission_engine
        self._provider = provider
        self._provider_id = require_valid_provider_id(provider.provider_id)
        self._profiles = profiles
        self._audit = audit_sink
        self._clock = clock
        self._timeout = provider_timeout_seconds
        self._seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._seen_lock = RLock()

    def synthesize(
        self, request: SynthesisRequest, *, confirmation_id: str | None = None
    ) -> SynthesisResult:
        now = self._clock()
        run = _Run(started=time.monotonic(), synthesis_id=new_id())
        try:
            result = self._run(request, confirmation_id, run, now)
        except Exception:
            result = self._result(request, run, now, _S.FAILED, _C.INTERNAL_ERROR)
        self._record(request, run, result, now)
        return result

    # ------------------------------------------------------------------ #

    def _run(
        self,
        request: SynthesisRequest,
        confirmation_id: str | None,
        run: _Run,
        now: datetime,
    ) -> SynthesisResult:
        rejected = _S.REJECTED

        # 1. trusted profile
        try:
            profile = self._profiles.get(request.trusted_voice_profile)
        except TTSError as error:
            return self._result(request, run, now, rejected, error.category)
        if profile.provider_id != self._provider_id:
            # A profile bound to another provider can never reach this one.
            return self._result(request, run, now, rejected, _C.UNKNOWN_PROFILE)
        run.profile = profile
        run.text_length = len(request.text) if isinstance(request.text, str) else None

        # 2-3. text validation, then secret withholding
        try:
            text = validate_text(request.text)
        except TTSSecretDetectedError:
            return self._result(request, run, now, rejected, _C.SECRET_DETECTED)
        except TTSError as error:
            return self._result(request, run, now, rejected, error.category)

        # 4. duplicate request id (peek only)
        key = (request.principal.kind.value, request.principal.id, request.request_id)
        if self._already(key):
            return self._result(request, run, now, rejected, _C.DUPLICATE_REQUEST)

        # 5-6. policy builds; the PermissionEngine decides
        try:
            perm = policy.build_permission_request(
                request.principal, profile, request_id=request.request_id, text=text
            )
        except TTSError as error:
            return self._result(request, run, now, rejected, error.category)
        run.perm = perm
        run.risk = policy.risk_for()
        decision = self._engine.evaluate(perm, confirmation_id=confirmation_id)
        run.risk = decision.risk
        gate = self._gate(decision, request, run, now)
        if gate is not None:
            return gate

        # 7. reserve, then at most one provider call
        if not self._reserve(key):
            return self._result(request, run, now, rejected, _C.DUPLICATE_REQUEST)
        run.attempted = True
        provider_request = ProviderSynthesisRequest(
            synthesis_id=run.synthesis_id,
            text=text,
            voice_reference=profile.provider_voice_reference,
            model=profile.provider_model,
            output_format=profile.output_format,
        )
        deadline = time.monotonic() + self._timeout
        try:
            raw = self._provider.synthesize(
                provider_request, timeout_seconds=self._timeout
            )
        except TimeoutError:
            return self._result(request, run, now, _S.FAILED, _C.TIMEOUT)
        except TTSError as error:
            return self._result(request, run, now, _S.FAILED, error.category)
        except Exception:
            return self._result(request, run, now, _S.FAILED, _C.PROVIDER_ERROR)
        if time.monotonic() > deadline:
            # A late result is discarded, never used.
            return self._result(request, run, now, _S.FAILED, _C.TIMEOUT)

        # 8. validate the untrusted result (only the bytes are ever used)
        if not isinstance(raw, ProviderSynthesisResult):
            return self._result(request, run, now, _S.FAILED, _C.INVALID_AUDIO)
        try:
            audio_bytes, digest = validate_audio_bytes(
                raw.audio_bytes, profile.output_format
            )
        except TTSError as error:
            return self._result(request, run, now, _S.FAILED, error.category)
        run.output_size = len(audio_bytes)
        audio = SynthesizedAudio(
            synthesis_id=run.synthesis_id,
            provider_id=self._provider_id,  # Sam's, never provider-supplied
            trusted_profile_id=profile.profile_id,  # Sam's, never provider-supplied
            format=profile.output_format,
            byte_length=len(audio_bytes),
            sha256=digest,
            audio_bytes=audio_bytes,
            created_at=now,
        )
        return self._result(
            request,
            run,
            now,
            _S.SUCCEEDED,
            audio=audio,
            permission=PermissionOutcomeSummary.ALLOW,
        )

    # ------------------------------------------------------------------ #

    def _gate(
        self,
        decision: PermissionDecision,
        request: SynthesisRequest,
        run: _Run,
        now: datetime,
    ) -> SynthesisResult | None:
        if decision.outcome is DecisionOutcome.DENY:
            run.outcome = PermissionOutcomeSummary.DENY
            category = (
                _C.CONFIRMATION_INVALID
                if decision.reason is DenialReason.CONFIRMATION_INVALID
                else _C.PERMISSION_DENIED
            )
            return self._result(request, run, now, _S.DENIED, category)
        if decision.outcome is DecisionOutcome.CONFIRM_REQUIRED:
            run.outcome = PermissionOutcomeSummary.CONFIRM_REQUIRED
            pending = (
                decision.confirmation.confirmation_id if decision.confirmation else None
            )
            return self._result(
                request,
                run,
                now,
                _S.CONFIRMATION_REQUIRED,
                _C.CONFIRMATION_REQUIRED,
                confirmation_id=pending,
            )
        run.outcome = PermissionOutcomeSummary.ALLOW
        return None

    def _already(self, key: tuple[str, str, str]) -> bool:
        with self._seen_lock:
            return key in self._seen

    def _reserve(self, key: tuple[str, str, str]) -> bool:
        with self._seen_lock:
            if key in self._seen:
                return False
            self._seen[key] = None
            while len(self._seen) > MAX_TRACKED_TTS_REQUEST_IDS:
                self._seen.popitem(last=False)
            return True

    def _result(
        self,
        request: SynthesisRequest,
        run: _Run,
        now: datetime,
        status: TTSStatus,
        category: TTSErrorCategory | None = None,
        *,
        audio: SynthesizedAudio | None = None,
        permission: PermissionOutcomeSummary | None = None,
        confirmation_id: str | None = None,
    ) -> SynthesisResult:
        return SynthesisResult(
            request_id=request.request_id,
            synthesis_id=run.synthesis_id,
            principal=request.principal,
            status=status,
            permission_outcome=permission or run.outcome,
            error_category=None if status is _S.SUCCEEDED else category,
            confirmation_id=confirmation_id,
            audio=audio,
            provider_call_attempted=run.attempted,
            duration_ms=int((time.monotonic() - run.started) * 1000),
            created_at=now,
        )

    def _record(
        self,
        request: SynthesisRequest,
        run: _Run,
        result: SynthesisResult,
        now: datetime,
    ) -> None:
        if self._audit is None:
            return
        perm = run.perm
        try:
            self._audit.record(
                TTSAuditEvent(
                    event_id=new_id(),
                    occurred_at=now,
                    request_id=request.request_id,
                    synthesis_id=run.synthesis_id,
                    principal=request.principal,
                    provider_id=self._provider_id if run.profile else None,
                    trusted_profile_id=run.profile.profile_id if run.profile else None,
                    permission_action=perm.action if perm else None,
                    permission_resource=perm.resource if perm else None,
                    risk=run.risk,
                    scope=perm.scope.as_text() if perm else None,
                    authorization_outcome=result.permission_outcome,
                    status=result.status,
                    error_category=result.error_category,
                    text_length=run.text_length,
                    output_size=run.output_size,
                    provider_call_attempted=run.attempted,
                    duration_ms=result.duration_ms,
                )
            )
        except Exception:
            # Best-effort observability, never an authorization gate.
            return


__all__ = ["TTSGateway"]
