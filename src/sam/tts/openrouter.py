"""OpenRouter speech synthesis through its dedicated audio/speech endpoint.

One text-only request using an existing voice. The gateway owns permissions
and the trusted voice profile; this transport resolves its provider-bound key
only after local validation. No cloning, reference recordings, redirects,
ambient proxies, or application retries are supported.

Official schema verified 2026-10-11:
https://openrouter.ai/docs/guides/overview/multimodal/tts
"""

from __future__ import annotations

import json
import math
import re
import time

import httpx

from sam.models.openrouter_config import validate_openrouter_model_id
from sam.tts.audio import validate_audio_bytes
from sam.tts.credentials import TTSCredentialProvider, TTSCredentialReference
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
from sam.tts.models import (
    MAX_TTS_AUDIO_BYTES,
    MAX_TTS_TIMEOUT_SECONDS,
    ProviderSynthesisRequest,
    ProviderSynthesisResult,
    TTSAudioFormat,
    TTSErrorCategory,
)
from sam.tts.validation import validate_text

OPENROUTER_TTS_PROVIDER_ID = "openrouter-tts"
OPENROUTER_TTS_URL = "https://openrouter.ai/api/v1/audio/speech"
DEFAULT_OPENROUTER_TTS_MODEL = "openai/gpt-4o-mini-tts-2025-12-15"
DEFAULT_OPENROUTER_TTS_VOICE = "alloy"
# Reviewed provider capability, not a claim about every OpenRouter audio model.
# https://developers.openai.com/api/docs/guides/text-to-speech#supported-languages
OPENAI_SPEECH_LANGUAGE_CODES = frozenset(
    "af ar hy az be bs bg ca zh hr cs da nl en et fi fr gl de el he hi hu "
    "is id it ja kn kk ko lv lt mk ms mr mi ne no fa pl pt ro ru sr sk sl "
    "es sw sv tl ta th tr uk ur vi cy".split()
)
_DOCUMENTED_MULTILINGUAL_MODELS = frozenset(
    {
        DEFAULT_OPENROUTER_TTS_MODEL,
        "openai/gpt-4o-mini-tts",
        "openai/tts-1",
        "openai/tts-1-hd",
    }
)
_VOICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create
_OPENAI_MAX_INPUT_CHARS = 4096


def openrouter_speech_languages(model_id: str) -> frozenset[str]:
    """Advertise only languages reviewed for the configured speech model."""

    return (
        OPENAI_SPEECH_LANGUAGE_CODES
        if model_id in _DOCUMENTED_MULTILINGUAL_MODELS
        else frozenset()
    )


def build_client(
    transport: httpx.BaseTransport | None, timeout_seconds: float
) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(timeout_seconds),
        trust_env=False,
        follow_redirects=False,
        http2=False,
    )


