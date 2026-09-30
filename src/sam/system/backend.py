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
from typing import TextIO

import uvicorn

LOOPBACK = "127.0.0.1"


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
