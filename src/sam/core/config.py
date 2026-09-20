"""Application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import (
    AnyHttpUrl,
    Field,
    FiniteFloat,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed runtime settings for the Sam service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = Field(default="Sam", min_length=1)
    app_env: str = Field(default="development", min_length=1)
    log_level: str = Field(default="INFO", min_length=1)
    api_host: str = Field(default="127.0.0.1", min_length=1)
    api_port: int = Field(default=8000, ge=1, le=65535)
    anthropic_api_key: SecretStr | None = None
    # Loaded here, at the trusted bootstrap boundary, so no provider ever
    # reads the ambient environment itself. There is deliberately NO Fish
    # base-URL setting: the Fish endpoint is pinned in sam.tts.fish_audio.
    fish_audio_api_key: SecretStr | None = None
    # Trusted voice configuration for the desktop bridge. A profile is only
    # built when BOTH the key and a voice reference are configured; neither
    # can ever be supplied by the UI, the LLM, or a request.
    fish_audio_voice_reference: str | None = None
    fish_audio_model: str = Field(default="s2.1-pro-free", min_length=1)
    # OPTIONAL Persian speech (Gemini TTS). Off unless GEMINI_API_KEY is set
    # locally. There is deliberately NO endpoint or model setting: both are
    # pinned in sam.tts.gemini_tts. Free-tier only; Sam never enables billing
    # and never falls back to another (paid) provider.
    gemini_api_key: SecretStr | None = None
    gemini_tts_voice: str = Field(default="Kore", min_length=1, max_length=24)
    # Shared secret between the local backend and the desktop shell. When
    # unset the /desktop/v1 routes refuse every request (fail closed).
    desktop_bridge_token: SecretStr | None = None
    # Step-up secret required to approve a CRITICAL confirmation from the
    # desktop. Unset means CRITICAL actions cannot be approved from the UI at
    # all (fail closed: approve out-of-band or not at all).
    desktop_step_up_secret: SecretStr | None = None
    # Owner voice identity (Phase 12). Off unless explicitly enabled; the local
    # models are set up explicitly and never downloaded during a request.
    voice_identity_enabled: bool = False
    speaker_verification_threshold: float = Field(default=0.5, ge=0.3, le=0.9)
    local_stt_model: Literal["small", "medium", "large-v3"] = "small"
    claude_model: str = Field(default="claude-sonnet-4-5", min_length=1)
    claude_base_url: AnyHttpUrl = Field(
        default=AnyHttpUrl("https://api.anthropic.com")
    )
    claude_timeout: FiniteFloat = Field(default=30.0, gt=0, le=300)
    claude_max_retries: int = Field(default=2, ge=0, le=3)

    @field_validator("anthropic_api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        """Reject configured keys that contain no usable credential."""

        if value is not None and not value.get_secret_value().strip():
            raise ValueError("ANTHROPIC_API_KEY must not be blank")
        return value

    @field_validator("fish_audio_api_key")
    @classmethod
    def validate_fish_audio_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        """Reject configured keys that contain no usable credential."""

        if value is not None and not value.get_secret_value().strip():
            raise ValueError("FISH_AUDIO_API_KEY must not be blank")
        return value

    @field_validator("gemini_api_key")
    @classmethod
    def validate_gemini_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        """Reject configured keys that contain no usable credential."""

        if value is not None and not value.get_secret_value().strip():
            raise ValueError("GEMINI_API_KEY must not be blank")
        return value

    @field_validator("desktop_bridge_token")
    @classmethod
    def validate_desktop_bridge_token(cls, value: SecretStr | None) -> SecretStr | None:
        """Require a token long enough to resist guessing by a local process."""

        if value is not None and len(value.get_secret_value().strip()) < 32:
            raise ValueError("DESKTOP_BRIDGE_TOKEN must be at least 32 characters")
        return value

    @field_validator("desktop_step_up_secret")
    @classmethod
    def validate_desktop_step_up_secret(
        cls, value: SecretStr | None
    ) -> SecretStr | None:
        """A short step-up secret is no stronger than a click."""

        if value is not None and len(value.get_secret_value()) < 16:
            raise ValueError("DESKTOP_STEP_UP_SECRET must be at least 16 characters")
        return value

    @model_validator(mode="after")
    def validate_step_up_is_distinct_from_bridge_token(self) -> "Settings":
        """The step-up secret proves a human is present; the bridge token only
        proves the caller is the local shell. They must never be the same."""

        token, step_up = self.desktop_bridge_token, self.desktop_step_up_secret
        if (
            token is not None
            and step_up is not None
            and token.get_secret_value() == step_up.get_secret_value()
        ):
            raise ValueError(
                "DESKTOP_STEP_UP_SECRET must differ from DESKTOP_BRIDGE_TOKEN"
            )
        return self

    @field_validator("claude_model")
    @classmethod
    def validate_claude_model(cls, value: str) -> str:
        """Reject blank or whitespace-only model identifiers."""

        if not value.strip():
            raise ValueError("CLAUDE_MODEL must not be blank")
        return value.strip()

    @field_validator("claude_base_url")
    @classmethod
    def validate_claude_base_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """Require an explicit HTTPS provider endpoint."""

        if value.scheme != "https":
            raise ValueError("CLAUDE_BASE_URL must use HTTPS")
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""

    return Settings()
