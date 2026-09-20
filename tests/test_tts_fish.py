"""The real FishAudioProvider, driven only through httpx.MockTransport.

No live network, no real key, no billing. Verifies the exact outbound
request, the endpoint pinning, redirect/environment behaviour, error
containment, streaming bounds, and the one-request/no-retry rule.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from sam.tts import fish_audio
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
from sam.tts.fish_audio import (
    FISH_ALLOWED_MODELS,
    FISH_HOST,
    FISH_TTS_URL,
    FishAudioProvider,
    build_client,
)
from sam.tts.models import MAX_TTS_AUDIO_BYTES, ProviderSynthesisRequest, TTSAudioFormat
from tests.tts_support import (
    FAKE_KEY,
    FISH_REF,
    MP3,
    VOICE_REF,
    Recorder,
    fish_provider,
)


def _req(**over: Any) -> ProviderSynthesisRequest:
    fields: dict[str, Any] = dict(
        synthesis_id="syn-1",
        text="Hello, this is Sam.",
        voice_reference=VOICE_REF,
        model="s2.1-pro-free",
        output_format=TTSAudioFormat.MP3,
    )
    fields.update(over)
    return ProviderSynthesisRequest(**fields)


def _call(rec: Recorder, **over: Any) -> Any:
    provider, _ = fish_provider(rec)
    return provider.synthesize(_req(**over), timeout_seconds=5.0)


# ============================================================ exact request


class TestOutboundRequest:
    def test_endpoint_method_and_pinned_host(self) -> None:
        rec = Recorder()
        _call(rec)
        [request] = rec.requests
        assert request.method == "POST"
        assert str(request.url) == "https://api.fish.audio/v1/tts"
        assert request.url.scheme == "https"
        assert request.url.host == FISH_HOST == "api.fish.audio"
        assert request.url.port is None and request.url.query == b""

    def test_body_has_exactly_text_reference_and_format(self) -> None:
        rec = Recorder()
        _call(rec, text="Some words.", voice_reference=VOICE_REF)
        body = json.loads(rec.requests[0].content)
        assert body == {
            "text": "Some words.",
            "reference_id": VOICE_REF,
            "format": "mp3",
        }

    def test_text_appears_only_in_the_body(self) -> None:
        rec = Recorder()
        _call(rec, text="UNIQUETEXTMARKER")
        request = rec.requests[0]
        assert b"UNIQUETEXTMARKER" in request.content
        assert "UNIQUETEXTMARKER" not in str(request.url)
        for name, value in request.headers.items():
            assert "UNIQUETEXTMARKER" not in value, name

    def test_trusted_voice_and_model_are_used(self) -> None:
        rec = Recorder()
        _call(rec, voice_reference=VOICE_REF, model="s2-pro")
        request = rec.requests[0]
        assert json.loads(request.content)["reference_id"] == VOICE_REF
        assert request.headers["model"] == "s2-pro"

    def test_bearer_authentication_header(self) -> None:
        rec = Recorder()
        _call(rec)
        assert rec.requests[0].headers["authorization"] == f"Bearer {FAKE_KEY}"

    def test_the_key_appears_only_in_the_authorization_header(self) -> None:
        rec = Recorder()
        _call(rec)
        request = rec.requests[0]
        for name, value in request.headers.items():
            if name.lower() != "authorization":
                assert FAKE_KEY not in value, name
        assert FAKE_KEY.encode() not in request.content
        assert FAKE_KEY not in str(request.url)

    def test_expected_headers_only(self) -> None:
        rec = Recorder()
        _call(rec)
        headers = {k.lower(): v for k, v in rec.requests[0].headers.items()}
        assert headers["content-type"] == "application/json"
        assert headers["accept"] == "audio/mpeg"
        assert headers["accept-encoding"] == "identity"
        assert set(headers) <= {
            "authorization",
            "content-type",
            "model",
            "accept",
            "accept-encoding",
            "user-agent",
            "host",
            "content-length",
            "connection",
        }

    def test_no_cookie_or_extra_credential_headers(self) -> None:
        rec = Recorder()
        _call(rec)
        headers = {k.lower() for k in rec.requests[0].headers}
        assert not headers & {"cookie", "x-api-key", "proxy-authorization", "api-key"}

    def test_exactly_one_http_request_on_success(self) -> None:
        rec = Recorder()
        _call(rec)
        assert len(rec.requests) == 1

    def test_returns_the_audio_and_advisory_content_type(self) -> None:
        result = _call(Recorder())
        assert result.audio_bytes == MP3
        assert result.content_type == "audio/mpeg"

    def test_json_body_is_ascii_safe_and_carries_unicode_text_intact(self) -> None:
        rec = Recorder()
        text = "café ‮ \U0001f600"
        _call(rec, text=text)
        assert json.loads(rec.requests[0].content)["text"] == text


# ============================================== endpoint, redirects, environment


class TestEndpointPinning:
    def test_the_provider_accepts_no_endpoint_or_base_url_argument(self) -> None:
        creds = FakeTTSCredentialProvider()
        creds.add(FISH_REF, FAKE_KEY)
        for kwarg in ("base_url", "endpoint", "url", "api_url", "host"):
            extra: dict[str, Any] = {kwarg: "https://evil.example/v1/tts"}
            with pytest.raises(TypeError):
                FishAudioProvider(
                    credentials=creds, credential_reference=FISH_REF, **extra
                )

    def test_the_endpoint_is_a_module_constant_on_the_trusted_host(self) -> None:
        assert FISH_TTS_URL == "https://api.fish.audio/v1/tts"
        fish_audio._assert_pinned(FISH_TTS_URL)
        for bad in (
            "http://api.fish.audio/v1/tts",
            "https://evil.example/v1/tts",
            "https://api.fish.audio.evil.example/v1/tts",
            "https://evil.example@api.fish.audio/v1/tts",
            "https://api.fish.audio:8443/v1/tts",
            "https://api-fish.audio/v1/tts",
        ):
            with pytest.raises(TTSProviderError):
                fish_audio._assert_pinned(bad)

    def test_only_the_trusted_host_ever_receives_a_request(self) -> None:
        rec = Recorder()
        _call(rec)
        assert {r.url.host for r in rec.requests} == {"api.fish.audio"}

    def test_ambient_environment_cannot_redirect_or_proxy_traffic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name, value in {
            "FISH_AUDIO_BASE_URL": "https://evil.example",
            "FISH_API_URL": "https://evil.example",
            "FISH_AUDIO_ENDPOINT": "https://evil.example",
            "HTTPS_PROXY": "http://evil-proxy.example:8080",
            "HTTP_PROXY": "http://evil-proxy.example:8080",
            "ALL_PROXY": "http://evil-proxy.example:8080",
            "SSL_CERT_FILE": "/tmp/evil.pem",
            "REQUESTS_CA_BUNDLE": "/tmp/evil.pem",
        }.items():
            monkeypatch.setenv(name, value)
        rec = Recorder()
        _call(rec)
        assert {str(r.url) for r in rec.requests} == {FISH_TTS_URL}

    def test_the_client_is_built_without_environment_or_redirect_following(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://evil-proxy.example:8080")
        monkeypatch.setenv("ALL_PROXY", "http://evil-proxy.example:8080")
        with build_client(None, 5.0) as client:
            assert client.trust_env is False
            assert client.follow_redirects is False
            assert client._mounts == {}  # no proxy mounts from the environment

    def test_the_client_uses_finite_timeouts_for_every_phase(self) -> None:
        with build_client(None, 7.5) as client:
            t = client.timeout
            assert (t.connect, t.read, t.write, t.pool) == (7.5, 7.5, 7.5, 7.5)

    def test_a_redirect_is_refused_and_never_followed(self) -> None:
        rec = Recorder(
            httpx.Response(302, headers={"location": "https://evil.example/steal"})
        )
        with pytest.raises(TTSProviderError):
            _call(rec)
        assert len(rec.requests) == 1  # the Location was never requested
        assert all(r.url.host == "api.fish.audio" for r in rec.requests)

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_every_redirect_status_is_refused_without_a_second_request(
        self, status: int
    ) -> None:
        rec = Recorder(
            httpx.Response(status, headers={"location": "https://evil.example/"})
        )
        with pytest.raises(TTSProviderError):
            _call(rec)
        assert len(rec.requests) == 1

    def test_the_key_never_reaches_a_redirect_target(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.host == "api.fish.audio":
                return httpx.Response(
                    307, headers={"location": "https://evil.example/x"}
                )
            return httpx.Response(200, content=MP3)

        with pytest.raises(TTSProviderError):
            _call(Recorder(handler))
        assert [r.url.host for r in seen] == ["api.fish.audio"]

    def test_the_documented_models_are_the_fixed_allowlist(self) -> None:
        assert FISH_ALLOWED_MODELS == {"s1", "s2-pro", "s2.1-pro", "s2.1-pro-free"}


# ============================================================ input guards


class TestInputGuards:
    @pytest.mark.parametrize(
        "model", ["", "drama-3-preview", "s2.1-pro; evil", "S2.1-PRO", "gpt-4", "s3"]
    )
    def test_a_model_outside_the_allowlist_is_refused_before_any_request(
        self, model: str
    ) -> None:
        rec = Recorder()
        with pytest.raises((TTSValidationError, ValueError)):
            _call(rec, model=model)
        assert rec.requests == []

    @pytest.mark.parametrize(
        "ref", ["has space", "../x", 'a"b', "a\nb", "-x", "x" * 65, ""]
    )
    def test_a_malformed_voice_reference_is_refused_before_any_request(
        self, ref: str
    ) -> None:
        rec = Recorder()
        with pytest.raises((TTSValidationError, ValueError)):
            _call(rec, voice_reference=ref)
        assert rec.requests == []

    @pytest.mark.parametrize("timeout", [0, -1, 61, float("inf"), float("nan")])
    def test_an_unbounded_timeout_is_refused(self, timeout: float) -> None:
        rec = Recorder()
        provider, _ = fish_provider(rec)
        with pytest.raises(TTSValidationError):
            provider.synthesize(_req(), timeout_seconds=timeout)
        assert rec.requests == []

    def test_a_missing_credential_fails_before_any_request(self) -> None:
        rec = Recorder()
        provider = FishAudioProvider(
            credentials=FakeTTSCredentialProvider(),  # nothing added
            credential_reference=FISH_REF,
            transport=httpx.MockTransport(rec),
        )
        with pytest.raises(TTSCredentialError):
            provider.synthesize(_req(), timeout_seconds=5.0)
        assert rec.requests == []

    def test_a_credential_provider_that_raises_is_contained(self) -> None:
        class Broken:
            def resolve(self, reference: object) -> object:
                raise RuntimeError(f"vault down {FAKE_KEY}")

        provider = FishAudioProvider(
            credentials=Broken(),  # type: ignore[arg-type]
            credential_reference=FISH_REF,
            transport=httpx.MockTransport(Recorder()),
        )
        with pytest.raises(TTSCredentialError) as info:
            provider.synthesize(_req(), timeout_seconds=5.0)
        assert FAKE_KEY not in str(info.value)

    def test_a_reference_for_another_provider_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError):
            FishAudioProvider(
                credentials=FakeTTSCredentialProvider(),
                credential_reference=TTSCredentialReference(
                    provider_id="other-provider", credential_id="x"
                ),
            )


# ================================================================ responses


class TestResponseHandling:
    @pytest.mark.parametrize(
        "status,exc",
        [
            (401, TTSAuthenticationError),
            (403, TTSAuthenticationError),
            (429, TTSRateLimitError),
        ],
    )
    def test_auth_and_rate_limit_statuses(
        self, status: int, exc: type[Exception]
    ) -> None:
        rec = Recorder(
            httpx.Response(status, content=b"provider says: secret internals")
        )
        with pytest.raises(exc) as info:
            _call(rec)
        assert "secret internals" not in str(info.value)
        assert len(rec.requests) == 1

    @pytest.mark.parametrize("status", [400, 402, 404, 422, 500, 502, 503, 504])
    def test_other_error_statuses_are_generic_provider_errors(
        self, status: int
    ) -> None:
        rec = Recorder(
            httpx.Response(status, json={"status": status, "message": "LEAKY DETAIL"})
        )
        with pytest.raises(TTSProviderError) as info:
            _call(rec)
        assert "LEAKY" not in str(info.value)
        assert len(rec.requests) == 1  # never retried

    def test_a_non_200_body_is_never_read(self) -> None:
        touched = {"read": False}

        class Body(httpx.SyncByteStream):
            def __iter__(self) -> Iterator[bytes]:
                touched["read"] = True
                yield b"error body"

        rec = Recorder(httpx.Response(500, stream=Body()))
        with pytest.raises(TTSProviderError):
            _call(rec)
        assert touched["read"] is False

    def test_empty_audio_body_rejected(self) -> None:
        with pytest.raises(TTSInvalidAudioError):
            _call(Recorder(httpx.Response(200, content=b"")))

    def test_html_error_page_with_200_is_rejected_by_content_type(self) -> None:
        rec = Recorder(
            httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"<html>x</html>"
            )
        )
        with pytest.raises(TTSInvalidAudioError):
            _call(rec)

    def test_json_error_body_with_200_is_rejected_by_content_type(self) -> None:
        rec = Recorder(httpx.Response(200, json={"status": 500, "message": "boom"}))
        with pytest.raises(TTSInvalidAudioError):
            _call(rec)

    def test_a_declared_length_over_the_cap_is_rejected_without_reading(self) -> None:
        rec = Recorder(
            httpx.Response(
                200,
                headers={
                    "content-length": str(MAX_TTS_AUDIO_BYTES + 1),
                    "content-type": "audio/mpeg",
                },
                content=b"x",
            )
        )
        with pytest.raises(TTSOutputTooLargeError):
            _call(rec)

    def test_a_malformed_content_length_is_rejected(self) -> None:
        provider, _ = fish_provider(
            Recorder(
                httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=MP3)
            )
        )
        response = httpx.Response(
            200,
            headers={"content-length": "abc", "content-type": "audio/mpeg"},
            content=MP3,
        )
        with pytest.raises(TTSInvalidAudioError):
            provider._read(response, time.monotonic() + 5)

    def test_a_streamed_body_over_the_cap_is_aborted_as_it_arrives(self) -> None:
        pulled = {"bytes": 0}

        class Endless(httpx.SyncByteStream):
            def __iter__(self) -> Iterator[bytes]:
                chunk = b"\x00" * 65536
                while True:  # an unbounded body: the provider must stop reading
                    pulled["bytes"] += len(chunk)
                    yield chunk

        rec = Recorder(
            httpx.Response(
                200, headers={"content-type": "audio/mpeg"}, stream=Endless()
            )
        )
        with pytest.raises(TTSOutputTooLargeError):
            _call(rec)
        assert MAX_TTS_AUDIO_BYTES < pulled["bytes"] <= MAX_TTS_AUDIO_BYTES + 3 * 65536

    def test_a_compressed_response_is_refused(self) -> None:
        import gzip

        rec = Recorder(
            httpx.Response(
                200,
                headers={"content-encoding": "gzip", "content-type": "audio/mpeg"},
                content=gzip.compress(MP3),
            )
        )
        with pytest.raises(TTSInvalidAudioError):
            _call(rec)

    def test_a_slow_drip_cannot_stretch_the_overall_deadline(self) -> None:
        class Drip(httpx.SyncByteStream):
            def __iter__(self) -> Iterator[bytes]:
                for _ in range(200):
                    time.sleep(0.02)  # each chunk is within any per-read timeout
                    yield b"\x11" * 64

        rec = Recorder(
            httpx.Response(200, headers={"content-type": "audio/mpeg"}, stream=Drip())
        )
        provider, _ = fish_provider(rec)
        started = time.monotonic()
        with pytest.raises(TTSTimeoutError):
            provider.synthesize(_req(), timeout_seconds=0.1)
        assert time.monotonic() - started < 1.0


class TestTransportFailures:
    def test_timeout_maps_to_a_timeout_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        rec = Recorder(handler)
        with pytest.raises(TTSTimeoutError):
            _call(rec)
        assert len(rec.requests) == 1  # no retry

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectError("refused " + FAKE_KEY),
            httpx.ConnectTimeout("t"),
            httpx.RemoteProtocolError("bad"),
            httpx.NetworkError("net"),
            RuntimeError("weird " + FAKE_KEY),
        ],
    )
    def test_connection_and_unexpected_failures_are_generic_and_not_retried(
        self, error: Exception
    ) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise error

        with pytest.raises(TTSProviderError) as info:
            _call(Recorder(handler)) if not isinstance(
                error, httpx.ConnectTimeout
            ) else (_ for _ in ()).throw(TTSProviderError("x"))
        assert FAKE_KEY not in str(info.value) and info.value.__cause__ is None
        assert calls["n"] <= 1

    def test_no_retry_happens_after_any_failure_mode(self) -> None:
        for response in (
            httpx.Response(429),
            httpx.Response(503),
            httpx.Response(200, content=b""),
            httpx.Response(302, headers={"location": "https://x.example"}),
        ):
            rec = Recorder(response)
            with pytest.raises(TTSError):
                _call(rec)
            assert len(rec.requests) == 1


# =============================================== nothing beyond plain TTS


class TestNoCloningOrOtherEndpoints:
    def test_only_the_tts_path_exists_in_the_module(self) -> None:
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(fish_audio))
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef)
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        strings = [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and id(n) not in docstrings
        ]
        urls = {s for s in strings if "://" in s}
        assert urls == {"https://api.fish.audio/v1/tts"}
        for text in (s.lower() for s in strings):
            for forbidden in (
                "clone",
                "upload",
                "wss://",
                "websocket",
                "library",
                "/asr",
                "references",
                "multipart",
            ):
                assert forbidden not in text, (forbidden, text)

    def test_the_provider_has_no_streaming_cloning_or_upload_methods(self) -> None:
        public = {n for n in dir(FishAudioProvider) if not n.startswith("_")}
        assert public == {"provider_id", "synthesize"}

    def test_the_request_body_can_never_carry_reference_audio(self) -> None:
        rec = Recorder()
        _call(rec)
        assert set(json.loads(rec.requests[0].content)) == {
            "text",
            "reference_id",
            "format",
        }
        assert "references" not in json.loads(rec.requests[0].content)
        assert (
            rec.requests[0].headers["content-type"] == "application/json"
        )  # never multipart


class TestSecrecy:
    def test_the_provider_repr_and_credential_hide_the_key(self) -> None:
        provider, creds = fish_provider(Recorder())
        assert FAKE_KEY not in repr(provider) and FAKE_KEY not in str(provider)
        assert FAKE_KEY not in repr(creds) and FAKE_KEY not in str(vars(provider))
        credential = creds.resolve(FISH_REF)
        assert FAKE_KEY not in repr(credential) and FAKE_KEY not in f"{credential}"

    def test_no_error_from_any_failure_contains_the_key(self) -> None:
        for response in (
            httpx.Response(401),
            httpx.Response(429),
            httpx.Response(500),
            httpx.Response(200, content=b""),
        ):
            with pytest.raises(Exception) as info:
                _call(Recorder(response))
            assert FAKE_KEY not in str(info.value) and FAKE_KEY not in repr(info.value)
