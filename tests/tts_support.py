"""Shared fixtures for the speech-synthesis tests.

No test contacts Fish Audio: the real ``FishAudioProvider`` is exercised only
through ``httpx.MockTransport``. The API key is an obviously synthetic
string, and audio is synthetic MP3-shaped bytes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from sam.permissions.audit import InMemoryAuditSink
from sam.permissions.confirmation import InMemoryConfirmationProvider
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    PermissionAction,
    PermissionGrant,
    PermissionResource,
    PermissionScope,
    Principal,
    PrincipalKind,
)
from sam.permissions.store import InMemoryPermissionStore
from sam.tts.audit import InMemoryTTSAuditSink, TTSAuditSink
from sam.tts.credentials import FakeTTSCredentialProvider, TTSCredentialReference
from sam.tts.fish_audio import FISH_PROVIDER_ID, FishAudioProvider
from sam.tts.gateway import TTSGateway
from sam.tts.models import SynthesisRequest, TrustedVoiceProfile
from sam.tts.profiles import TrustedVoiceProfiles
from sam.tts.provider import FakeSpeechSynthesisProvider, SpeechSynthesisProvider

NOW = datetime.now(UTC)
ALICE = Principal(kind=PrincipalKind.USER, id="alice")
BOB = Principal(kind=PrincipalKind.USER, id="bob")

FAKE_KEY = "FAKE-FISH-KEY-0001-not-a-real-key"
VOICE_REF = "abcdef0123456789abcdef0123456789"
OTHER_VOICE_REF = "0123456789abcdef0123456789abcdef"
SECRET_TEXT = "please read this: my api key is sk-ant-abcdefghijklmnopqrst1234"

# Synthetic MP3-shaped payload: ID3 header + one frame header + filler.
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x11" * 400
FISH_REF = TTSCredentialReference(
    provider_id=FISH_PROVIDER_ID, credential_id="fish-main"
)


def profile(
    profile_id: str = "sam_default",
    *,
    provider_id: str = "fake-tts",
    voice: str = VOICE_REF,
    model: str = "s2.1-pro-free",
    enabled: bool = True,
) -> TrustedVoiceProfile:
    return TrustedVoiceProfile(
        profile_id=profile_id,
        provider_id=provider_id,
        provider_voice_reference=voice,
        provider_model=model,
        enabled=enabled,
    )


class Recorder:
    """A MockTransport handler that records every outbound request."""

    def __init__(
        self,
        response: httpx.Response
        | Callable[[httpx.Request], httpx.Response]
        | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self._response
        if r is None:
            return httpx.Response(
                200, headers={"content-type": "audio/mpeg"}, content=MP3
            )
        if callable(r):
            return r(request)
        return r


def fish_provider(
    handler: Callable[[httpx.Request], httpx.Response] | Recorder,
    *,
    key: str = FAKE_KEY,
) -> tuple[FishAudioProvider, FakeTTSCredentialProvider]:
    creds = FakeTTSCredentialProvider()
    creds.add(FISH_REF, key)
    provider = FishAudioProvider(
        credentials=creds,
        credential_reference=FISH_REF,
        transport=httpx.MockTransport(handler),
    )
    return provider, creds


class TTSHarness:
    """Real registry/PermissionEngine/gateway; fake or Fish-over-MockTransport
    provider."""

    def __init__(
        self,
        *,
        provider: SpeechSynthesisProvider | None = None,
        audit_sink: TTSAuditSink | None = None,
        timeout: float = 5.0,
        grant_all: bool = True,
        extra_profiles: tuple[TrustedVoiceProfile, ...] = (),
    ) -> None:
        self.pstore = InMemoryPermissionStore()
        self.confirmations = InMemoryConfirmationProvider()
        self.permission_audit = InMemoryAuditSink()
        self.pengine = PermissionEngine(
            store=self.pstore,
            confirmation_provider=self.confirmations,
            audit_sink=self.permission_audit,
        )
        self.provider: SpeechSynthesisProvider = (
            provider if provider is not None else FakeSpeechSynthesisProvider()
        )
        pid = self.provider.provider_id
        self.profiles = TrustedVoiceProfiles(
            [
                profile("sam_default", provider_id=pid),
                profile("other_voice", provider_id=pid, voice=OTHER_VOICE_REF),
                profile("off_voice", provider_id=pid, enabled=False),
                *extra_profiles,
            ]
        )
        self.audit = audit_sink if audit_sink is not None else InMemoryTTSAuditSink()
        self.gateway = TTSGateway(
            permission_engine=self.pengine,
            provider=self.provider,
            profiles=self.profiles,
            audit_sink=self.audit,
            provider_timeout_seconds=timeout,
        )
        self._seq = 0
        if grant_all:
            for name in ("sam_default", "other_voice", "off_voice"):
                self.grant(name)

    def grant(
        self,
        profile_id: str,
        *,
        principal: Principal = ALICE,
        provider_id: str | None = None,
        resource: PermissionResource = PermissionResource.SPEECH_SYNTHESIS,
        action: PermissionAction = PermissionAction.SEND,
        segments: tuple[str, ...] | None = None,
        expires_at: datetime | None = None,
        always_confirm: bool = False,
    ) -> str:
        self._seq += 1
        gid = f"g{self._seq}"
        scope = segments or (provider_id or self.provider.provider_id, profile_id)
        self.pstore.create_grant(
            PermissionGrant(
                grant_id=gid,
                principal=principal,
                resource=resource,
                action=action,
                scope=PermissionScope(segments=scope),
                always_require_confirmation=always_confirm,
                created_at=NOW,
                updated_at=NOW,
                expires_at=expires_at,
            )
        )
        return gid

    def request(
        self,
        text: str = "Hello, this is Sam speaking.",
        *,
        principal: Principal = ALICE,
        profile_id: str = "sam_default",
        request_id: str | None = None,
    ) -> SynthesisRequest:
        kwargs: dict[str, Any] = dict(
            principal=principal, text=text, trusted_voice_profile=profile_id
        )
        if request_id is not None:
            kwargs["request_id"] = request_id
        return SynthesisRequest(**kwargs)

    def approve(self, confirmation_id: str, *, approved: bool = True) -> None:
        self.confirmations.decide(confirmation_id, approved=approved, now=NOW)


def fish_harness(
    handler: Callable[[httpx.Request], httpx.Response] | Recorder | None = None,
    **kwargs: Any,
) -> tuple[TTSHarness, Recorder, FakeTTSCredentialProvider]:
    recorder = handler if isinstance(handler, Recorder) else Recorder(handler)
    provider, creds = fish_provider(recorder)
    return TTSHarness(provider=provider, **kwargs), recorder, creds


def reachable(root: object, limit: int = 40_000) -> list[object]:
    """Every object reachable from ``root`` (attributes, slots, containers,
    bound methods, closures), including str/bytes leaves."""

    import types
    from collections import deque

    seen: set[int] = set()
    out: list[object] = []
    queue: deque[object] = deque([root])
    leaf = (str, bytes, bytearray, int, float, bool, type(None))
    while queue and len(out) < limit:
        obj = queue.popleft()
        if id(obj) in seen or isinstance(obj, types.ModuleType | type):
            continue
        seen.add(id(obj))
        out.append(obj)
        if isinstance(obj, leaf):
            continue
        children: list[object] = []
        try:
            children.extend(vars(obj).values())
        except TypeError:
            pass
        for klass in type(obj).__mro__:
            for slot in getattr(klass, "__slots__", ()):
                if hasattr(obj, slot):
                    children.append(getattr(obj, slot))
        if isinstance(obj, dict | types.MappingProxyType):
            children.extend(obj.keys())
            children.extend(obj.values())
        elif isinstance(obj, list | tuple | set | frozenset | deque):
            children.extend(obj)
        if isinstance(obj, types.MethodType):
            children.extend([obj.__self__, obj.__func__])
        if isinstance(obj, types.FunctionType):
            children.extend(c.cell_contents for c in (obj.__closure__ or ()))
        queue.extend(children)
    return out
