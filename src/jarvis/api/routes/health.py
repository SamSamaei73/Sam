"""Health check endpoint."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from jarvis.core.config import Settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Public response shape for the service health check."""

    status: str
    service: str
    environment: str


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """Report whether the API process is available."""

    settings: Settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.app_env,
    )
