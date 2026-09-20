"""The OPTIONAL Gemini Persian TTS provider, driven only by httpx.MockTransport.

No live network, no real key, no billing. Verifies the exact outbound request
(current Interactions API), endpoint/model/voice pinning, the key only in a
header, no redirects/env, ONE request and NO retry, bounds, PCM -> WAV,
secret text -> zero calls, disabled-by-default wiring and that there is no
paid or cross-provider fallback.
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sam.core.config import Settings
from sam.desktop.runtime import (
    PERSIAN_SPEECH_PROFILE,
    build_desktop_runtime,
    gemini_speech_from_settings,
)
from sam.tts import gemini_tts
from sam.tts.credentials import FakeTTSCredentialProvider, TTSCredentialReference
from sam.tts.errors import (
    TTSAuthenticationError,
    TTSCredentialError,
    TTSError,
    TTSInvalidAudioError,
    TTSOutputTooLargeError,
    TTSProviderError,
    TTSRateLimitError,
    TTSTimeoutError,
    TTSValidationError,
)
from sam.tts.gemini_tts import (
    GEMINI_INTERACTIONS_URL,
    GEMINI_PROVIDER_ID,
    GEMINI_TTS_MODEL,
    GeminiTTSProvider,
    build_client,
    pcm16_to_wav,
)
from sam.tts.models import (
    MAX_TTS_AUDIO_BYTES,
    ProviderSynthesisRequest,
    TrustedVoiceProfile,
    TTSAudioFormat,
    TTSErrorCategory,
    TTSStatus,
)
from tests.desktop_support import Bridge
from tests.tts_support import Recorder, TTSHarness

KEY = "FAKE-GEMINI-KEY-0001-not-a-real-key"
REF = TTSCredentialReference(
    provider_id=GEMINI_PROVIDER_ID, credential_id="gemini-main"
)
PCM = b"\x01\x00\x02\x00" * 600  # synthetic 16-bit mono samples
PERSIAN = "سلام سام، این API با FastAPI کار می‌کند."


def ok_body(pcm: bytes = PCM) -> dict[str, Any]:
    return {"output_audio": {"data": base64.b64encode(pcm).decode()}}


def json_response(body: Any = None, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json"},
        content=json.dumps(ok_body() if body is None else body).encode(),
    )


def provider_for(handler: Any, key: str = KEY) -> GeminiTTSProvider:
    creds = FakeTTSCredentialProvider()
    creds.add(REF, key)
    return GeminiTTSProvider(
        credentials=creds,
        credential_reference=REF,
        transport=httpx.MockTransport(handler),
    )


def req(**overrides: Any) -> ProviderSynthesisRequest:
    fields: dict[str, Any] = dict(
        synthesis_id="s1",
        text=PERSIAN,
        voice_reference="Kore",
        model=GEMINI_TTS_MODEL,
        output_format=TTSAudioFormat.WAV,
    )
    fields.update(overrides)
    return ProviderSynthesisRequest(**fields)


# ---------------------------------------------------------- request shape


def test_request_matches_the_documented_interactions_schema() -> None:
    rec = Recorder(json_response())
    result = provider_for(rec).synthesize(req(), timeout_seconds=5)
    (sent,) = rec.requests
    assert sent.method == "POST" and str(sent.url) == GEMINI_INTERACTIONS_URL
    assert sent.url.query == b"" and KEY not in str(sent.url)
    assert sent.headers["x-goog-api-key"] == KEY
    assert "authorization" not in sent.headers
    assert sent.headers["content-type"] == "application/json"
    body = json.loads(sent.content)
    assert body == {
        "model": "gemini-3.1-flash-tts-preview",
        "input": PERSIAN,
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": "Kore"}]},
    }
    assert KEY not in sent.content.decode()
    assert result.content_type == "audio/wav"


def test_pcm_is_wrapped_into_a_canonical_24khz_mono_16bit_wav() -> None:
    result = provider_for(Recorder(json_response())).synthesize(
        req(), timeout_seconds=5
    )
    wav = result.audio_bytes
    assert wav[:4] == b"RIFF" and wav[8:16] == b"WAVEfmt "
    fmt, channels, rate, byte_rate, align, bits = struct.unpack("<HHIIHH", wav[20:36])
    assert (fmt, channels, rate, byte_rate, align, bits) == (
        1,
        1,
        24_000,
        48_000,
        2,
        16,
    )
    assert wav[36:40] == b"data" and wav[44:] == PCM
    assert struct.unpack("<I", wav[40:44])[0] == len(PCM)
    assert pcm16_to_wav(PCM) == wav


# ------------------------------------------ pinning / no override / no env


def test_model_voice_and_format_come_only_from_the_trusted_request() -> None:
    rec = Recorder(json_response())
    p = provider_for(rec)
    for bad in (
        dict(model="gemini-2.5-pro-preview-tts"),
        dict(model="gemini-3.1-flash-tts-preview-x"),
        dict(voice_reference="kore"),
        dict(voice_reference="Kore; DROP"),
        dict(output_format=TTSAudioFormat.MP3),
    ):
        with pytest.raises(TTSValidationError):
            p.synthesize(req(**bad), timeout_seconds=5)
    assert rec.requests == []  # refused before the key is resolved or any call


def test_no_endpoint_parameter_and_the_client_is_hardened() -> None:
    import inspect

    params = inspect.signature(GeminiTTSProvider.__init__).parameters
    assert set(params) == {"self", "credentials", "credential_reference", "transport"}
    client = build_client(None, 5.0)
    assert client.follow_redirects is False and client.trust_env is False
    assert gemini_tts.GEMINI_HOST == "generativelanguage.googleapis.com"


def test_ambient_proxy_env_cannot_redirect_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.invalid:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent")
    rec = Recorder(json_response())
    provider_for(rec).synthesize(req(), timeout_seconds=5)
    assert len(rec.requests) == 1
    assert build_client(None, 5.0).trust_env is False


def test_a_redirect_is_refused_and_never_followed() -> None:
    rec = Recorder(
        httpx.Response(302, headers={"location": "https://evil.invalid/steal"})
    )
    with pytest.raises(TTSProviderError):
        provider_for(rec).synthesize(req(), timeout_seconds=5)
    assert len(rec.requests) == 1


# --------------------------------------- one request, NO retry, safe errors


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (429, TTSRateLimitError),
        (401, TTSAuthenticationError),
        (403, TTSAuthenticationError),
        (500, TTSProviderError),
        (503, TTSProviderError),
        (400, TTSProviderError),
    ],
)
def test_error_statuses_map_to_safe_errors_with_exactly_one_call(
    status: int, error: type[TTSError]
) -> None:
    rec = Recorder(
        httpx.Response(status, content=f"provider text {KEY} {PERSIAN}".encode())
    )
    with pytest.raises(error) as caught:
        provider_for(rec).synthesize(req(), timeout_seconds=5)
    assert len(rec.requests) == 1  # a 5xx is NEVER retried
    assert KEY not in str(caught.value) and "provider text" not in str(caught.value)


def test_timeout_is_typed_and_not_retried() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    rec = Recorder(slow)
    with pytest.raises(TTSTimeoutError):
        provider_for(rec).synthesize(req(), timeout_seconds=1)
    assert len(rec.requests) == 1


def test_transport_failures_are_generic() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {KEY}", request=request)

    with pytest.raises(TTSProviderError) as caught:
        provider_for(Recorder(boom)).synthesize(req(), timeout_seconds=5)
    assert KEY not in str(caught.value) and caught.value.__cause__ is None


def test_missing_credential_makes_no_request() -> None:
    rec = Recorder(json_response())
    provider = GeminiTTSProvider(
        credentials=FakeTTSCredentialProvider(),
        credential_reference=REF,
        transport=httpx.MockTransport(rec),
    )
    with pytest.raises(TTSCredentialError):
        provider.synthesize(req(), timeout_seconds=5)
    assert rec.requests == []


# ------------------------------------------------------ hostile responses


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"output_audio": {}},
        {"output_audio": {"data": ""}},
        {"output_audio": {"data": 123}},
        {"output_audio": {"data": "###not-base64###"}},
        {"output_audio": {"data": base64.b64encode(b"\x01\x02\x03").decode()}},
        {"output_audio": "x"},
        [],
    ],
)
def test_malformed_or_empty_audio_is_rejected(body: Any) -> None:
    with pytest.raises(TTSInvalidAudioError):
        provider_for(Recorder(json_response(body))).synthesize(req(), timeout_seconds=5)


def test_non_json_body_is_rejected() -> None:
    with pytest.raises(TTSInvalidAudioError):
        provider_for(Recorder(httpx.Response(200, content=b"<html>"))).synthesize(
            req(), timeout_seconds=5
        )


def test_oversize_output_is_bounded() -> None:
    too_big = b"\x00\x01" * (MAX_TTS_AUDIO_BYTES // 2)  # + 44-byte header > cap
    with pytest.raises(TTSOutputTooLargeError):
        provider_for(Recorder(json_response(ok_body(too_big)))).synthesize(
            req(), timeout_seconds=5
        )
    rec = Recorder(
        httpx.Response(200, headers={"content-length": str(50 * 1024 * 1024)})
    )
    with pytest.raises(TTSOutputTooLargeError):
        provider_for(rec).synthesize(req(), timeout_seconds=5)


def test_a_compressed_response_is_refused() -> None:
    rec = Recorder(httpx.Response(200, headers={"content-encoding": "gzip"}))
    with pytest.raises(TTSInvalidAudioError):
        provider_for(rec).synthesize(req(), timeout_seconds=5)


# ------------------------------------------------ through the real gateway


def _persian_profile() -> TrustedVoiceProfile:
    return TrustedVoiceProfile(
        profile_id=PERSIAN_SPEECH_PROFILE,
        provider_id=GEMINI_PROVIDER_ID,
        provider_voice_reference="Kore",
        provider_model=GEMINI_TTS_MODEL,
        output_format=TTSAudioFormat.WAV,
    )


def _gateway_harness(rec: Recorder) -> TTSHarness:
    h = TTSHarness(provider=provider_for(rec), extra_profiles=(_persian_profile(),))
    h.grant(PERSIAN_SPEECH_PROFILE)
    return h


def test_gateway_returns_validated_wav_and_one_call() -> None:
    rec = Recorder(json_response())
    h = _gateway_harness(rec)
    result = h.gateway.synthesize(h.request(PERSIAN, profile_id=PERSIAN_SPEECH_PROFILE))
    assert result.status is TTSStatus.SUCCEEDED and result.audio is not None
    assert result.audio.format is TTSAudioFormat.WAV
    assert result.audio.provider_id == GEMINI_PROVIDER_ID
    assert result.audio.audio_bytes[:4] == b"RIFF" and len(rec.requests) == 1


def test_secret_text_makes_zero_network_calls() -> None:
    rec = Recorder(json_response())
    h = _gateway_harness(rec)
    result = h.gateway.synthesize(
        h.request(
            "کلید من sk-ant-abcdefghijklmnopqrst1234 است",
            profile_id=PERSIAN_SPEECH_PROFILE,
        )
    )
    assert result.status is not TTSStatus.SUCCEEDED
    assert result.error_category is TTSErrorCategory.SECRET_DETECTED
    assert rec.requests == []


def test_quota_exhausted_is_a_safe_result_not_a_fallback() -> None:
    rec = Recorder(httpx.Response(429))
    h = _gateway_harness(rec)
    result = h.gateway.synthesize(h.request(PERSIAN, profile_id=PERSIAN_SPEECH_PROFILE))
    assert result.status is TTSStatus.FAILED and result.audio is None
    assert result.error_category is TTSErrorCategory.RATE_LIMITED
    assert len(rec.requests) == 1  # no retry, no other provider


def test_gateway_wav_validation_rejects_a_malformed_container() -> None:
    from sam.tts.audio import validate_audio_bytes

    good = pcm16_to_wav(PCM)
    validate_audio_bytes(good, TTSAudioFormat.WAV)
    for bad in (good[:-1], b"RIFF" + good[4:20] + b"\x00" * 30, b"ID3" + good):
        with pytest.raises(TTSInvalidAudioError):
            validate_audio_bytes(bad, TTSAudioFormat.WAV)


# ------------------------------------- optional / disabled by default wiring


def test_disabled_by_default_and_needs_a_local_key() -> None:
    assert Settings(_env_file=None).gemini_api_key is None  # type: ignore[call-arg]
    assert gemini_speech_from_settings(Settings(_env_file=None)) is None  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        Settings(gemini_api_key=SecretStr("   "), _env_file=None)  # type: ignore[call-arg]


def test_no_key_means_no_persian_voice_and_persian_stays_text_only() -> None:
    b = Bridge()  # no Gemini key configured
    assert b.runtime.speech_boundary is None or all(
        "fa" not in langs for langs in b.runtime.speech_profile_languages.values()
    )


def test_configured_key_builds_one_trusted_persian_profile() -> None:
    settings = Settings(
        gemini_api_key=SecretStr(KEY),
        gemini_tts_voice="Puck",
        _env_file=None,  # type: ignore[call-arg]
    )
    built = gemini_speech_from_settings(settings)
    assert built is not None
    provider, profile = built
    assert provider.provider_id == GEMINI_PROVIDER_ID
    assert (
        profile.profile_id,
        profile.provider_model,
        profile.provider_voice_reference,
    ) == (
        PERSIAN_SPEECH_PROFILE,
        GEMINI_TTS_MODEL,
        "Puck",
    )
    assert profile.output_format is TTSAudioFormat.WAV
    assert KEY not in repr(provider) and KEY not in repr(profile)


def test_runtime_marks_the_gemini_profile_persian_only_and_keeps_fish_english() -> None:
    from tests.desktop_support import StubAgent

    settings = Settings(gemini_api_key=SecretStr(KEY), _env_file=None)  # type: ignore[call-arg]
    runtime = build_desktop_runtime(settings, StubAgent(), agent_configured=True)  # type: ignore[arg-type]
    assert runtime.speech_boundary is not None
    assert runtime.speech_profile_languages[PERSIAN_SPEECH_PROFILE] == {"fa"}
    assert runtime.speech_provider_id == GEMINI_PROVIDER_ID


def test_a_duplicate_provider_id_is_refused() -> None:
    from sam.tts.gateway import TTSGateway

    h = TTSHarness()
    with pytest.raises(ValueError):
        TTSGateway(
            permission_engine=h.pengine,
            provider=h.provider,
            extra_providers=(h.provider,),
            profiles=h.profiles,
        )


# --------------------------------------------------- static cost invariants

SRC = Path(gemini_tts.__file__)


def test_module_has_no_retry_billing_fallback_or_environment_access() -> None:
    import ast

    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    tree.body = [
        n
        for n in tree.body
        if not (
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        )
    ]  # drop the module docstring: only executable code is scanned
    code = ast.unparse(tree).lower()
    for token in (
        "os.environ",
        "getenv",
        "while true",
        "for attempt",
        "time.sleep",
        "fish",
        "billing",
        "upgrade",
        "?key=",
        "&key=",
        "api_key=",
    ):
        assert token not in code, token
    assert code.count("client.stream(") == 1  # a single outbound call site


def test_no_paid_fallback_setting_or_code_path_exists() -> None:
    settings_fields = set(Settings.model_fields)
    assert not {f for f in settings_fields if "fallback" in f or "billing" in f}
    runtime_src = (
        Path(gemini_tts.__file__).parents[1] / "desktop" / "runtime.py"
    ).read_text()
    assert "PAID_FALLBACK" not in runtime_src
