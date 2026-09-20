"""Optional Gemini text-to-speech provider (Persian), non-streaming REST only.

Official API (Gemini Interactions API, per ai.google.dev speech-generation):
``POST https://generativelanguage.googleapis.com/v1beta/interactions`` with an
``x-goog-api-key`` header and a JSON body of ``{"model", "input",
"response_format": {"type": "audio"}, "generation_config": {"speech_config":
[{"voice": ...}]}}``. The audio is returned base64-encoded in
``output_audio.data`` as raw PCM: 24 kHz, mono, 16-bit. Sam wraps it into a
canonical WAV before it goes anywhere.

Security and cost properties, all enforced here:

* **Pinned endpoint.** ``GEMINI_INTERACTIONS_URL`` is a module constant; there
  is no endpoint/base-URL parameter and no environment variable is read.
* **Pinned model.** Only ``gemini-3.1-flash-tts-preview`` (free-tier flash
  preview) is allowed; the model and voice come from a trusted profile, never
  from a request, the frontend, an LLM, a document or a provider response.
* **Key only in a header.** Never in the URL or query string, never in a
  request model, never in an error message.
* **No redirects, no ambient config.** ``follow_redirects=False`` and
  ``trust_env=False``: no proxy variable or ``.netrc`` can redirect the key.
* **One request, NO retry.** Google's docs mention retrying rare 500s. Sam does
  not: a 5xx becomes a generic provider error. There is no other provider to
  fall back to, no billing/tier control, and no paid path of any kind.
* **Finite timeout, bounded body.** Overall deadline checked while streaming;
  the response is aborted past a byte cap and the decoded PCM is capped too.
* **No error-body leakage.** A non-200 body is never read into Sam.

The provider's output is untrusted; the gateway validates the WAV again.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import struct
import time
from urllib.parse import urlsplit

import httpx

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
)

GEMINI_PROVIDER_ID = "gemini-tts"
GEMINI_HOST = "generativelanguage.googleapis.com"
GEMINI_INTERACTIONS_URL = (
    "https://generativelanguage.googleapis.com/v1beta/interactions"
)
GEMINI_TTS_MODEL = "gemini-3.1-flash-tts-preview"
GEMINI_ALLOWED_MODELS: frozenset[str] = frozenset({GEMINI_TTS_MODEL})

PCM_SAMPLE_RATE = 24_000
_WAV_HEADER_BYTES = 44
MAX_PCM_BYTES = MAX_TTS_AUDIO_BYTES - _WAV_HEADER_BYTES
# base64 inflates by 4/3; leave room for the small JSON envelope.
MAX_RESPONSE_BYTES = (MAX_PCM_BYTES * 4) // 3 + 64 * 1024
_VOICE_RE = re.compile(r"^[A-Z][a-z]{2,23}$")


def build_client(
    transport: httpx.BaseTransport | None, timeout_seconds: float
) -> httpx.Client:
    """The one place an HTTP client is configured. Exposed for tests."""

    return httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        trust_env=False,
        http2=False,
    )


def _assert_pinned(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != GEMINI_HOST
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.path != "/v1beta/interactions"
        or parts.query
    ):
        raise TTSProviderError("endpoint is not the trusted Gemini endpoint")


def pcm16_to_wav(pcm: bytes, sample_rate: int = PCM_SAMPLE_RATE) -> bytes:
    """Wrap raw 16-bit mono little-endian PCM in a canonical WAV container."""

    byte_rate = sample_rate * 2
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
    )
    return header + pcm


class GeminiTTSProvider:
    """``SpeechSynthesisProvider`` backed by Gemini's Interactions TTS API."""

    provider_id = GEMINI_PROVIDER_ID

    def __init__(
        self,
        *,
        credentials: TTSCredentialProvider,
        credential_reference: TTSCredentialReference,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if credential_reference.provider_id != GEMINI_PROVIDER_ID:
            raise ValueError("credential reference is not for this provider")
        self._credentials = credentials
        self._reference = credential_reference
        self._transport = transport  # tests inject httpx.MockTransport

    def synthesize(
        self, request: ProviderSynthesisRequest, *, timeout_seconds: float
    ) -> ProviderSynthesisResult:
        if not 0 < timeout_seconds <= MAX_TTS_TIMEOUT_SECONDS:
            raise TTSValidationError("timeout is out of bounds")
        # A misconfigured trusted profile fails before the key is resolved.
        if request.model not in GEMINI_ALLOWED_MODELS:
            raise TTSValidationError("voice model is not allowed")
        if not _VOICE_RE.match(request.voice_reference):
            raise TTSValidationError("voice is malformed")
        if request.output_format is not TTSAudioFormat.WAV:
            raise TTSValidationError("output format is not supported")
        _assert_pinned(GEMINI_INTERACTIONS_URL)

        try:
            credential = self._credentials.resolve(self._reference)
        except TTSError:
            raise
        except Exception:
            raise TTSCredentialError("credential is not available") from None

        body = json.dumps(
            {
                "model": request.model,
                "input": request.text,
                "response_format": {"type": "audio"},
                "generation_config": {
                    "speech_config": [{"voice": request.voice_reference}]
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "x-goog-api-key": credential.reveal(),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "sam-tts/0.1",
        }
        deadline = time.monotonic() + timeout_seconds
        try:
            with build_client(self._transport, timeout_seconds) as client:
                with client.stream(
                    "POST", GEMINI_INTERACTIONS_URL, headers=headers, content=body
                ) as response:
                    payload = self._read(response, deadline)
        except TTSError:
            raise
        except httpx.TimeoutException:
            raise TTSTimeoutError("speech synthesis timed out") from None
        except Exception:
            raise TTSProviderError("speech synthesis request failed") from None
        pcm = self._extract_pcm(payload)
        return ProviderSynthesisResult(
            audio_bytes=pcm16_to_wav(pcm), content_type="audio/wav"
        )

    # ------------------------------------------------------------------ #

    @staticmethod
    def _read(response: httpx.Response, deadline: float) -> bytes:
        status = response.status_code
        if 300 <= status < 400:
            raise TTSProviderError("provider redirect was refused")
        if status in (401, 403):
            raise TTSAuthenticationError("speech synthesis was not authorized")
        if status == 429:
            # Free quota exhausted: a safe, typed result. Never retried, and
            # never routed to another provider.
            raise TTSRateLimitError("speech synthesis is rate limited")
        if status != 200:
            # Includes 5xx: NOT retried. Body deliberately not read.
            raise TTSProviderError("speech synthesis failed")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise TTSInvalidAudioError("provider used an unexpected content encoding")
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_RESPONSE_BYTES:
                    raise TTSOutputTooLargeError("generated audio exceeds the limit")
            except ValueError:
                raise TTSInvalidAudioError(
                    "provider length header is malformed"
                ) from None
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise TTSOutputTooLargeError("generated audio exceeds the limit")
            chunks.append(chunk)
            if time.monotonic() > deadline:
                raise TTSTimeoutError("speech synthesis timed out")
        if time.monotonic() > deadline:
            raise TTSTimeoutError("speech synthesis timed out")
        return b"".join(chunks)

    @staticmethod
    def _extract_pcm(payload: bytes) -> bytes:
        """``output_audio.data`` (base64 raw PCM16 mono 24 kHz) -> bytes."""

        try:
            document = json.loads(payload)
            data = document["output_audio"]["data"]
        except (ValueError, KeyError, TypeError):
            raise TTSInvalidAudioError("provider returned no audio") from None
        if not isinstance(data, str) or not data:
            raise TTSInvalidAudioError("provider returned no audio")
        if len(data) > (MAX_PCM_BYTES * 4) // 3 + 8:
            raise TTSOutputTooLargeError("generated audio exceeds the size limit")
        try:
            pcm = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise TTSInvalidAudioError("provider audio is not valid base64") from None
        if not pcm or len(pcm) % 2 != 0:
            raise TTSInvalidAudioError("provider audio is not 16-bit PCM")
        if len(pcm) > MAX_PCM_BYTES:
            raise TTSOutputTooLargeError("generated audio exceeds the size limit")
        return pcm

    def __repr__(self) -> str:
        return f"GeminiTTSProvider(provider_id={self.provider_id!r})"


__all__ = [
    "GEMINI_ALLOWED_MODELS",
    "GEMINI_HOST",
    "GEMINI_INTERACTIONS_URL",
    "GEMINI_PROVIDER_ID",
    "GEMINI_TTS_MODEL",
    "GeminiTTSProvider",
    "build_client",
    "pcm16_to_wav",
]
