"""Application errors raised by agent execution."""


class AgentError(Exception):
    """Base class for errors safe to translate at the API boundary."""

    status_code = 500
    error_code = "agent_error"


class InvalidRequestError(AgentError):
    """The agent request is invalid or empty."""

    status_code = 400
    error_code = "invalid_request"


class AuthenticationError(AgentError):
    """The provider could not authenticate the configured account."""

    status_code = 502
    error_code = "provider_authentication_failed"


class ProviderTimeoutError(AgentError):
    """The provider did not respond before the configured timeout."""

    status_code = 504
    error_code = "provider_timeout"


class ProviderUnavailableError(AgentError):
    """The provider could not be reached."""

    status_code = 503
    error_code = "provider_unavailable"


class ProviderRateLimitError(AgentError):
    """The provider rate limit was reached."""

    status_code = 429
    error_code = "provider_rate_limited"


class ProviderOverloadedError(AgentError):
    """The provider is temporarily overloaded."""

    status_code = 503
    error_code = "provider_overloaded"


class ProviderServerError(AgentError):
    """The provider returned a server-side failure."""

    status_code = 502
    error_code = "provider_server_error"


class ProviderRequestError(AgentError):
    """The provider rejected or failed to process a request."""

    status_code = 502
    error_code = "provider_request_failed"


class MalformedProviderResponseError(AgentError):
    """The provider returned a response outside the expected contract."""

    status_code = 502
    error_code = "malformed_provider_response"


class AgentExecutionError(AgentError):
    """The agent failed without a more specific safe classification."""

    status_code = 500
    error_code = "agent_execution_failed"


class PolicyBlockedError(AgentError):
    """Sam's own privacy, cost or owner policy refused the request before any
    provider was called."""

    status_code = 403
    error_code = "blocked_by_policy"


class ProviderRefusalError(AgentError):
    """The selected provider declined the request under its own policy. This is
    not an outage and Sam does not work around it."""

    status_code = 422
    error_code = "provider_policy_limit"
