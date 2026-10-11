"""Production backend entry for the packaged desktop app.

    <Sam.app>/Contents/Resources/backend/python/bin/python3.12 \\
        -I -B -m sam.system.backend

Started ONLY by the desktop shell (``sidecar.rs``) with argv (no shell), a
cleared environment and Python's isolated mode (``-I``: no user site, no
``PYTHON*`` variables, no working directory on ``sys.path``), so it depends
on nothing outside the app bundle and the owner's data directory.

* production mode is required (``APP_ENV=production``) or it refuses to run;
* it binds 127.0.0.1 only, on the port the shell chose; no reload, one worker,
  no proxy headers, no server header, no access log;
* the bridge token is generated per launch by the shell and never leaves it;
* it exits when its stdin reaches EOF: the shell closes the pipe on quit, and
  the kernel closes it if the shell crashes, so it never outlives Sam.app.
"""

from __future__ import annotations

import os
import sys
import threading
from io import StringIO
from pathlib import Path
from typing import TextIO

import uvicorn
from dotenv import dotenv_values

LOOPBACK = "127.0.0.1"
MAX_OPENROUTER_ENV_BYTES = 65_536
OPENROUTER_ENV_NAMES = (
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "OPENROUTER_TTS_MODEL",
    "OPENROUTER_TTS_VOICE",
)


def _load_openrouter_environment() -> str | None:
    """Optionally load OpenRouter settings from an owner-selected file.

    The packaged desktop starts in its application-data directory, so it
    cannot infer a development project's .env. An explicit absolute path
    keeps this independent of cwd. Other dotenv fields never enter the child
    environment, interpolation is disabled, and explicit process values win.
    Return a safe diagnostic code on failure; never include path or contents.
    """

    raw = os.environ.get("SAM_OPENROUTER_ENV_FILE")
    if raw is None:
        return None
    path = Path(raw)
    if not path.is_absolute():
        return "openrouter_env_file_must_be_absolute"
    try:
        if not path.is_file() or path.stat().st_size > MAX_OPENROUTER_ENV_BYTES:
            return "openrouter_env_file_invalid"
        with path.open("rb") as stream:
            data = stream.read(MAX_OPENROUTER_ENV_BYTES + 1)
        if len(data) > MAX_OPENROUTER_ENV_BYTES:
            return "openrouter_env_file_invalid"
        values = dotenv_values(stream=StringIO(data.decode("utf-8")), interpolate=False)
    except (OSError, ValueError, UnicodeError):
        return "openrouter_env_file_unreadable"
    # Validate all selected fields before importing any. A malformed later
    # field must not leave an earlier credential partially installed.
    if any("\x00" in (values.get(name) or "") for name in OPENROUTER_ENV_NAMES):
        return "openrouter_env_file_invalid"
    for name in OPENROUTER_ENV_NAMES:
        value = values.get(name)
        if value is not None:
            os.environ.setdefault(name, value)
    return None


def _port(raw: str | None) -> int | None:
    if raw is None or not raw.isdigit():
        return None
    port = int(raw)
    return port if 1024 <= port <= 65535 else None


def _exit_when_parent_goes(server: uvicorn.Server, stream: TextIO) -> None:
    try:
        while stream.buffer.read(4096):
            pass
    except (OSError, ValueError):
        pass
    server.should_exit = True


def main() -> int:
    if os.environ.get("APP_ENV") != "production":
        print("sam.system.backend: production mode required", file=sys.stderr)
        return 2
    port = _port(os.environ.get("API_PORT"))
    if port is None:
        print("sam.system.backend: invalid port", file=sys.stderr)
        return 2
    env_error = _load_openrouter_environment()
    if env_error is not None:
        print(f"sam.system.backend: {env_error}", file=sys.stderr)
        return 2
    from sam.main import app  # the ONE production app, built at import

    config = uvicorn.Config(
        app,
        host=LOOPBACK,
        port=port,
        reload=False,
        workers=1,
        access_log=False,
        server_header=False,
        date_header=False,
        proxy_headers=False,
        lifespan="on",
        log_config=None,
    )
    server = uvicorn.Server(config)
    threading.Thread(
        target=_exit_when_parent_goes,
        args=(server, sys.stdin),
        name="sam-parent-watch",
        daemon=True,
    ).start()
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
