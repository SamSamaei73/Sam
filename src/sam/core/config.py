"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import AnyHttpUrl, Field, FiniteFloat, SecretStr, field_validator
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
