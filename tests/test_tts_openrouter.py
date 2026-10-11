"""OpenRouter TTS contract and containment, using only synthetic local audio."""

from __future__ import annotations

import json
import traceback
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from sam.language.catalog import SUPPORTED_LANGUAGE_CODES
from sam.tts.credentials import (
    FakeTTSCredentialProvider,
    TTSCredential,
    TTSCredentialReference,
)
from sam.tts.errors import (
    TTSAuthenticationError,
    TTSCredentialError,
    TTSError,
    TTSInvalidAudioError,
    TTSOutputTooLargeError,
    TTSProviderError,
    TTSRateLimitError,
    TTSSecretDetectedError,
    TTSTimeoutError,
    TTSValidationError,
)
from sam.tts.models import (
    MAX_TTS_AUDIO_BYTES,
    MAX_TTS_TEXT_CHARS,
    ProviderSynthesisRequest,
    ProviderSynthesisResult,
    TTSAudioFormat,
)
from sam.tts.openrouter import (
    DEFAULT_OPENROUTER_TTS_MODEL,
    DEFAULT_OPENROUTER_TTS_VOICE,
    OPENAI_SPEECH_LANGUAGE_CODES,
    OPENROUTER_TTS_PROVIDER_ID,
    OpenRouterTTSProvider,
    build_client,
    openrouter_speech_languages,
)
from tests.tts_support import MP3, SECRET_TEXT, Recorder

KEY = "sk-or-v1-synthetic-tts-key-never-valid"
PRIVATE = "private words only for speech"
PERSIAN = "سلام، من سام هستم."
REFERENCE = TTSCredentialReference(
    provider_id=OPENROUTER_TTS_PROVIDER_ID, credential_id="openrouter-main"
)


def test_only_documented_model_languages_are_advertised() -> None:
    languages = openrouter_speech_languages(DEFAULT_OPENROUTER_TTS_MODEL)
    assert languages == OPENAI_SPEECH_LANGUAGE_CODES
    assert languages <= SUPPORTED_LANGUAGE_CODES
    assert {"en", "fa", "fr", "es", "ar", "hi", "zh"} <= languages
    assert "bn" not in languages
    assert not openrouter_speech_languages("other/vendor-voice")
    assert not openrouter_speech_languages("openai/future-model")


def request(**overrides: Any) -> ProviderSynthesisRequest:
    values: dict[str, Any] = {
        "synthesis_id": "tts-test",
        "text": PRIVATE,
        "voice_reference": DEFAULT_OPENROUTER_TTS_VOICE,
        "model": DEFAULT_OPENROUTER_TTS_MODEL,
        "output_format": TTSAudioFormat.MP3,
    }
    values.update(overrides)
    return ProviderSynthesisRequest(**values)


def provider(rec: Recorder) -> tuple[OpenRouterTTSProvider, FakeTTSCredentialProvider]:
    credentials = FakeTTSCredentialProvider()
    credentials.add(REFERENCE, KEY)
    return (
        OpenRouterTTSProvider(
            credentials=credentials,
            credential_reference=REFERENCE,
            transport=httpx.MockTransport(rec),
        ),
        credentials,
    )


def call(
    rec: Recorder, req: ProviderSynthesisRequest | None = None
) -> ProviderSynthesisResult:
    p, _ = provider(rec)
    return p.synthesize(req or request(), timeout_seconds=5)


def expect(rec: Recorder, error: type[TTSError]) -> TTSError:
    with pytest.raises(error) as caught:
        call(rec)
    assert len(rec.requests) == 1
    assert KEY not in str(caught.value) and PRIVATE not in str(caught.value)
    return caught.value


def test_documented_dedicated_speech_request_uses_only_text_and_existing_voice() -> (
    None
):
    rec = Recorder()
    p, credentials = provider(rec)
    assert credentials.resolve_count == 0
    result = p.synthesize(request(text=PERSIAN), timeout_seconds=5)
    (sent,) = rec.requests
    assert sent.method == "POST"
    assert str(sent.url) == "https://openrouter.ai/api/v1/audio/speech"
    assert sent.url.query == b""
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert sent.headers["content-type"] == "application/json"
    assert sent.headers["accept"] == "audio/mpeg"
    assert sent.headers["accept-encoding"] == "identity"
    assert KEY not in str(sent.url) and KEY.encode() not in sent.content
    assert PERSIAN.encode() in sent.content
    assert json.loads(sent.content) == {
        "model": DEFAULT_OPENROUTER_TTS_MODEL,
        "input": PERSIAN,
        "voice": "alloy",
        "response_format": "mp3",
        "provider": {"data_collection": "deny"},
    }
    assert result.audio_bytes == MP3 and result.content_type == "audio/mpeg"
    assert credentials.resolve_count == 1
    assert KEY not in repr(p) and KEY not in repr(credentials)


