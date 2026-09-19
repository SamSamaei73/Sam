"""Tests for centralized logging configuration."""

import logging

import pytest
from pytest import MonkeyPatch

from sam.core.logging import configure_logging


def test_configure_logging_uses_requested_level(monkeypatch: MonkeyPatch) -> None:
    """A supported level configures root and Uvicorn access logging."""

    captured: dict[str, object] = {}

    def capture_basic_config(**kwargs: object) -> None:
        captured.update(kwargs)

    access_logger = logging.getLogger("uvicorn.access")
    previous_level = access_logger.level
    monkeypatch.setattr(logging, "basicConfig", capture_basic_config)

    try:
        configure_logging("debug")
    finally:
        configured_level = access_logger.level
        access_logger.setLevel(previous_level)

    assert captured["level"] == logging.DEBUG
    assert captured["force"] is True
    assert configured_level == logging.DEBUG


def test_configure_logging_rejects_unknown_level() -> None:
    """An unsupported level fails before logging is reconfigured."""

    with pytest.raises(ValueError, match="Unsupported log level"):
        configure_logging("verbose")
