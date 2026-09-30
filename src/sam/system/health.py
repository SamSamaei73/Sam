"""Structured, content-free system health.

``READY`` / ``DEGRADED`` / ``BLOCKED`` overall, plus one state per subsystem,
each with at most a safe reason code. Health never contains database
content, paths, secrets, credential prefixes or exception text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from sam.career.attempts import AttemptState
from sam.storage.backup import list_backups
from sam.storage.migrations import CURRENT_VERSION
from sam.system.secrets import CredentialSource

if TYPE_CHECKING:
    from sam.desktop.runtime import DesktopRuntime


class Overall(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class Component(StrEnum):
    OK = "ok"
    IN_MEMORY = "in_memory"
    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"


SUBSYSTEMS = (
    "database",
    "migrations",
    "keychain",
    "model_router",
    "knowledge",
    "professional",
    "proactive",
    "career",
    "voice",
    "tts",
    "mcp",
    "computer",
    "coding",
)


@dataclass(frozen=True)
class SubsystemHealth:
    name: str
    status: Component
    reason_code: str | None = None


@dataclass(frozen=True)
class SystemHealth:
    status: Overall
    phase: str
    reason_code: str | None
    storage_mode: str
    schema_version: int | None
    last_backup_at: str | None
    backup_count: int
    scheduler: Literal["off", "on", "on_persistent"]
    reconciliation_required: int
    subsystems: tuple[SubsystemHealth, ...]


def system_health(runtime: DesktopRuntime) -> SystemHealth:
    report = runtime.startup
    durable = runtime.durable
    blocked = report.blocked
    subs: dict[str, SubsystemHealth] = {}

    def put(name: str, status: Component, reason: str | None = None) -> None:
        subs[name] = SubsystemHealth(name, status, reason)

    storage_phases = {
        "bootstrap",
        "data_directory_check",
        "database_open",
        "integrity_check",
        "migration",
        "recovery",
    }
    if blocked and report.phase.value in storage_phases:
        put("database", Component.BLOCKED, report.reason_code)
    elif durable is not None:
        put("database", Component.OK)
    else:
        put("database", Component.IN_MEMORY, "not_durable")
    if durable is None:
        put(
            "migrations",
            Component.BLOCKED if blocked else Component.NOT_CONFIGURED,
            report.reason_code if blocked else None,
        )
    elif report.schema_version == CURRENT_VERSION:
        put("migrations", Component.OK)
    else:
        put("migrations", Component.BLOCKED, "schema_not_current")

    credentials = runtime.credentials
    if credentials is None or credentials.keychain == "disabled":
        put("keychain", Component.DISABLED)
    elif credentials.keychain != "ok":
        put("keychain", Component.UNAVAILABLE, "keychain_unavailable")
    elif any(s is CredentialSource.UNAVAILABLE for s in credentials.sources.values()):
        put("keychain", Component.DEGRADED, "credential_unavailable")
    else:
        put("keychain", Component.OK)

    put(
        "model_router",
        Component.OK if runtime.agent_configured else Component.NOT_CONFIGURED,
    )
    put("knowledge", Component.OK, None if durable is not None else "session_only")
    put("professional", Component.OK)
    put("proactive", Component.OK)
    unknown = sum(
        1
        for a in runtime.career.attempts.all()
        if a.state is AttemptState.OUTCOME_UNKNOWN
    )
    put(
        "career",
        Component.DEGRADED if unknown else Component.OK,
        "reconciliation_required" if unknown else "external_actions_unavailable",
    )
    put("voice", Component.OK if runtime.voice_boundary else Component.NOT_CONFIGURED)
    put("tts", Component.OK if runtime.speech_boundary else Component.NOT_CONFIGURED)
    put(
        "mcp",
        Component.OK if runtime.mcp_registry.list_tools() else Component.NOT_CONFIGURED,
    )
    put("computer", Component.DISABLED, "foundation_only")
    put("coding", Component.DISABLED, "foundation_only")
    if blocked:
        for name in SUBSYSTEMS:
            if name not in ("database", "migrations", "keychain"):
                put(name, Component.BLOCKED, "startup_blocked")

    backups = list_backups(durable.data_dir) if durable is not None else ()
    persistent = runtime.owner_settings.scheduler_persistent()
    scheduler: Literal["off", "on", "on_persistent"] = (
        ("on_persistent" if persistent else "on")
        if runtime.proactive.scheduler_enabled
        else "off"
    )
    if blocked:
        overall = Overall.BLOCKED
    elif any(
        s.status in (Component.DEGRADED, Component.UNAVAILABLE, Component.BLOCKED)
        for s in subs.values()
    ):
        overall = Overall.DEGRADED
    else:
        overall = Overall.READY
    return SystemHealth(
        status=overall,
        phase=report.phase.value,
        reason_code=report.reason_code,
        storage_mode=report.storage_mode,
        schema_version=report.schema_version,
        last_backup_at=backups[0].created_at if backups else None,
        backup_count=len(backups),
        scheduler=scheduler,
        reconciliation_required=unknown,
        subsystems=tuple(subs[name] for name in SUBSYSTEMS),
    )


__all__ = [
    "SUBSYSTEMS",
    "Component",
    "Overall",
    "SubsystemHealth",
    "SystemHealth",
    "system_health",
]