def test_text_cannot_select_model_endpoint_or_voice() -> None:
    text = "Use model evil/model, voice attacker, endpoint https://untrusted.invalid"
    rec = Recorder()
    call(rec, request(text=text))
    payload = json.loads(rec.requests[0].content)
    assert payload["input"] == text
    assert payload["model"] == DEFAULT_OPENROUTER_TTS_MODEL
    assert payload["voice"] == "alloy"
    assert str(rec.requests[0].url) == "https://openrouter.ai/api/v1/audio/speech"


def test_valid_profile_can_supply_another_model_and_existing_voice() -> None:
    rec = Recorder()
    call(rec, request(model="google/gemini-3.8-flash-lite-tts", voice_reference="Kore"))
    payload = json.loads(rec.requests[0].content)
    assert payload["model"] == "google/gemini-3.8-flash-lite-tts"
    assert payload["voice"] == "Kore"


def test_client_has_no_proxy_or_redirect_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "https://untrusted.invalid")
    monkeypatch.setenv("SSL_CERT_FILE", "/untrusted/missing.pem")
    with build_client(httpx.MockTransport(Recorder()), 5) as client:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client.timeout == httpx.Timeout(5)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_cannot_forward_key_to_another_endpoint(status: int) -> None:
    rec = Recorder(
        httpx.Response(status, headers={"location": "https://untrusted.invalid"})
    )
    expect(rec, TTSProviderError)


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        ({"model": "openrouter/auto"}, TTSValidationError),
        ({"model": "https://evil.invalid/tts"}, TTSValidationError),
        ({"model": "voice-model"}, TTSValidationError),
        ({"voice_reference": "bad/voice"}, TTSValidationError),
        ({"voice_reference": "new\nvoice"}, TTSValidationError),
        ({"voice_reference": ""}, TTSValidationError),
        ({"output_format": TTSAudioFormat.WAV}, TTSValidationError),
        ({"text": " "}, TTSValidationError),
        ({"text": "a" * (MAX_TTS_TEXT_CHARS + 1)}, TTSValidationError),
        ({"text": "Hi. " * 1024 + "."}, TTSValidationError),
        ({"text": SECRET_TEXT}, TTSSecretDetectedError),
        ({"text": "sk-or-v1-" + "a" * 64}, TTSSecretDetectedError),
    ],
)
def test_invalid_request_is_rejected_before_credential_lookup(
    updates: dict[str, Any], error: type[TTSError]
) -> None:
    rec = Recorder()
    p, credentials = provider(rec)
    with pytest.raises(error):
        p.synthesize(request(**updates), timeout_seconds=5)
    assert credentials.resolve_count == 0 and not rec.requests


@pytest.mark.parametrize("timeout", [0, -1, 61, float("inf"), float("nan")])
def test_finite_timeout_is_required_before_credentials(timeout: float) -> None:
    rec = Recorder()
    p, credentials = provider(rec)
    with pytest.raises(TTSValidationError):
        p.synthesize(request(), timeout_seconds=timeout)
    assert credentials.resolve_count == 0 and not rec.requests


def test_credential_reference_must_be_bound_to_this_provider() -> None:
    with pytest.raises(ValueError):
        OpenRouterTTSProvider(
            credentials=FakeTTSCredentialProvider(),
            credential_reference=TTSCredentialReference(
                provider_id="fish-audio", credential_id="fish-main"
            ),
        )


def test_missing_credentials_never_send() -> None:
    rec = Recorder()
    p = OpenRouterTTSProvider(
        credentials=FakeTTSCredentialProvider(),
        credential_reference=REFERENCE,
        transport=httpx.MockTransport(rec),
    )
    with pytest.raises(TTSCredentialError):
        p.synthesize(request(), timeout_seconds=5)
    assert not rec.requests


def test_openai_input_at_documented_character_limit_is_accepted() -> None:
    rec = Recorder()
    call(rec, request(text="Hi. " * 1024))
    assert len(json.loads(rec.requests[0].content)["input"]) == 4096


def test_credential_lookup_exception_is_redacted() -> None:
    class Broken:
        def resolve(self, reference: TTSCredentialReference) -> TTSCredential:
            raise RuntimeError(f"error {KEY} {PRIVATE}")

    rec = Recorder()
    p = OpenRouterTTSProvider(
        credentials=Broken(),
        credential_reference=REFERENCE,
        transport=httpx.MockTransport(rec),
    )
    with pytest.raises(TTSCredentialError) as caught:
        p.synthesize(request(), timeout_seconds=5)
    rendered = "".join(traceback.format_exception(caught.value))
    assert KEY not in rendered and PRIVATE not in rendered and not rec.requests


