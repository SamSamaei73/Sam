"""Gatekeeping for every ``/desktop/v1`` request.

The bridge is meant for exactly one caller: the Tauri shell on the same
machine. This dependency fails closed unless ALL of these hold:

* a bridge token is configured (``DESKTOP_BRIDGE_TOKEN``) — otherwise 503;
* the TCP peer is loopback;
* the ``Host`` header names a loopback host (defeats DNS rebinding);
* there is **no** ``Origin`` header (a browser page sending a cross-site
  request always adds one; the native shell never does);
* the shared token matches (constant-time compare);
* the declared body size is within bounds.

None of this is *authorization* — that is still the PermissionEngine. It only
stops other local processes and web pages from driving the bridge.
"""

from __future__ import annotations

import hmac
from typing import cast

from fastapi import HTTPException, Request

from sam.desktop.models import MAX_REQUEST_BODY_BYTES
from sam.desktop.runtime import DesktopRuntime

TOKEN_HEADER = "x-sam-desktop-token"
_LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _deny(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def _host_name(header: str) -> str:
    value = header.strip().lower()
    if value.startswith("["):  # [::1]:8000
        return value[1:].split("]", 1)[0]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def bridge_runtime(request: Request) -> DesktopRuntime:
    """FastAPI dependency: authenticate the caller, return the runtime."""

    settings = request.app.state.settings
    token = settings.desktop_bridge_token
    if token is None:
        raise _deny(503, "bridge_not_configured")
    peer = request.client.host if request.client else ""
    if peer not in _LOOPBACK_CLIENTS:
        raise _deny(403, "bridge_forbidden")
    if _host_name(request.headers.get("host", "")) not in _LOOPBACK_HOSTS:
        raise _deny(403, "bridge_forbidden")
    if "origin" in request.headers:
        raise _deny(403, "bridge_forbidden")
    supplied = request.headers.get(TOKEN_HEADER, "")
    if not hmac.compare_digest(
        supplied.encode("utf-8"), token.get_secret_value().encode("utf-8")
    ):
        raise _deny(401, "bridge_unauthorized")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            too_big = int(declared) > MAX_REQUEST_BODY_BYTES
        except ValueError:
            raise _deny(400, "bridge_bad_request") from None
        if too_big:
            raise _deny(413, "bridge_payload_too_large")
    runtime = getattr(request.app.state, "desktop_runtime", None)
    if runtime is None:
        raise _deny(503, "bridge_not_configured")
    return cast(DesktopRuntime, runtime)


def owner_bridge_runtime(request: Request) -> DesktopRuntime:
    """Authenticate the bridge AND refuse while Guest Mode is active.

    Every owner-bound route binds the backend's default OWNER principal, so
    UI reachability must not be the boundary: while a Guest Mode session is
    live, these routes are refused here, before any body is processed, any
    provider is called, or any confirmation is touched. Only the owner ending
    Guest Mode (which removes access) and read-only status stay available.
    """

    runtime = bridge_runtime(request)
    identity = runtime.identity
    if identity is not None and identity.guests.current() is not None:
        runtime.activity.add("permission", "Owner route refused", "guest_mode_active")
        raise _deny(403, "guest_mode_active")
    return runtime


__all__ = ["TOKEN_HEADER", "bridge_runtime", "owner_bridge_runtime"]
