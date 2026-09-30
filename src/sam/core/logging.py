"""Centralized application logging configuration.

Every handler uses ``RedactingFormatter``: the fully formatted record,
tracebacks included, is scrubbed of credential-shaped values (API keys,
bearer / basic authorization, known header names, private keys) before it
is written anywhere. Production (a durable data directory) also writes a
bounded, rotating, owner-only local log file (``logs/sam.log``: at most
``LOG_BACKUPS + 1`` files of ``LOG_MAX_BYTES``). Nothing is uploaded; there
is no crash reporter. Sam's own code logs metadata only (reason codes,
ids); prompts, document text, e-mail bodies, answers and biometric data are
never passed to the logger.
"""

import io
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_THIRD_PARTY_LOGGERS = ("anthropic", "httpx", "httpcore")
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 4
REDACTED = "[REDACTED]"

_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(
        r"(?i)\b(authorization|x-api-key|api[_-]?key|x-sam-bridge-token|"
        r"password|secret|token)\b(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    ),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class PrivateRotatingFileHandler(RotatingFileHandler):
    """Every (re)opened log file is owner-only (0600) and never followed
    through a symlink, including the files created by rotation."""

    def _open(self) -> io.TextIOWrapper:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.baseFilename, flags, 0o600)
        os.fchmod(fd, 0o600)
        return io.TextIOWrapper(os.fdopen(fd, "ab"), encoding=self.encoding or "utf-8")


def configure_logging(log_level: str, *, log_dir: Path | None = None) -> None:
    """Configure consistent, redacted logging for the application."""

    level = getattr(logging, log_level.upper(), None)
    if not isinstance(level, int):
        raise ValueError(f"Unsupported log level: {log_level}")

    logging.basicConfig(level=level, format=_LOG_FORMAT, force=True)
    root = logging.getLogger()
    for handler in root.handlers:
        handler.setFormatter(RedactingFormatter(_LOG_FORMAT))
    if log_dir is not None:
        from sam.storage.paths import ensure_private_dir, ensure_private_file

        folder = ensure_private_dir(log_dir)
        path = ensure_private_file(folder / "sam.log")
        file_handler = PrivateRotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
        file_handler.setFormatter(RedactingFormatter(_LOG_FORMAT))
        root.addHandler(file_handler)
    logging.getLogger("uvicorn.access").setLevel(level)
    for logger_name in _THIRD_PARTY_LOGGERS:
        third_party_logger = logging.getLogger(logger_name)
        third_party_logger.setLevel(logging.WARNING)
        third_party_logger.propagate = True