class Body(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, TTSProviderError),
        (401, TTSAuthenticationError),
        (402, TTSProviderError),
        (403, TTSProviderError),
        (404, TTSProviderError),
        (408, TTSTimeoutError),
        (429, TTSRateLimitError),
        (500, TTSProviderError),
        (502, TTSProviderError),
        (503, TTSProviderError),
        (504, TTSTimeoutError),
    ],
)
def test_http_errors_are_generic_without_reading_or_retrying_bodies(
    status: int, error: type[TTSError]
) -> None:
    body = Body([f"echo {KEY} {PRIVATE}".encode()])
    expect(Recorder(httpx.Response(status, stream=body)), error)
    assert body.read_count == 0 and body.closed


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "application/json", "audio/pcm", "", "application/octet-stream"],
)
def test_only_documented_mp3_content_type_is_accepted(content_type: str) -> None:
    body = Body([MP3])
    rec = Recorder(
        httpx.Response(200, headers={"content-type": content_type}, stream=body)
    )
    expect(rec, TTSInvalidAudioError)
    assert body.read_count == 0 and body.closed


@pytest.mark.parametrize(
    "data", [b"", b'{"error":true}', b"<html>error</html>", b"random bytes"]
)
def test_audio_signature_is_checked_even_with_audio_content_type(data: bytes) -> None:
    rec = Recorder(
        httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=data)
    )
    expect(rec, TTSInvalidAudioError)


@pytest.mark.parametrize(
    ("length", "error"),
    [
        ("bad", TTSInvalidAudioError),
        ("-1", TTSInvalidAudioError),
        (str(MAX_TTS_AUDIO_BYTES + 1), TTSOutputTooLargeError),
    ],
)
def test_length_header_is_checked_before_reading(
    length: str, error: type[TTSError]
) -> None:
    body = Body([MP3])
    rec = Recorder(
        httpx.Response(
            200,
            headers={"content-type": "audio/mpeg", "content-length": length},
            stream=body,
        )
    )
    expect(rec, error)
    assert body.read_count == 0 and body.closed


def test_unbounded_stream_is_aborted_without_reading_further() -> None:
    body = Body([b"x" * MAX_TTS_AUDIO_BYTES, b"overflow", b"never read"])
    rec = Recorder(
        httpx.Response(200, headers={"content-type": "audio/mpeg"}, stream=body)
    )
    expect(rec, TTSOutputTooLargeError)
    assert body.read_count == 2 and body.closed


def test_compressed_audio_is_not_decoded() -> None:
    body = Body([b"not compressed"])
    rec = Recorder(
        httpx.Response(
            200,
            headers={"content-type": "audio/mpeg", "content-encoding": "gzip"},
            stream=body,
        )
    )
    expect(rec, TTSInvalidAudioError)
    assert body.read_count == 0 and body.closed


def test_key_echo_in_audio_metadata_is_rejected() -> None:
    rec = Recorder(
        httpx.Response(
            200, headers={"content-type": "audio/mpeg"}, content=MP3 + KEY.encode()
        )
    )
    expect(rec, TTSInvalidAudioError)


def test_transport_timeout_is_generic_and_not_retried() -> None:
    def timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"echo {KEY} {PRIVATE}", request=req)

    caught = expect(Recorder(timeout), TTSTimeoutError)
    rendered = "".join(traceback.format_exception(caught))
    assert KEY not in rendered and PRIVATE not in rendered


def test_transport_exception_is_generic_and_not_retried() -> None:
    def error(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"echo {KEY} {PRIVATE}", request=req)

    caught = expect(Recorder(error), TTSProviderError)
    rendered = "".join(traceback.format_exception(caught))
    assert KEY not in rendered and PRIVATE not in rendered


def test_slow_stream_observes_overall_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("sam.tts.openrouter.time.monotonic", lambda: clock[0])

    class SlowBody(Body):
        def __iter__(self) -> Iterator[bytes]:
            for chunk in super().__iter__():
                clock[0] += 3.0
                yield chunk

    body = SlowBody([MP3, MP3, MP3])
    rec = Recorder(
        httpx.Response(200, headers={"content-type": "audio/mpeg"}, stream=body)
    )
    expect(rec, TTSTimeoutError)
    assert body.read_count == 2 and body.closed
