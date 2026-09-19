"""Centralized application logging configuration."""

import logging

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_THIRD_PARTY_LOGGERS = ("anthropic", "httpx", "httpcore")


def configure_logging(log_level: str) -> None:
    """Configure consistent console logging for the application."""

    level = getattr(logging, log_level.upper(), None)
    if not isinstance(level, int):
        raise ValueError(f"Unsupported log level: {log_level}")

    logging.basicConfig(level=level, format=_LOG_FORMAT, force=True)
    logging.getLogger("uvicorn.access").setLevel(level)
    for logger_name in _THIRD_PARTY_LOGGERS:
        third_party_logger = logging.getLogger(logger_name)
        third_party_logger.setLevel(logging.WARNING)
        third_party_logger.propagate = True
