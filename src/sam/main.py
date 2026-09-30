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
from sam.desktop.career_api import router as desktop_career_router
from sam.desktop.identity_api import router as desktop_identity_router
from sam.desktop.models_api import router as desktop_models_router
from sam.desktop.proactive_api import router as desktop_proactive_router
from sam.desktop.professional_api import router as desktop_professional_router
from sam.desktop.runtime import build_desktop_runtime
from sam.models.adapter import RoutedLLMProvider
from sam.models.factory import build_model_router
from sam.system.secrets import credential_store_for, resolve_credentials
from sam.system.startup import (
    DurableState,
    StartupPhase,
    StartupReport,
    open_durable_state,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configure application services during startup and shutdown."""

    settings: Settings = app.state.settings
    provider: RoutedLLMProvider | None = app.state.provider
    runtime = app.state.desktop_runtime
    durable: DurableState | None = app.state.durable
    report: StartupReport = app.state.startup
    scheduler = runtime.proactive.scheduler if runtime is not None else None
    configure_logging(
        settings.log_level,
        log_dir=durable.data_dir / "logs" if durable is not None else None,
    )
    logger.info("Starting %s in %s environment", settings.app_name, settings.app_env)
    if report.blocked:
        # Reason code only: never a path, SQL or exception text.
        logger.error("Startup blocked: %s", report.reason_code)
    try:
        if provider is not None and not report.blocked:
            provider.open()
        # Proactive scheduling is off unless the owner turned it on: through
        # trusted local settings, or with the Automations switch set to stay
        # on across restarts (persisted, OFF by default and after migration).
        if (
            scheduler is not None
            and runtime is not None
            and not report.blocked
            and (
                settings.proactive_scheduler_enabled
                or runtime.owner_settings.scheduler_persistent()
            )
        ):
            scheduler.enable()
        report.done(StartupPhase.SCHEDULER_INIT)
        report.done(StartupPhase.READY)
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown()
        if provider is not None:
            provider.close()
        if durable is not None:
            durable.close()
        logger.info("Stopping %s", settings.app_name)


def create_app(
    settings: Settings | None = None,
    agent_core: AgentCore | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""

    resolved_settings = settings or get_settings()
    desktop = resolved_settings.desktop_bridge_token is not None
    # Phase 17 startup: storage phases first (fail closed: BLOCKED, never a
    # silent in-memory fallback for durable data), then credentials.
    report = StartupReport()
    durable = open_durable_state(resolved_settings, report) if desktop else None
    store, keychain_status = credential_store_for(resolved_settings)
    resolved_settings, credentials = resolve_credentials(
        resolved_settings, store, keychain_status
    )
    application = FastAPI(title=resolved_settings.app_name, lifespan=lifespan)
    application.state.settings = resolved_settings
    application.state.durable = durable
    application.state.startup = report
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
            durable=durable,
            startup=report,
            credentials=credentials,
            # Write-only, one credential: lets the owner set the step-up secret
            # the first time from the app. Absent unless the Keychain opened.
            step_up_writer=(
                (lambda value: store.set("desktop_step_up_secret", value))
                if store is not None and keychain_status == "ok"
                else None
            ),
        )
        if desktop
        else None
    )
    report.done(StartupPhase.PROVIDER_INIT)
    application.include_router(health_router)
    application.include_router(agent_router)
    application.include_router(desktop_router)
    application.include_router(desktop_identity_router)
    application.include_router(desktop_models_router)
    application.include_router(desktop_professional_router)
    application.include_router(desktop_proactive_router)
    application.include_router(desktop_career_router)
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
