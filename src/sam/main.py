"""FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sam.agent.core import AgentCore
from sam.agent.errors import AgentError
from sam.api.routes.agent import router as agent_router
from sam.api.routes.health import router as health_router
from sam.core.config import Settings, get_settings
from sam.core.logging import configure_logging
from sam.desktop.api import router as desktop_router
from sam.desktop.identity_api import router as desktop_identity_router
from sam.desktop.models_api import router as desktop_models_router
from sam.desktop.runtime import build_desktop_runtime
from sam.models.adapter import RoutedLLMProvider
from sam.models.factory import build_model_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configure application services during startup and shutdown."""

    settings: Settings = app.state.settings
    provider: RoutedLLMProvider | None = app.state.provider
    configure_logging(settings.log_level)
    logger.info("Starting %s in %s environment", settings.app_name, settings.app_env)
    try:
        if provider is not None:
            provider.open()
        yield
    finally:
        if provider is not None:
            provider.close()
        logger.info("Stopping %s", settings.app_name)


def create_app(
    settings: Settings | None = None,
    agent_core: AgentCore | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""

    resolved_settings = settings or get_settings()
    application = FastAPI(title=resolved_settings.app_name, lifespan=lifespan)
    application.state.settings = resolved_settings
    # ONE trusted model boundary: the router. The paid Anthropic Messages API
    # provider is deliberately not wired here (PAID_FALLBACK = OFF).
    provider: RoutedLLMProvider | None = None
    model_router = None
    model_settings = None
    if agent_core is None:
        model_router, model_settings = build_model_router(resolved_settings)
        provider = RoutedLLMProvider(model_router)
        agent_core = AgentCore(provider)
    application.state.provider = provider
    application.state.model_router = model_router
    application.state.agent_core = agent_core
    # The desktop bridge only exists when a bridge token is configured; without
    # one every /desktop/v1 request fails closed with 503.
    application.state.desktop_runtime = (
        build_desktop_runtime(
            resolved_settings,
            agent_core,
            model_router=model_router,
            model_settings=model_settings,
        )
        if resolved_settings.desktop_bridge_token is not None
        else None
    )
    application.include_router(health_router)
    application.include_router(agent_router)
    application.include_router(desktop_router)
    application.include_router(desktop_identity_router)
    application.include_router(desktop_models_router)
    application.add_exception_handler(AgentError, agent_error_handler)
    return application


def agent_error_handler(request: Request, error: Exception) -> JSONResponse:
    """Return safe, structured errors without provider internals."""

    agent_error = cast(AgentError, error)
    public_messages = {
        "invalid_request": "Invalid agent request",
        "provider_authentication_failed": "Provider authentication failed",
        "provider_timeout": "Provider request timed out",
        "provider_unavailable": "Provider is unavailable",
        "provider_rate_limited": "Provider rate limit reached",
        "provider_overloaded": "Provider is overloaded",
        "provider_server_error": "Provider server error",
        "provider_request_failed": "Provider request failed",
        "malformed_provider_response": "Provider returned an invalid response",
        "blocked_by_policy": "Request blocked by Sam's privacy or cost policy",
        "provider_policy_limit": "The selected provider declined this request",
        "agent_execution_failed": "Agent execution failed",
    }
    return JSONResponse(
        status_code=agent_error.status_code,
        content={
            "error": {
                "code": agent_error.error_code,
                "message": public_messages.get(
                    agent_error.error_code, "Agent request failed"
                ),
            }
        },
    )


app = create_app()
