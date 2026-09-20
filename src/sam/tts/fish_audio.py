"""Fish Audio text-to-speech provider (non-streaming REST only).

Official API (verified against docs.fish.audio on 2026-09-20; see
docs/tts.md): ``POST https://api.fish.audio/v1/tts`` with
``Authorization: Bearer <key>``, a ``model`` request *header*, and a JSON body
of ``{"text", "reference_id", "format"}``.

Security properties, all enforced here:

* **Pinned endpoint.** ``FISH_TTS_URL`` is a module constant. The provider has
  no ``base_url``/``endpoint`` parameter, reads no environment variable, and a
  request/LLM/provider response/document/MCP result cannot influence it. The
  transport is refused unless the request host is exactly ``api.fish.audio``
  over HTTPS.
* **No redirects, no ambient config.** The client is built with
  ``follow_redirects=False`` and ``trust_env=False`` (no proxy variables, no
  ``SSL_CERT_*``, no ``.netrc``), so nothing in the process environment can
  redirect traffic that carries the key, and a 3xx is refused, never followed.
* **The key exists only here.** It is resolved from a trusted reference inside
  ``synthesize`` and appears only in the ``Authorization`` header.
* **Finite timeouts.** Sam's ``timeout_seconds`` sets the connect/read/write/
  pool timeouts *and* an overall deadline checked while streaming (a read
  timeout alone is per-chunk and can be stretched by a slow drip).
* **Bounded body.** The response is streamed and aborted the moment it exceeds
  ``MAX_TTS_AUDIO_BYTES``; ``Accept-Encoding: identity`` is sent and any
  content-encoding is refused (no decompression amplification).
* **One request, no retry.** At most one HTTP call per ``synthesize``. There
  is no retry loop, no streaming/WebSocket, and no cloning, upload,
  voice-library, or ASR endpoint anywhere in this module.
* **No error-body leakage.** A non-200 response body is never read into Sam;
  errors map to generic typed errors.

Fish output is untrusted; the gateway validates it independently.
"""

from __future__ import annotations

import json
import re
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

FISH_PROVIDER_ID = "fish-audio"
FISH_HOST = "api.fish.audio"
FISH_TTS_URL = "https://api.fish.audio/v1/tts"

# Models documented on 2026-09-20. Fish silently falls back to its default for
# an unrecognized value, so Sam refuses anything outside this fixed list rather
# than let a typo change which model bills the call. ("drama-3-preview" is a
# preview model and deliberately excluded.)
FISH_ALLOWED_MODELS: frozenset[str] = frozenset(
    {"s1", "s2-pro", "s2.1-pro", "s2.1-pro-free"}
)
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def build_client(
    transport: httpx.BaseTransport | None, timeout_seconds: float
) -> httpx.Client:
    """The one place an HTTP client is configured. Exposed for tests."""

    return httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(timeout_seconds),  # connect/read/write/pool
        follow_redirects=False,
        trust_env=False,
        http2=False,
    )


def _assert_pinned(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != FISH_HOST
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
    ):
        raise TTSProviderError("endpoint is not the trusted Fish Audio endpoint")


