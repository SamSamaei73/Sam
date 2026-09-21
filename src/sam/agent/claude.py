"""Anthropic-backed implementation of the language model provider contract.

LEGACY (Phase 2) and NOT WIRED into production: this provider calls the paid
Anthropic Messages API with an API key. Since Phase 13 every model call goes
through the ``sam.models`` router, which has no Anthropic-API provider
(``PAID_FALLBACK = OFF``); Claude is reached only through the owner's own
subscription login (``claude_subscription``). A test asserts nothing in
production wiring imports this module.
"""

import logging
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from threading import RLock
from typing import cast

import httpx
from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    OverloadedError,
    RateLimitError,
)
from anthropic import AuthenticationError as AnthropicAuthenticationError
from anthropic.types import MessageParam

from sam.agent.errors import (
    AgentError,
    AuthenticationError,
    MalformedProviderResponseError,
    ProviderOverloadedError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from sam.agent.models import Message, MessageRole, ProviderResponse
from sam.core.config import Settings

logger = logging.getLogger(__name__)
_SDK_ENV_LOCK = RLock()
_SDK_ENV_OVERRIDES = ("ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS")


@contextmanager
def _isolated_sdk_environment() -> Iterator[None]:
    """Prevent undocumented Anthropic routing/header environment overrides."""

    with _SDK_ENV_LOCK:
        removed = {
            name: os.environ.pop(name)
            for name in _SDK_ENV_OVERRIDES
            if name in os.environ
        }
        try:
            yield
        finally:
            os.environ.update(removed)


class ClaudeProvider:
    """Translate the provider-neutral contract to Anthropic's Messages API."""

    def __init__(
        self,
        settings: Settings,
        client: Anthropic | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._http_client = http_client

    @property
    def client(self) -> Anthropic | None:
        """Expose the owned client for lifecycle and transport tests."""

        return self._client

    def open(self) -> None:
        """Construct the SDK client once for the application lifetime."""

        if self._client is not None or self._settings.anthropic_api_key is None:
            return

        try:
            with _isolated_sdk_environment():
                self._client = Anthropic(
                    api_key=self._settings.anthropic_api_key.get_secret_value(),
                    base_url=str(self._settings.claude_base_url),
                    timeout=self._settings.claude_timeout,
                    max_retries=self._settings.claude_max_retries,
                    http_client=self._http_client,
                )
        except Exception as error:
            raise ProviderRequestError(
                "Anthropic client initialization failed"
            ) from error

    def close(self) -> None:
        """Close the owned SDK client without leaking cleanup details."""

        if self._client is None:
            return
        try:
            self._client.close()
        except Exception:
            logger.info("Provider client cleanup failed")
        finally:
            self._client = None

    def complete(self, messages: Sequence[Message]) -> ProviderResponse:
        """Generate one response and translate every provider boundary failure."""

        try:
            self.open()
            if self._client is None:
                raise AuthenticationError("Anthropic API key is not configured")

            system_messages = [
                message.content
                for message in messages
                if message.role is MessageRole.SYSTEM
            ]
            provider_messages: list[MessageParam] = [
                cast(
                    MessageParam,
                    {"role": message.role.value, "content": message.content},
                )
                for message in messages
                if message.role in (MessageRole.USER, MessageRole.ASSISTANT)
            ]
            if system_messages:
                response = self._client.messages.create(
                    model=self._settings.claude_model,
                    max_tokens=1024,
                    messages=provider_messages,
                    system="\n\n".join(system_messages),
                )
            else:
                response = self._client.messages.create(
                    model=self._settings.claude_model,
                    max_tokens=1024,
                    messages=provider_messages,
                )
            return self._parse_response(response)
        except AgentError:
            raise
        except AnthropicAuthenticationError as error:
            logger.info("Provider failure: authentication")
            raise AuthenticationError("Anthropic authentication failed") from error
        except APITimeoutError as error:
            logger.info("Provider failure: timeout")
            raise ProviderTimeoutError("Anthropic request timed out") from error
        except APIConnectionError as error:
            logger.info("Provider failure: unavailable")
            raise ProviderUnavailableError("Anthropic is unavailable") from error
        except RateLimitError as error:
            logger.info("Provider failure: rate_limit")
            raise ProviderRateLimitError("Anthropic rate limit reached") from error
        except OverloadedError as error:
            logger.info("Provider failure: overloaded")
            raise ProviderOverloadedError("Anthropic is overloaded") from error
        except InternalServerError as error:
            logger.info("Provider failure: server")
            raise ProviderServerError("Anthropic server error") from error
        except APIStatusError as error:
            logger.info("Provider failure: request")
            raise ProviderRequestError("Anthropic request failed") from error
        except Exception as error:
            logger.info("Provider failure: unexpected")
            raise ProviderRequestError("Anthropic request failed") from error

    def _parse_response(self, response: object) -> ProviderResponse:
        """Validate the untrusted SDK response inside the provider boundary."""

        try:
            content_name = "content"
            model_name = "model"
            content = getattr(response, content_name)
            model = getattr(response, model_name)
            stop_reason = getattr(response, "stop_reason", None)
            if not isinstance(content, (list, tuple)):
                raise ValueError("content must be a sequence")
            if not isinstance(model, str) or not model.strip():
                raise ValueError("model must be a non-empty string")
            if stop_reason is not None and not isinstance(stop_reason, str):
                raise ValueError("stop_reason must be a string or null")

            text_parts: list[str] = []
            for block in content:
                if getattr(block, "type", None) != "text":
                    raise ValueError("content contains a non-text block")
                text = getattr(block, "text", None)
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("text block is malformed")
                text_parts.append(text)
            text = "".join(text_parts).strip()
            if not text:
                raise ValueError("content contains no text")

            return ProviderResponse(
                message=Message(role=MessageRole.ASSISTANT, content=text),
                model=model,
                stop_reason=stop_reason,
            )
        except MalformedProviderResponseError:
            raise
        except Exception as error:
            logger.info("Provider failure: malformed_response")
            raise MalformedProviderResponseError(
                "Anthropic returned an invalid response"
            ) from error
