"""Trusted OpenRouter model selection shared by bootstrap and routing."""

from __future__ import annotations

import re

# Verified against https://openrouter.ai/openai/gpt-4.1-mini on 2026-10-11.
DEFAULT_OPENROUTER_MODEL = "openai/gpt-4.1-mini"
_MODEL_SLUG = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
    r"(?::[a-z0-9][a-z0-9_-]{0,31})?"
)


def validate_openrouter_model_id(value: str) -> str:
    """Accept one explicit model, never a URL or OpenRouter's auto routers.

    The slug selects only a model in a JSON body. The API endpoint is pinned
    separately in the provider; neither prompts nor responses can select it.
    """

    if (
        len(value) > 160
        or _MODEL_SLUG.fullmatch(value) is None
        or value.lower().startswith("openrouter/")
    ):
        raise ValueError("OPENROUTER_MODEL must name one explicit provider/model")
    return value


__all__ = ["DEFAULT_OPENROUTER_MODEL", "validate_openrouter_model_id"]
