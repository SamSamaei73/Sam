"""Models, validation, audio checks, profiles, credentials, and settings."""

from __future__ import annotations

import hashlib
import pickle
import typing
from typing import Any

import pytest
from pydantic import ValidationError

from sam.core.config import Settings
from sam.tts.audio import validate_audio_bytes
from sam.tts.credentials import (
    FakeTTSCredentialProvider,
    TTSCredential,
    TTSCredentialReference,
    credentials_from_settings,
)
from sam.tts.errors import (
    TTSCredentialError,
    TTSInvalidAudioError,
    TTSOutputTooLargeError,
    TTSProfileNotFoundError,
    TTSSecretDetectedError,
    TTSValidationError,
)
from sam.tts.models import (
    MAX_TTS_AUDIO_BYTES,
    MAX_TTS_TEXT_BYTES,
    MAX_TTS_TEXT_CHARS,
    ProviderSynthesisRequest,
    SynthesisRequest,
    SynthesisResult,
    SynthesizedAudio,
    TrustedVoiceProfile,
    TTSAudioFormat,
    TTSErrorCategory,
    TTSStatus,
)
from sam.tts.profiles import TrustedVoiceProfiles
from sam.tts.validation import validate_text
from tests.tts_support import ALICE, FAKE_KEY, MP3, NOW, SECRET_TEXT, profile

# ============================================================ request shape


