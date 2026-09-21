"""Where Sam runs. Kept dependency-free so configuration can use it without
importing the agent or model layers."""

from __future__ import annotations

from enum import StrEnum


class DeploymentMode(StrEnum):
    """Where Sam runs. Trusted configuration, never prompt-controlled. The
    Claude subscription provider may operate ONLY in ``OWNER_LOCAL``: Anthropic
    does not allow third-party products to offer claude.ai login, so any
    multi-user, hosted or distributed deployment must fail closed until the
    provider approach is re-reviewed."""

    OWNER_LOCAL = "owner_local"
    MULTI_USER = "multi_user"
    HOSTED = "hosted"
    DISTRIBUTED = "distributed"


__all__ = ["DeploymentMode"]
