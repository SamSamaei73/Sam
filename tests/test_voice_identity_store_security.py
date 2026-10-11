"""Keychain store contract and static security properties of the package."""

from __future__ import annotations

import ast
import pathlib
import re
from datetime import UTC, datetime
from typing import Any

import pytest

import sam.voice_identity as package
from sam.voice_identity.errors import ProfileStoreError
from sam.voice_identity.models import OwnerTemplate
from sam.voice_identity.store import MacOSKeychainVoiceProfileStore
from tests.voice_identity_support import OWNER_VOICE

ROOT = pathlib.Path(package.__file__).parent
SOURCES = {p.name: p.read_text() for p in ROOT.glob("*.py")}


class PasswordDeleteError(Exception):
    pass


class _MacBackend:
    pass


_MacBackend.__module__ = "keyring.backends.macOS"


class _FileBackend:
    pass


_FileBackend.__module__ = "keyrings.alt.file"


class FakeKeyring:
    def __init__(self, backend: object | None = None) -> None:
        self._backend = backend or _MacBackend()
        self.items: dict[tuple[str, str], str] = {}
        self.fail: set[str] = set()

    def get_keyring(self) -> object:
        return self._backend

    def get_password(self, service: str, account: str) -> str | None:
        if "get" in self.fail:
            raise RuntimeError("keychain locked /secret")
        return self.items.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        if "set" in self.fail:
            raise RuntimeError("keychain locked")
        self.items[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        if "delete" in self.fail:
            raise RuntimeError("keychain locked")
        if (service, account) not in self.items:
            raise PasswordDeleteError("not found")
        del self.items[(service, account)]


def template() -> OwnerTemplate:
    return OwnerTemplate(
        vector=tuple(float(x) for x in OWNER_VOICE),
        model_id="fake-ecapa",
        model_revision="r1",
        sample_count=4,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def store(
    kr: FakeKeyring | None = None,
) -> tuple[MacOSKeychainVoiceProfileStore, FakeKeyring]:
    kr = kr or FakeKeyring()
    return MacOSKeychainVoiceProfileStore(keyring_module=kr, platform="darwin"), kr


# --------------------------------------------------------------- keychain


def test_round_trip_through_the_keychain_and_only_the_keychain() -> None:
    s, kr = store()
    assert s.load() is None
    s.save(template())
    assert len(kr.items) == 1
    assert s.load() is not None and s.load().model_id == "fake-ecapa"  # type: ignore[union-attr]
    s.delete()
    assert s.load() is None
    s.delete()  # idempotent


def test_refuses_non_macos_and_non_keychain_backends() -> None:
    with pytest.raises(ProfileStoreError):
        MacOSKeychainVoiceProfileStore(keyring_module=FakeKeyring(), platform="linux")
    with pytest.raises(ProfileStoreError):
        MacOSKeychainVoiceProfileStore(
            keyring_module=FakeKeyring(_FileBackend()), platform="darwin"
        )


def test_read_write_delete_failures_are_reported_and_generic() -> None:
    s, kr = store()
    s.save(template())
    for op, call in [
        ("get", s.load),
        ("set", lambda: s.save(template())),
        ("delete", s.delete),
    ]:
        kr.fail = {op}
        with pytest.raises(ProfileStoreError) as info:
            call()
        assert "secret" not in str(info.value) and info.value.__cause__ is None
    kr.fail = set()
    assert s.load() is not None  # a failed delete left the profile intact


def test_corrupt_keychain_item_is_an_error_not_not_enrolled() -> None:
    s, kr = store()
    kr.items[("app.sam.voice-identity", "owner-template")] = "{not json"
    with pytest.raises(ProfileStoreError):
        s.load()


def test_stored_item_is_the_template_only_no_audio() -> None:
    s, kr = store()
    s.save(template())
    (value,) = kr.items.values()
    assert set(__import__("json").loads(value)) == {
        "v", "model_id", "model_revision", "sample_count", "created_at", "vector",
    }  # fmt: skip


# ------------------------------------------------------- static properties

BANNED_PERSISTENCE = (
    "open(", "sqlite", "shelve", "pickle", "marshal", "json.dump(", "write_text",
    "write_bytes", "tempfile", "os.environ", "getenv", "localStorage",
)  # fmt: skip


def test_no_plaintext_or_filesystem_persistence_in_the_package() -> None:
    for name, text in SOURCES.items():
        code = re.sub(r'""".*?"""', "", text, flags=re.S)
        for banned in BANNED_PERSISTENCE:
            assert banned not in code, f"{name} contains {banned}"


def test_package_has_no_network_printing_or_unrestricted_logging() -> None:
    for name, text in SOURCES.items():
        tree = ast.parse(text)
        imported = {
            n.module.split(".")[0]
            if isinstance(n, ast.ImportFrom) and n.module
            else a.name.split(".")[0]
            for n in ast.walk(tree)
            for a in (n.names if isinstance(n, ast.Import | ast.ImportFrom) else [])
        }
        assert not imported & {
            "requests",
            "httpx",
            "urllib",
            "socket",
            "http",
            "subprocess",
            "torch",
            "speechbrain",
        }, name
        assert "print(" not in text, name
        if name != "verification.py":
            assert "logging" not in imported and "logger" not in text, name
            continue
        # The one allowed diagnostic is local operational metadata. Separate
        # runtime containment tests reject any score, vector, digest or content.
        logging_uses = [
            n.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id == "logging"
        ]
        assert logging_uses == ["getLogger"]
        log_calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "logger"
        ]
        assert len(log_calls) == 1
        call = log_calls[0]
        assert isinstance(call.func, ast.Attribute) and call.func.attr == "info"
        assert not call.keywords and len(call.args) == 5
        assert isinstance(call.args[0], ast.Constant)
        assert call.args[0].value == (
            "Speaker verification result=%s reason=%s duration_ms=%d elapsed_ms=%d"
        )
        allowed_arguments = (
            "result",
            "reason",
            "round(audio.metadata.duration_seconds * 1000)",
            "max(0, round((time.monotonic() - started) * 1000))",
        )
        assert [ast.dump(arg) for arg in call.args[1:]] == [
            ast.dump(ast.parse(expression, mode="eval").body)
            for expression in allowed_arguments
        ]


def test_no_unsafe_model_loading_or_hidden_downloads_in_the_package() -> None:
    joined = "\n".join(SOURCES.values())
    for banned in (
        "trust_remote_code",
        "torch.load",
        "from_pretrained",
        "from_hparams",
        "hf_hub_download",
        "snapshot_download",
        "eval(",
        "exec(",
    ):
        assert banned not in joined


def test_no_module_writes_a_template_or_vector_anywhere_but_the_store() -> None:
    for name, text in SOURCES.items():
        if name in {"store.py", "models.py"}:
            continue
        assert "to_json" not in text, name


def test_no_module_puts_biometrics_in_audit_or_exceptions() -> None:
    audit = re.sub(r'""".*?"""', "", SOURCES["audit.py"], flags=re.S)
    assert (
        "vector" not in audit and "embedding" not in audit and "template" not in audit
    )
    for text in SOURCES.values():
        for match in re.finditer(r"raise \w+\((.*?)\)", text):
            assert "{" not in match.group(1), match.group(1)  # no interpolated values


def test_repr_of_every_public_object_is_redacted() -> None:
    from tests.voice_identity_support import Harness

    h = Harness()
    h.enroll_owner()
    dump: Any = " ".join(
        repr(o)
        for o in (h.store, h.store.load(), h.verifier, h.embedder, h.audit.events())
    )
    for x in OWNER_VOICE[:4]:
        assert f"{x:.5f}"[:7] not in dump