class TestSynthesisRequest:
    @pytest.mark.parametrize(
        "field",
        [
            "reference_id",
            "voice_reference",
            "model",
            "endpoint",
            "base_url",
            "api_key",
            "authorization",
            "timeout",
            "timeout_seconds",
            "temperature",
            "top_p",
            "risk",
            "permission",
            "provider",
            "format",
            "references",
            "confirmation_id",
        ],
    )
    def test_provider_and_authorization_fields_are_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            SynthesisRequest.model_validate(
                {
                    "principal": ALICE,
                    "text": "hi",
                    "trusted_voice_profile": "sam_default",
                    field: "anything",
                }
            )

    def test_the_request_fields_are_exactly_the_safe_set(self) -> None:
        assert set(SynthesisRequest.model_fields) == {
            "principal",
            "text",
            "trusted_voice_profile",
            "request_id",
        }

    def test_the_api_key_cannot_enter_a_request_model(self) -> None:
        for model in (SynthesisRequest, ProviderSynthesisRequest, SynthesizedAudio):
            assert not {"api_key", "key", "authorization", "credential", "token"} & set(
                model.model_fields
            )

    @pytest.mark.parametrize(
        "bad", ["", "Has Space", "UPPER", "../x", "a/b", "x" * 65, "1abc", "é"]
    )
    def test_profile_ids_must_be_safe(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            SynthesisRequest(principal=ALICE, text="hi", trusted_voice_profile=bad)

    def test_request_id_defaults_to_unique(self) -> None:
        a = SynthesisRequest(principal=ALICE, text="hi", trusted_voice_profile="p")
        b = SynthesisRequest(principal=ALICE, text="hi", trusted_voice_profile="p")
        assert a.request_id != b.request_id

    def test_text_is_not_in_repr_or_str(self) -> None:
        req = SynthesisRequest(
            principal=ALICE, text="PRIVATESENTENCE", trusted_voice_profile="p"
        )
        assert "PRIVATESENTENCE" not in repr(req) and "PRIVATESENTENCE" not in str(req)

    def test_provider_request_carries_no_credential_or_timeout(self) -> None:
        fields = set(ProviderSynthesisRequest.model_fields)
        assert not {"api_key", "timeout", "timeout_seconds", "credential"} & fields


class TestResultModels:
    def _audio(self, **over: Any) -> SynthesizedAudio:
        fields: dict[str, Any] = dict(
            synthesis_id="s",
            provider_id="p",
            trusted_profile_id="sam_default",
            format=TTSAudioFormat.MP3,
            byte_length=len(MP3),
            sha256=hashlib.sha256(MP3).hexdigest(),
            audio_bytes=MP3,
            created_at=NOW,
        )
        fields.update(over)
        return SynthesizedAudio(**fields)

    def test_raw_audio_is_excluded_from_every_serialization(self) -> None:
        audio = self._audio()
        assert "audio_bytes" not in audio.model_dump()
        assert "audio_bytes" not in audio.model_dump_json()
        assert MP3.hex() not in audio.model_dump_json()
        result = SynthesisResult(
            request_id="r",
            synthesis_id="s",
            principal=ALICE,
            status=TTSStatus.SUCCEEDED,
            audio=audio,
            created_at=NOW,
        )
        assert "audio_bytes" not in result.model_dump_json()
        assert audio.audio_bytes == MP3  # still available to the explicit caller

    def test_request_text_is_excluded_from_serialization(self) -> None:
        req = SynthesisRequest(
            principal=ALICE, text="PRIVATESENTENCE", trusted_voice_profile="p"
        )
        assert "PRIVATESENTENCE" not in req.model_dump_json()
        assert "text" not in req.model_dump()

    def test_audio_bytes_never_appear_in_repr_or_str(self) -> None:
        audio = self._audio(
            audio_bytes=b"ID3\x04" + b"RAWAUDIOMARK" * 20, byte_length=244
        )
        for text in (repr(audio), str(audio)):
            assert "RAWAUDIOMARK" not in text and "\\x" not in text

    @pytest.mark.parametrize(
        "over",
        [
            {"byte_length": 0},
            {"byte_length": MAX_TTS_AUDIO_BYTES + 1},
            {"sha256": "xyz"},
            {"created_at": NOW.replace(tzinfo=None)},
        ],
    )
    def test_audio_model_bounds(self, over: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            self._audio(**over)

    def test_result_shape_and_pinned_flag(self) -> None:
        ok = SynthesisResult(
            request_id="r",
            synthesis_id="s",
            principal=ALICE,
            status=TTSStatus.SUCCEEDED,
            audio=self._audio(),
            created_at=NOW,
        )
        assert ok.grants_authorization is False
        with pytest.raises(ValidationError):
            SynthesisResult(
                request_id="r",
                synthesis_id="s",
                principal=ALICE,
                status=TTSStatus.SUCCEEDED,
                audio=self._audio(),
                created_at=NOW,
                grants_authorization=True,
            )
        with pytest.raises(ValidationError):  # success needs audio
            SynthesisResult(
                request_id="r",
                synthesis_id="s",
                principal=ALICE,
                status=TTSStatus.SUCCEEDED,
                created_at=NOW,
            )
        with pytest.raises(ValidationError):  # failure carries no audio
            SynthesisResult(
                request_id="r",
                synthesis_id="s",
                principal=ALICE,
                status=TTSStatus.FAILED,
                error_category=TTSErrorCategory.PROVIDER_ERROR,
                audio=self._audio(),
                created_at=NOW,
            )
        with pytest.raises(ValidationError):  # failure needs a category
            SynthesisResult(
                request_id="r",
                synthesis_id="s",
                principal=ALICE,
                status=TTSStatus.FAILED,
                created_at=NOW,
            )


# ============================================================ text validation


class TestTextValidation:
    def test_valid_text_is_returned_unchanged(self) -> None:
        text = "  Hello,\nthis\tis é \U0001f600 text.  "
        assert validate_text(text) == text

    @pytest.mark.parametrize("bad", [None, 5, b"bytes", ["a"], object()])
    def test_non_string_rejected(self, bad: Any) -> None:
        with pytest.raises(TTSValidationError):
            validate_text(bad)

    @pytest.mark.parametrize("text", ["", " ", "\n\n", "\t \r\n"])
    def test_empty_or_blank_rejected(self, text: str) -> None:
        with pytest.raises(TTSValidationError):
            validate_text(text)

    @pytest.mark.parametrize(
        "ch", ["\x00", "\x01", "\x07", "\x08", "\x0b", "\x0c", "\x1b", "\x7f"]
    )
    def test_nul_and_control_characters_rejected(self, ch: str) -> None:
        with pytest.raises(TTSValidationError):
            validate_text(f"a{ch}b")

    def test_allowed_whitespace_controls(self) -> None:
        assert validate_text("a\tb\nc\rd") == "a\tb\nc\rd"

    def test_character_limit(self) -> None:
        assert validate_text("a" * MAX_TTS_TEXT_CHARS)
        with pytest.raises(TTSValidationError) as info:
            validate_text("a" * (MAX_TTS_TEXT_CHARS + 1))
        assert info.value.category is TTSErrorCategory.TEXT_TOO_LARGE

    def test_utf8_byte_limit_is_enforced_separately(self) -> None:
        emoji = "\U0001f600"  # 4 bytes
        assert validate_text(emoji * (MAX_TTS_TEXT_BYTES // 4))
        with pytest.raises(TTSValidationError) as info:
            validate_text(emoji * (MAX_TTS_TEXT_BYTES // 4) + "x")
        assert info.value.category is TTSErrorCategory.TEXT_TOO_LARGE

    def test_lone_surrogates_are_not_valid_unicode(self) -> None:
        with pytest.raises(TTSValidationError):
            validate_text("a\ud800b")

    def test_huge_text_is_rejected_before_any_scan(self) -> None:
        import time

        started = time.monotonic()
        with pytest.raises(TTSValidationError):
            validate_text("x" * 50_000_000)
        assert time.monotonic() - started < 1.0

    def test_secret_like_text_is_withheld_and_not_echoed(self) -> None:
        with pytest.raises(TTSSecretDetectedError) as info:
            validate_text(SECRET_TEXT)
        assert "sk-ant" not in str(info.value)
        assert info.value.category is TTSErrorCategory.SECRET_DETECTED

    def test_inline_provider_style_tags_are_just_text(self) -> None:
        for text in (
            "[whisper] hello",
            "(excited) hi",
            "<voice id='x'>hi</voice>",
            "reference_id=abc model=s1",
        ):
            assert validate_text(text) == text  # never interpreted, never stripped


# ============================================================ audio validation


class TestAudioValidation:
    def test_valid_id3_and_frame_sync_audio(self) -> None:
        data, digest = validate_audio_bytes(MP3, TTSAudioFormat.MP3)
        assert data == MP3 and digest == hashlib.sha256(MP3).hexdigest()
        frame = b"\xff\xfb\x90\x00" + b"\x00" * 100
        assert validate_audio_bytes(frame, TTSAudioFormat.MP3)[0] == frame

    def test_digest_is_of_exactly_the_returned_bytes(self) -> None:
        a = validate_audio_bytes(MP3, TTSAudioFormat.MP3)[1]
        b = validate_audio_bytes(MP3 + b"\x00", TTSAudioFormat.MP3)[1]
        assert a != b

    @pytest.mark.parametrize("bad", [None, "text", 5, bytearray(b"ID3xx"), [b"ID3"]])
    def test_non_bytes_rejected(self, bad: Any) -> None:
        with pytest.raises(TTSInvalidAudioError):
            validate_audio_bytes(bad, TTSAudioFormat.MP3)

    def test_empty_rejected(self) -> None:
        with pytest.raises(TTSInvalidAudioError):
            validate_audio_bytes(b"", TTSAudioFormat.MP3)

    def test_oversize_rejected_at_the_boundary(self) -> None:
        with pytest.raises(TTSOutputTooLargeError):
            validate_audio_bytes(
                b"\xff\xfb\x90\x00" + b"\x00" * MAX_TTS_AUDIO_BYTES, TTSAudioFormat.MP3
            )
        ok = b"\xff\xfb\x90\x00" + b"\x00" * (MAX_TTS_AUDIO_BYTES - 4)
        assert (
            len(validate_audio_bytes(ok, TTSAudioFormat.MP3)[0]) == MAX_TTS_AUDIO_BYTES
        )

    @pytest.mark.parametrize(
        "body",
        [
            b"<html><body>error</body></html>",
            b"  \n<!DOCTYPE html>",
            b'{"status":500,"message":"boom"}',
            b'[{"error":"x"}]',
            b"Error: rate limited",
            b'\xef\xbb\xbf{"a":1}',
        ],
    )
    def test_text_bodies_posing_as_audio_rejected(self, body: bytes) -> None:
        with pytest.raises(TTSInvalidAudioError):
            validate_audio_bytes(body, TTSAudioFormat.MP3)

    @pytest.mark.parametrize(
        "body",
        [
            b"RIFF\x00\x00\x00\x00WAVEfmt ",
            b"OggS\x00\x02" + b"\x00" * 30,
            b"\x00" * 64,
            b"\xff\xff\xff\xff" + b"\x00" * 30,  # Layer I, not Layer III
            b"\xff\xe1\x90\x00" + b"\x00" * 30,  # MPEG-2.5 with layer 00
            b"\xff\xf9\x90\x00" + b"\x00" * 30,  # layer 00
            b"ID3",  # truncated header
            b"ID3\xff\x00\x00\x00\x00\x00\x00\x00",  # bad version byte
            b"ID3\x04\x00\x00\x80\x00\x00\x00\x00",  # non-syncsafe size
            b"ID3\x04\x00\x00\x00\x00\x00\x00",  # header only, no payload
        ],
    )
    def test_non_mp3_or_malformed_signatures_rejected(self, body: bytes) -> None:
        with pytest.raises(TTSInvalidAudioError):
            validate_audio_bytes(body, TTSAudioFormat.MP3)


# ================================================================== profiles


class TestProfiles:
    def test_profile_shape(self) -> None:
        p = profile()
        assert p.enabled and p.output_format is TTSAudioFormat.MP3

    def test_profile_has_no_cloning_or_reference_audio_field(self) -> None:
        names = set(TrustedVoiceProfile.model_fields)
        assert names == {
            "profile_id",
            "provider_id",
            "provider_voice_reference",
            "provider_model",
            "output_format",
            "enabled",
        }
        assert (
            not {
                "reference_audio",
                "references",
                "sample",
                "audio",
                "clone",
                "voice_sample",
            }
            & names
        )

    @pytest.mark.parametrize(
        "over",
        [
            {"profile_id": "Bad Id"},
            {"provider_id": "Bad Provider"},
            {"provider_voice_reference": "has space"},
            {"provider_voice_reference": "../x"},
            {"provider_voice_reference": "x" * 65},
            {"provider_model": "bad model"},
            {"provider_model": ""},
        ],
    )
    def test_profile_fields_are_validated(self, over: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            profile(**{**{}, **over}) if False else TrustedVoiceProfile(
                **{
                    "profile_id": "p",
                    "provider_id": "prov",
                    "provider_voice_reference": "abc123",
                    "provider_model": "s1",
                    **over,
                }
            )

    def test_profile_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            TrustedVoiceProfile.model_validate(
                {
                    "profile_id": "p",
                    "provider_id": "prov",
                    "provider_voice_reference": "abc",
                    "provider_model": "s1",
                    "reference_audio": "AAAA",
                }
            )

    def test_profile_is_immutable(self) -> None:
        with pytest.raises(ValidationError):
            profile().enabled = False

    def test_catalog_resolves_enabled_profiles(self) -> None:
        catalog = TrustedVoiceProfiles([profile("a"), profile("b")])
        assert catalog.get("a").profile_id == "a"
        assert [p.profile_id for p in catalog.list_profiles()] == ["a", "b"]

    def test_unknown_and_disabled_profiles_are_rejected(self) -> None:
        catalog = TrustedVoiceProfiles([profile("a"), profile("off", enabled=False)])
        with pytest.raises(TTSProfileNotFoundError):
            catalog.get("zzz")
        with pytest.raises(TTSValidationError) as info:
            catalog.get("off")
        assert info.value.category is TTSErrorCategory.PROFILE_DISABLED

    def test_duplicate_and_excess_profiles_rejected(self) -> None:
        with pytest.raises(ValueError):
            TrustedVoiceProfiles([profile("a"), profile("a")])
        with pytest.raises(ValueError):
            TrustedVoiceProfiles([profile(f"p{i}") for i in range(65)])

    def test_the_catalog_is_read_only_at_runtime(self) -> None:
        import types

        catalog: Any = TrustedVoiceProfiles([profile("a")])
        assert {n for n in dir(catalog) if not n.startswith("_")} == {
            "get",
            "list_profiles",
        }
        for name in ("register", "add", "set", "enable", "disable", "update", "remove"):
            assert not hasattr(catalog, name)
        with pytest.raises(AttributeError):
            catalog._profiles = {}
        with pytest.raises(AttributeError):
            catalog.extra = 1
        held = catalog._profiles
        assert isinstance(held, types.MappingProxyType)
        writable = typing.cast(Any, held)  # the write the type system forbids
        with pytest.raises(TypeError):
            writable["evil"] = profile("evil")

    def test_returned_profiles_cannot_be_edited(self) -> None:
        catalog = TrustedVoiceProfiles([profile("a")])
        with pytest.raises(ValidationError):
            catalog.get("a").provider_voice_reference = "hijacked"
        assert catalog.get("a").provider_voice_reference != "hijacked"


# =============================================================== credentials


class TestCredentials:
    ref = TTSCredentialReference(provider_id="fish-audio", credential_id="fish-main")

    def test_credential_is_redacted_everywhere(self) -> None:
        cred = TTSCredential(FAKE_KEY)
        for text in (
            repr(cred),
            str(cred),
            f"{cred}",
            format(cred),
            f"{cred}",
            repr([cred]),
            repr({"k": cred}),
        ):
            assert FAKE_KEY not in text
        assert cred.reveal() == FAKE_KEY

    def test_credential_cannot_be_pickled_copied_mutated_or_inspected(self) -> None:
        import copy

        cred = TTSCredential(FAKE_KEY)
        with pytest.raises(TypeError):
            pickle.dumps(cred)
        with pytest.raises(TypeError):
            copy.deepcopy(cred)
        with pytest.raises(AttributeError):
            cred.value = "x"
        with pytest.raises(TypeError):
            vars(cred)
        assert TTSCredential(FAKE_KEY) != TTSCredential(FAKE_KEY)

    @pytest.mark.parametrize(
        "bad", ["", " ", None, 5, "x" * 5000, "has space", "a\nb", "a\x00b"]
    )
    def test_invalid_credentials_rejected_without_echo(self, bad: Any) -> None:
        with pytest.raises(TTSCredentialError) as info:
            TTSCredential(bad)
        assert "x" * 40 not in str(info.value)

    def test_fake_provider_resolves_and_hides(self) -> None:
        provider = FakeTTSCredentialProvider()
        provider.add(self.ref, FAKE_KEY)
        assert provider.resolve(self.ref).reveal() == FAKE_KEY
        assert FAKE_KEY not in repr(provider) and FAKE_KEY not in str(provider)

    def test_unknown_reference_fails_generically(self) -> None:
        with pytest.raises(TTSCredentialError) as info:
            FakeTTSCredentialProvider().resolve(self.ref)
        assert "fish-main" not in str(info.value)

    def test_reference_for_another_provider_does_not_resolve(self) -> None:
        provider = FakeTTSCredentialProvider()
        provider.add(self.ref, FAKE_KEY)
        other = TTSCredentialReference(provider_id="other", credential_id="fish-main")
        with pytest.raises(TTSCredentialError):
            provider.resolve(other)

    def test_a_reference_holds_no_secret_and_forbids_extras(self) -> None:
        assert set(TTSCredentialReference.model_fields) == {
            "provider_id",
            "credential_id",
        }
        with pytest.raises(ValidationError):
            TTSCredentialReference.model_validate(
                {"provider_id": "p", "credential_id": "c", "secret": FAKE_KEY}
            )

    def test_the_fake_provider_never_reads_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FISH_AUDIO_API_KEY", "FROM-ENV")
        with pytest.raises(TTSCredentialError):
            FakeTTSCredentialProvider().resolve(self.ref)


class TestSettingsBootstrap:
    ref = TTSCredentialReference(provider_id="fish-audio", credential_id="fish-main")

    def test_key_is_loaded_at_the_settings_boundary_as_a_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FISH_AUDIO_API_KEY", FAKE_KEY)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.fish_audio_api_key is not None
        assert FAKE_KEY not in repr(settings) and FAKE_KEY not in str(settings)
        assert FAKE_KEY not in settings.model_dump_json()

    def test_unset_key_is_none_and_bootstrap_fails_cleanly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FISH_AUDIO_API_KEY", raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.fish_audio_api_key is None
        with pytest.raises(TTSCredentialError):
            credentials_from_settings(settings, self.ref)

    def test_blank_key_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FISH_AUDIO_API_KEY", "   ")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_bootstrap_helper_yields_a_provider_that_hides_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FISH_AUDIO_API_KEY", FAKE_KEY)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        provider = credentials_from_settings(settings, self.ref)
        assert provider.resolve(self.ref).reveal() == FAKE_KEY
        assert FAKE_KEY not in repr(provider)

    def test_there_is_no_fish_base_url_setting(self) -> None:
        names = set(Settings.model_fields)
        assert "fish_audio_api_key" in names
        assert not {
            n
            for n in names
            if "fish" in n and ("url" in n or "endpoint" in n or "host" in n)
        }