class OpenRouterTTSProvider:
    provider_id = OPENROUTER_TTS_PROVIDER_ID

    def __init__(
        self,
        *,
        credentials: TTSCredentialProvider,
        credential_reference: TTSCredentialReference,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if credential_reference.provider_id != self.provider_id:
            raise ValueError("credential reference is not for this provider")
        self._credentials = credentials
        self._reference = credential_reference
        self._transport = transport

    def __repr__(self) -> str:
        return f"OpenRouterTTSProvider(provider_id={self.provider_id!r})"

    def synthesize(
        self, request: ProviderSynthesisRequest, *, timeout_seconds: float
    ) -> ProviderSynthesisResult:
        if (
            not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= MAX_TTS_TIMEOUT_SECONDS
        ):
            raise TTSValidationError("timeout is out of bounds")
        try:
            validate_openrouter_model_id(request.model)
        except ValueError:
            raise TTSValidationError("voice model is malformed") from None
        if _VOICE_RE.fullmatch(request.voice_reference) is None:
            raise TTSValidationError("voice reference is malformed")
        if request.output_format is not TTSAudioFormat.MP3:
            raise TTSValidationError("output format is not supported")
        text = validate_text(request.text)
        if request.model.startswith("openai/") and len(text) > _OPENAI_MAX_INPUT_CHARS:
            raise TTSValidationError(
                "text exceeds the voice model input limit",
                category=TTSErrorCategory.TEXT_TOO_LARGE,
            )
        body = json.dumps(
            {
                "model": request.model,
                "input": text,
                "voice": request.voice_reference,
                "response_format": "mp3",
                # Speech supports data policies but not chat's upstream routing
                # preferences. Do not claim allow_fallbacks is enforced here.
                "provider": {"data_collection": "deny"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            credential = self._credentials.resolve(self._reference)
        except Exception:
            raise TTSCredentialError("credential is not available") from None
        headers = {
            "Authorization": f"Bearer {credential.reveal()}",
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
            "Accept-Encoding": "identity",
            "User-Agent": "sam-tts/0.1",
            "X-OpenRouter-Title": "Sam",
        }
        deadline = time.monotonic() + timeout_seconds
        try:
            with build_client(self._transport, timeout_seconds) as client:
                with client.stream(
                    "POST", OPENROUTER_TTS_URL, headers=headers, content=body
                ) as response:
                    audio = _read_audio(response, deadline)
        except TTSError:
            raise
        except httpx.TimeoutException:
            raise TTSTimeoutError("speech synthesis timed out") from None
        except Exception:
            raise TTSProviderError("speech synthesis request failed") from None
        if credential.reveal().encode("utf-8") in audio:
            raise TTSInvalidAudioError("provider returned invalid audio")
        # MIME alone is not evidence of valid audio. The gateway performs this
        # validation again before storing any artifact or returning it to callers.
        validate_audio_bytes(audio, TTSAudioFormat.MP3)
        return ProviderSynthesisResult(audio_bytes=audio, content_type="audio/mpeg")


def _read_audio(response: httpx.Response, deadline: float) -> bytes:
    status = response.status_code
    # Error bodies are never read: OpenRouter may include input text or provider
    # diagnostics. This gateway never retries or fails over on any TTS error.
    if 300 <= status < 400:
        raise TTSProviderError("provider redirect was refused")
    if status == 401:
        raise TTSAuthenticationError("speech synthesis was not authorized")
    if status == 402:
        raise TTSProviderError("speech synthesis credit limit reached")
    if status == 403:
        raise TTSProviderError("speech synthesis was declined")
    if status == 429:
        raise TTSRateLimitError("speech synthesis is rate limited")
    if status in (408, 504):
        raise TTSTimeoutError("speech synthesis timed out")
    if status != 200:
        raise TTSProviderError("speech synthesis failed")
    if time.monotonic() > deadline:
        raise TTSTimeoutError("speech synthesis timed out")
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise TTSInvalidAudioError("provider used an unexpected content encoding")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type.lower() != "audio/mpeg":
        raise TTSInvalidAudioError("provider did not return the requested audio format")
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise TTSInvalidAudioError("provider length header is malformed") from None
        if length < 0:
            raise TTSInvalidAudioError("provider length header is malformed")
        if length > MAX_TTS_AUDIO_BYTES:
            raise TTSOutputTooLargeError("generated audio exceeds the size limit")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_TTS_AUDIO_BYTES:
            raise TTSOutputTooLargeError("generated audio exceeds the size limit")
        chunks.append(chunk)
        if time.monotonic() > deadline:
            raise TTSTimeoutError("speech synthesis timed out")
    if time.monotonic() > deadline:
        raise TTSTimeoutError("speech synthesis timed out")
    if not total:
        raise TTSInvalidAudioError("provider returned no audio")
    return b"".join(chunks)


__all__ = [
    "DEFAULT_OPENROUTER_TTS_MODEL",
    "DEFAULT_OPENROUTER_TTS_VOICE",
    "OPENROUTER_TTS_PROVIDER_ID",
    "OPENROUTER_TTS_URL",
    "OpenRouterTTSProvider",
    "build_client",
]
