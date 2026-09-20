"""Timeout semantics after removing the daemon-thread wrapper.

Precise guarantee under test: Sam owns the timeout value and passes it to the
provider; the provider is called synchronously, exactly once, with no thread,
no retry, and no way for the request or the provider to raise the timeout. A
late result is discarded. (Sam cannot preempt a synchronous provider that
ignores the timeout - see docs/voice.md.)"""

from __future__ import annotations

import importlib
import threading

import pytest
from pydantic import ValidationError

from sam.voice.identity import FakeVoiceIdentityProvider
from sam.voice.models import (
    MAX_AUDIO_DURATION_SECONDS,
    AudioFormat,
    AudioInput,
    TranscriptionRequest,
    VoiceErrorCategory,
    VoiceProcessingRequest,
    VoiceProcessingStatus,
)
from sam.voice.transcription import FakeTranscriptionProvider
from tests.voice_support import ALICE, VoiceHarness, pcm_ramp, wav_input

S = VoiceProcessingStatus
C = VoiceErrorCategory


class _ThreadSpy:
    """Records every Thread started while active."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.started: list[str] = []
        original = threading.Thread.start
        spy = self

        def start(thread: threading.Thread) -> None:
            spy.started.append(thread.name)
            original(thread)

        monkeypatch.setattr(threading.Thread, "start", start)


class TestNoThreads:
    def test_no_thread_is_created_during_normal_transcription(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        h = VoiceHarness(identity=FakeVoiceIdentityProvider())
        sid = h.start()
        spy = _ThreadSpy(monkeypatch)
        result = h.gateway.process(h.request(sid))
        assert result.status is S.SUCCEEDED
        assert spy.started == []

    @pytest.mark.parametrize(
        "provider",
        [
            FakeTranscriptionProvider(delay_seconds=5.0),  # cooperative timeout
            FakeTranscriptionProvider(delay_seconds=0.12, honor_timeout=False),  # late
            FakeTranscriptionProvider(raises=RuntimeError("x")),
        ],
    )
    def test_no_thread_is_created_or_left_after_timeout_handling(
        self, provider: FakeTranscriptionProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        h = VoiceHarness(transcription=provider, timeout=0.05)
        sid = h.start()
        before = threading.active_count()
        spy = _ThreadSpy(monkeypatch)
        result = h.gateway.process(h.request(sid))
        assert result.status is S.FAILED
        assert spy.started == []
        assert threading.active_count() == before

    def test_identity_calls_create_no_threads_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        h = VoiceHarness(identity=FakeVoiceIdentityProvider(delay_seconds=60.0))
        sid = h.start()
        spy = _ThreadSpy(monkeypatch)
        before = threading.active_count()
        result = h.gateway.process(h.request(sid))
        assert result.status is S.SUCCEEDED  # identity degrades to unknown
        assert spy.started == [] and threading.active_count() == before

    def test_the_previous_daemon_worker_behaviour_no_longer_exists(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("sam.voice.timeout")
        import sam.voice.gateway as gateway
        import sam.voice.identity as identity
        import sam.voice.transcription as transcription

        for module in (gateway, identity, transcription):
            assert not hasattr(module, "run_with_timeout")
            assert not hasattr(module, "CallTimedOut")

    def test_no_worker_thread_named_like_the_old_helper_is_ever_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _ThreadSpy(monkeypatch)
        h = VoiceHarness(
            transcription=FakeTranscriptionProvider(delay_seconds=9.0), timeout=0.05
        )
        sid = h.start()
        h.gateway.process(h.request(sid))
        assert "sam-voice-call" not in spy.started


class TestTimeoutOwnership:
    @pytest.mark.parametrize("timeout", [0.05, 1.25, 7.0, 30.0])
    def test_provider_receives_exactly_the_configured_timeout(
        self, timeout: float
    ) -> None:
        h = VoiceHarness(timeout=timeout)
        sid = h.start()
        h.gateway.process(h.request(sid))
        assert h.stt.seen_timeouts == [timeout]  # type: ignore[attr-defined]

    def test_request_supplied_data_cannot_change_the_timeout(self) -> None:
        with pytest.raises(ValidationError):
            VoiceProcessingRequest.model_validate(
                {
                    "principal": ALICE,
                    "session_id": "s",
                    "audio": wav_input(),
                    "timeout_seconds": 9999,
                }
            )
        assert "timeout_seconds" not in TranscriptionRequest.model_fields
        with pytest.raises(ValidationError):
            AudioInput.model_validate(
                {
                    "content": b"x",
                    "declared_format": AudioFormat.WAV_PCM16,
                    "timeout": 9999,
                }
            )

    def test_audio_size_or_duration_does_not_influence_the_timeout(self) -> None:
        h = VoiceHarness(timeout=0.7)
        sid = h.start()
        short = h.request(sid, seed=1)
        long_audio = AudioInput(
            content=pcm_ramp(int(MAX_AUDIO_DURATION_SECONDS * 16_000)),
            declared_format=AudioFormat.RAW_PCM16LE,
            sample_rate=16_000,
            channels=1,
        )
        long_request = h.request(sid, audio=long_audio)
        assert h.gateway.process(short).status is S.SUCCEEDED
        assert h.gateway.process(long_request).status is S.SUCCEEDED
        assert h.stt.seen_timeouts == [0.7, 0.7]  # type: ignore[attr-defined]

    def test_provider_supplied_data_cannot_raise_the_timeout(self) -> None:
        provider = FakeTranscriptionProvider(
            {"text": "hi", "timeout": 9999, "timeout_seconds": 9999}
        )
        h = VoiceHarness(transcription=provider, timeout=0.7)
        sid = h.start()
        first = h.gateway.process(h.request(sid, seed=1))
        assert first.status is S.FAILED and first.error_category is C.INVALID_TRANSCRIPT
        h.gateway.process(h.request(sid, seed=2))
        assert provider.seen_timeouts == [0.7, 0.7]  # unchanged by anything it returned
        assert h.gateway._timeout == 0.7

    def test_a_result_after_the_deadline_is_discarded_however_it_arrives(self) -> None:
        provider = FakeTranscriptionProvider(
            "late", delay_seconds=0.15, honor_timeout=False
        )
        h = VoiceHarness(transcription=provider, timeout=0.05)
        sid = h.start()
        result = h.gateway.process(h.request(sid))
        assert (
            result.error_category is C.TRANSCRIPTION_TIMEOUT
            and result.transcript is None
        )

    def test_the_gateway_rejects_an_unbounded_timeout(self) -> None:
        from sam.voice.gateway import VoiceGateway

        h = VoiceHarness()
        for bad in (0, -1, 31, float("inf"), float("nan")):
            with pytest.raises(ValueError):
                VoiceGateway(
                    permission_engine=h.pengine,
                    transcription_provider=h.stt,
                    provider_timeout_seconds=bad,
                )


class TestExactlyOnceAndNoRetry:
    @pytest.mark.parametrize(
        "provider",
        [
            FakeTranscriptionProvider(),
            FakeTranscriptionProvider(delay_seconds=5.0),
            FakeTranscriptionProvider(delay_seconds=0.12, honor_timeout=False),
            FakeTranscriptionProvider(raises=RuntimeError("x")),
            FakeTranscriptionProvider(raises=TimeoutError()),
            FakeTranscriptionProvider(result=b"bytes"),
        ],
    )
    def test_one_request_invokes_the_provider_exactly_once(
        self, provider: FakeTranscriptionProvider
    ) -> None:
        h = VoiceHarness(transcription=provider, timeout=0.05)
        sid = h.start()
        h.gateway.process(h.request(sid))
        assert provider.call_count == 1

    def test_a_timeout_does_not_retry_and_the_slot_stays_consumed(self) -> None:
        provider = FakeTranscriptionProvider(delay_seconds=5.0)
        h = VoiceHarness(transcription=provider, timeout=0.05)
        sid = h.start()
        first = h.gateway.process(h.request(sid, utterance_id="u1"))
        again = h.gateway.process(h.request(sid, utterance_id="u1"))
        assert first.error_category is C.TRANSCRIPTION_TIMEOUT
        assert again.error_category is C.DUPLICATE_UTTERANCE
        assert provider.call_count == 1


class TestTimeoutResultsAreContentFree:
    MARK = "SPOKENMARKER"

    def test_timeout_result_contains_no_transcript_or_audio(self) -> None:
        provider = FakeTranscriptionProvider(
            lambda r: self.MARK + "LATE", delay_seconds=0.15, honor_timeout=False
        )
        h = VoiceHarness(transcription=provider, timeout=0.05)
        sid = h.start()
        audio = AudioInput(
            content=(b"RAWAUDIOMARK!!" * 200)[:2800],
            declared_format=AudioFormat.RAW_PCM16LE,
            sample_rate=16_000,
            channels=1,
        )
        result = h.gateway.process(h.request(sid, audio=audio))
        blob = repr(result) + result.model_dump_json()
        blob += "\n".join(e.model_dump_json() for e in h.audit.list_events())  # type: ignore[attr-defined]
        assert result.transcript is None
        assert self.MARK not in blob and "RAWAUDIOMARK" not in blob

    def test_provider_exception_content_never_reaches_results_or_audit(self) -> None:
        leak = f"{self.MARK} RAWAUDIOMARK sk-ant-abcdefghijklmnopqrst"
        provider = FakeTranscriptionProvider(raises=RuntimeError(leak))
        h = VoiceHarness(transcription=provider)
        sid = h.start()
        result = h.gateway.process(h.request(sid))
        blob = repr(result) + result.model_dump_json()
        blob += "\n".join(e.model_dump_json() for e in h.audit.list_events())  # type: ignore[attr-defined]
        blob += "\n".join(str(e) for e in h.permission_audit.list_events())
        assert result.error_category is C.TRANSCRIPTION_ERROR
        for fragment in (self.MARK, "RAWAUDIOMARK", "sk-ant"):
            assert fragment not in blob

    def test_provider_exception_carries_no_cause_or_message(self) -> None:
        from sam.voice.errors import TranscriptionError
        from sam.voice.transcription import transcribe

        request = TranscriptionRequest(
            session_id="s",
            utterance_id="u",
            audio=__import__(
                "sam.voice.audio", fromlist=["validate_audio"]
            ).validate_audio(wav_input()),
        )
        provider = FakeTranscriptionProvider(raises=RuntimeError("secret detail"))
        with pytest.raises(TranscriptionError) as info:
            transcribe(provider, request, timeout_seconds=1.0)
        assert "secret" not in str(info.value)
        assert info.value.__cause__ is None and info.value.__suppress_context__ is True
