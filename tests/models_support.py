"""Shared helpers for the multi-model router tests. No test contacts a real
provider: adapters are fakes, mocked HTTP, or a fake ``claude`` script."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sam.agent.models import Message, MessageRole
from sam.models.audit import InMemoryRoutingAuditSink
from sam.models.health import HealthTracker
from sam.models.models import (
    Availability,
    Capability,
    GeminiFreeAttestation,
    ModelRequest,
    PrivacyClass,
    ProviderId,
    TaskProfile,
)
from sam.models.policies import OwnerPreferences, PersonalContentPolicy
from sam.models.provider import ModelProvider
from sam.models.providers.fake import FakeProvider
from sam.models.registry import build_registry
from sam.models.router import ModelRouter

# Secret-shaped fixtures are assembled at runtime so no literal exists in source.
FAKE_API_KEY = "sk-" + "ant-" + "api03-" + "abcdefghijklmnopqrstuvwxyz0123456789"
FAKE_GEMINI_KEY = "AI" + "zaSy" + "FAKEFAKEFAKEFAKEFAKEFAKEFAKE12345"
PRIVATE_TEXT = "PRIVATE-DIARY-LINE about my relationship, do not leak"
NORMAL_TEXT = "What is the capital of France?"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: int) -> None:
        self.now += timedelta(**kw)


def request(
    text: str = NORMAL_TEXT,
    *,
    privacy: PrivacyClass = PrivacyClass.NORMAL,
    profile: TaskProfile = TaskProfile.GENERAL,
    capabilities: frozenset[Capability] | None = None,
    language: str = "auto",
    request_id: str = "req-1",
) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(Message(role=MessageRole.USER, content=text),),
        language=language,  # type: ignore[arg-type]
        capabilities=capabilities or frozenset({Capability.TEXT}),
        privacy_class=privacy,
        task_profile=profile,
    )


@dataclass
class Holder:
    # Routing tests that expect Gemini start from an attested owner; tests of the
    # UNKNOWN default build their own ``OwnerPreferences()``.
    prefs: OwnerPreferences = field(
        default_factory=lambda: OwnerPreferences(
            gemini_attestation=GeminiFreeAttestation.OWNER_ATTESTED_UNBILLED
        )
    )
    content: PersonalContentPolicy = field(default_factory=PersonalContentPolicy)


@dataclass
class Rig:
    router: ModelRouter
    claude: Any
    gemini: Any
    openai: FakeProvider
    grok: FakeProvider
    audit: InMemoryRoutingAuditSink
    holder: Holder
    clock: Clock
    health: HealthTracker

    def paid_calls(self) -> int:
        return self.openai.call_count + self.grok.call_count


def make_rig(
    *,
    claude: ModelProvider | None = None,
    gemini: ModelProvider | None = None,
    gemini_enabled: bool = True,
    claude_enabled: bool = True,
    holder: Holder | None = None,
) -> Rig:
    clock = Clock()
    holder = holder or Holder()
    claude_p = claude or FakeProvider(
        ProviderId.CLAUDE_SUBSCRIPTION, text="from claude"
    )
    gemini_p = gemini or FakeProvider(ProviderId.GEMINI_FREE, text="from gemini")
    openai_p = FakeProvider(ProviderId.OPENAI_API, text="from openai")
    grok_p = FakeProvider(ProviderId.GROK_API, text="from grok")
    audit = InMemoryRoutingAuditSink()
    health = HealthTracker(clock)
    router = ModelRouter(
        registry=build_registry(
            gemini_configured=gemini_enabled, claude_enabled=claude_enabled
        ),
        providers={
            ProviderId.CLAUDE_SUBSCRIPTION: claude_p,
            ProviderId.GEMINI_FREE: gemini_p,
            ProviderId.OPENAI_API: openai_p,
            ProviderId.GROK_API: grok_p,
        },
        preferences=lambda: holder.prefs,
        content_policy=lambda: holder.content,
        audit=audit,
        health=health,
    )
    return Rig(
        router, claude_p, gemini_p, openai_p, grok_p, audit, holder, clock, health
    )


def audit_json(rig: Rig) -> str:
    return json.dumps([e.model_dump(mode="json") for e in rig.audit.events()])


def unavailable() -> Availability:
    return Availability.UNAVAILABLE


def dump(obj: Any) -> str:
    return json.dumps(obj, default=repr)


def make_desktop_router(
    *,
    claude: FakeProvider | None = None,
    gemini: FakeProvider | None = None,
) -> tuple[ModelRouter, Any, dict[str, FakeProvider]]:
    """A router over fakes plus the owner-settings store the Desktop edits."""

    from sam.models.factory import ModelSettingsStore

    store = ModelSettingsStore()
    fakes = {
        "claude": claude
        or FakeProvider(ProviderId.CLAUDE_SUBSCRIPTION, text="claude says hi"),
        "gemini": gemini or FakeProvider(ProviderId.GEMINI_FREE, text="gemini says hi"),
        "openai": FakeProvider(ProviderId.OPENAI_API, text="openai"),
        "grok": FakeProvider(ProviderId.GROK_API, text="grok"),
    }
    router = ModelRouter(
        registry=build_registry(gemini_configured=True),
        providers={
            ProviderId.CLAUDE_SUBSCRIPTION: fakes["claude"],
            ProviderId.GEMINI_FREE: fakes["gemini"],
            ProviderId.OPENAI_API: fakes["openai"],
            ProviderId.GROK_API: fakes["grok"],
        },
        preferences=store.preferences,
        content_policy=store.content,
        audit=InMemoryRoutingAuditSink(),
    )
    return router, store, fakes


def attested(**overrides: Any) -> OwnerPreferences:
    """Owner preferences with the Gemini project attested unbilled (the state in
    which the free provider may be used at all)."""

    overrides.setdefault(
        "gemini_attestation", GeminiFreeAttestation.OWNER_ATTESTED_UNBILLED
    )
    return OwnerPreferences(**overrides)