class FishAudioProvider:
    """``SpeechSynthesisProvider`` backed by Fish Audio's REST TTS API."""

    provider_id = FISH_PROVIDER_ID

    def __init__(
        self,
        *,
        credentials: TTSCredentialProvider,
        credential_reference: TTSCredentialReference,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if credential_reference.provider_id != FISH_PROVIDER_ID:
            raise ValueError("credential reference is not for this provider")
        self._credentials = credentials
        self._reference = credential_reference
        self._transport = transport  # tests inject httpx.MockTransport

    def synthesize(
        self, request: ProviderSynthesisRequest, *, timeout_seconds: float
    ) -> ProviderSynthesisResult:
        if not 0 < timeout_seconds <= MAX_TTS_TIMEOUT_SECONDS:
            raise TTSValidationError("timeout is out of bounds")
        # Trusted-profile values are still checked: a misconfigured profile must
        # fail before the key is resolved or any request is made.
        if request.model not in FISH_ALLOWED_MODELS:
            raise TTSValidationError("voice model is not allowed")
        if not _REFERENCE_RE.match(request.voice_reference):
            raise TTSValidationError("voice reference is malformed")
        if request.output_format is not TTSAudioFormat.MP3:
            raise TTSValidationError("output format is not supported")
        _assert_pinned(FISH_TTS_URL)

        try:
            credential = self._credentials.resolve(self._reference)
        except TTSError:
            raise
        except Exception:
            raise TTSCredentialError("credential is not available") from None

        body = json.dumps(
            {
                "text": request.text,
                "reference_id": request.voice_reference,
                "format": request.output_format.value,
            }
        )
        headers = {
            "Authorization": f"Bearer {credential.reveal()}",
            "Content-Type": "application/json",
            "model": request.model,
            "Accept": "audio/mpeg",
            "Accept-Encoding": "identity",
            "User-Agent": "sam-tts/0.1",
        }
        deadline = time.monotonic() + timeout_seconds
        try:
            with build_client(self._transport, timeout_seconds) as client:
                with client.stream(
                    "POST", FISH_TTS_URL, headers=headers, content=body
                ) as response:
                    audio = self._read(response, deadline)
        except TTSError:
            raise
        except httpx.TimeoutException:
            raise TTSTimeoutError("speech synthesis timed out") from None
        except Exception:
            # Connection errors, protocol errors, and anything unexpected:
            # generic, no text, no cause, and the key is never in the message.
            raise TTSProviderError("speech synthesis request failed") from None
        return ProviderSynthesisResult(
            audio_bytes=audio, content_type=self._content_type(response)
        )

    # ------------------------------------------------------------------ #

    @staticmethod
    def _content_type(response: httpx.Response) -> str | None:
        value = response.headers.get("content-type")
        return value[:100] if value else None

    @staticmethod
    def _read(response: httpx.Response, deadline: float) -> bytes:
        status = response.status_code
        if 300 <= status < 400:
            raise TTSProviderError("provider redirect was refused")
        if status in (401, 403):
            raise TTSAuthenticationError("speech synthesis was not authorized")
        if status == 429:
            raise TTSRateLimitError("speech synthesis is rate limited")
        if status != 200:
            # Body deliberately not read: it may carry provider text.
            raise TTSProviderError("speech synthesis failed")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise TTSInvalidAudioError("provider used an unexpected content encoding")
        content_type = (response.headers.get("content-type") or "").lower()
        if content_type.startswith(("application/json", "text/")):
            raise TTSInvalidAudioError("provider returned a text body, not audio")
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_TTS_AUDIO_BYTES:
                    raise TTSOutputTooLargeError(
                        "generated audio exceeds the size limit"
                    )
            except ValueError:
                raise TTSInvalidAudioError(
                    "provider length header is malformed"
                ) from None
        chunks: list[bytes] = []
        total = 0
        # Content-Encoding was already required to be identity, so nothing is
        # ever decompressed here. No ``chunk_size``: re-chunking would buffer a
        # slow drip and defeat the per-chunk deadline check.
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_TTS_AUDIO_BYTES:
                raise TTSOutputTooLargeError("generated audio exceeds the size limit")
            chunks.append(chunk)
            if time.monotonic() > deadline:
                raise TTSTimeoutError("speech synthesis timed out")
        if time.monotonic() > deadline:
            raise TTSTimeoutError("speech synthesis timed out")
        if total == 0:
            raise TTSInvalidAudioError("provider returned no audio")
        return b"".join(chunks)

    def __repr__(self) -> str:
        return f"FishAudioProvider(provider_id={self.provider_id!r})"


__all__ = [
    "FISH_ALLOWED_MODELS",
    "FISH_HOST",
    "FISH_PROVIDER_ID",
    "FISH_TTS_URL",
    "FishAudioProvider",
    "build_client",
]
