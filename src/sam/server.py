"""Development server launcher using validated application settings."""

import uvicorn

from sam.core.config import Settings, get_settings


def run_server(settings: Settings | None = None) -> None:
    """Run the API using the same settings source as the application."""

    resolved_settings = settings or get_settings()
    uvicorn.run(
        "sam.main:app",
        host=resolved_settings.api_host,
        port=resolved_settings.api_port,
    )


if __name__ == "__main__":
    run_server()
