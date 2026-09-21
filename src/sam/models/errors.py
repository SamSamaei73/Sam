"""Fixed, content-free errors for the model layer."""

from __future__ import annotations

from collections.abc import Mapping

from sam.models.models import FailureCategory


class RouterError(Exception):
    """Base class. Messages are static and never carry prompts or secrets."""


class RegistryError(RouterError):
    """The trusted provider registry was built or used unsafely."""


class ProviderFailure(Exception):
    """A provider adapter's normalized failure. It carries a category and a
    short lowercase code, never a provider error body.

    ``detail`` is OPTIONAL allowlisted diagnostic metadata for the owner-run
    smoke tools (an HTTP status, an allowlisted error status/reason, a sanitized
    truncated message). It is not part of ``str(exception)``, is never used for
    routing, and the router never records it."""

    def __init__(
        self,
        category: FailureCategory,
        code: str = "provider_failure",
        detail: Mapping[str, str | int] | None = None,
    ):
        super().__init__(code)
        self.category = category
        self.code = code
        self.detail: Mapping[str, str | int] | None = detail


__all__ = ["ProviderFailure", "RegistryError", "RouterError"]
