"""In-app owner voice setup: the owner-started model installer, first-time
owner security (step-up) setup, and the distinct setup states.

No test touches the network, the real Keychain or the real model cache: the
installer gets a fake byte source and a temporary root, and the Keychain is a
fake write-only callable."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from sam.system.secrets import SecretUnavailable
from sam.voice_local import install as install_mod
from sam.voice_local.install import (
    InstallError,
    InstallState,
    VoiceModelInstaller,
    file_url,
    install_models,
    is_allowed_url,
)
from sam.voice_local.registry import ModelFile, TrustedModel, model_dir, verify_model
from tests.desktop_support import Bridge
from tests.test_desktop_identity import SECRET, IdentityBridge

ALPHA = b"alpha-weights" * 100
BETA = b"beta-config"


def _spec(name: str, data: bytes) -> ModelFile:
    return ModelFile(name, hashlib.sha256(data).hexdigest(), len(data))


MODEL = TrustedModel(
    key="speaker-test",
    repo_id="example/voice-model",
    revision="0123456789abcdef0123456789abcdef01234567",
    license="Apache-2.0",
    files=(_spec("weights.bin", ALPHA), _spec("config.json", BETA)),
)
CDN = "https://us.aws.cdn.hf.co/blob/"


class FakeSource:
    """Serves the pinned files; weights go through one allowlisted redirect,
    like the real Hugging Face CDN. Records every URL it is asked for."""

    def __init__(
        self,
        *,
        files: dict[str, bytes] | None = None,
        redirect: dict[str, str] | None = None,
        status: int = 200,
    ) -> None:
        self.files = (
            files if files is not None else {"weights.bin": ALPHA, "config.json": BETA}
        )
        self.redirect = (
            redirect if redirect is not None else {"weights.bin": CDN + "weights"}
        )
        self.status = status
        self.urls: list[str] = []

    @contextmanager
    def __call__(self, url: str) -> Iterator[tuple[int, str | None, Iterator[bytes]]]:
        self.urls.append(url)
        name = url.rsplit("/", 1)[-1]
        if url.startswith("https://huggingface.co/") and name in self.redirect:
            yield 302, self.redirect[name], iter(())
            return
        if url.startswith(CDN):
            name = "weights.bin"
        data = self.files.get(name, b"")
        yield self.status, None, iter([data[i : i + 7] for i in range(0, len(data), 7)])


# ------------------------------------------------------------- the source


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://huggingface.co/a/b/resolve/c/f", True),
        ("https://us.aws.cdn.hf.co/x", True),
        ("https://cas-bridge.xethub.hf.co/x", True),
        ("https://cdn-lfs.huggingface.co/x", True),
        ("http://huggingface.co/a", False),  # not HTTPS
        ("https://huggingface.co.evil.example/a", False),
        ("https://evilhf.co/a", False),
        ("https://hf.co.evil.example/a", False),
        ("https://user:pw@huggingface.co/a", False),
        ("https://huggingface.co:8443/a", False),
        ("file:///etc/passwd", False),
    ],
)
def test_only_https_huggingface_and_its_cdn_are_allowed(
    url: str, allowed: bool
) -> None:
    assert is_allowed_url(url) is allowed


def test_the_first_request_is_always_the_pinned_commit_url() -> None:
    url = file_url(MODEL, MODEL.files[0])
    assert url == (
        "https://huggingface.co/example/voice-model/resolve/"
        "0123456789abcdef0123456789abcdef01234567/weights.bin"
    )


# ---------------------------------------------------------------- install


def test_install_verifies_and_activates_the_pinned_files(tmp_path: Path) -> None:
    source = FakeSource()
    done: list[int] = []
    install_models((MODEL,), tmp_path, source, progress=done.append)
    target = model_dir(tmp_path, MODEL)
    assert verify_model(target, MODEL)
    assert sum(done) == MODEL.total_bytes
    assert source.urls[0] == file_url(MODEL, MODEL.files[0])
    assert source.urls[1] == CDN + "weights"  # followed the allowlisted redirect
    assert not list(target.parent.glob(".staging-*"))


def test_a_verified_model_is_never_downloaded_again(tmp_path: Path) -> None:
    install_models((MODEL,), tmp_path, FakeSource())
    again = FakeSource()
    install_models((MODEL,), tmp_path, again)
    assert again.urls == []


@pytest.mark.parametrize(
    "source",
    [
        FakeSource(files={"weights.bin": b"x" * len(ALPHA), "config.json": BETA}),
        FakeSource(files={"weights.bin": ALPHA + b"extra", "config.json": BETA}),
        FakeSource(files={"weights.bin": ALPHA[:-1], "config.json": BETA}),
    ],
    ids=["tampered", "oversized", "truncated"],
)
def test_a_file_that_does_not_match_its_pin_never_becomes_active(
    tmp_path: Path, source: FakeSource
) -> None:
    with pytest.raises(InstallError) as caught:
        install_models((MODEL,), tmp_path, source)
    assert caught.value.code == "verification_failed"
    target = model_dir(tmp_path, MODEL)
    assert not target.exists()
    assert not list(target.parent.glob(".staging-*"))


@pytest.mark.parametrize(
    "location",
    [
        "http://us.aws.cdn.hf.co/blob/weights",
        "https://attacker.example/weights",
        "https://huggingface.co.attacker.example/weights",
    ],
)
def test_a_redirect_off_the_allowlist_is_refused_before_any_request(
    tmp_path: Path, location: str
) -> None:
    source = FakeSource(redirect={"weights.bin": location})
    with pytest.raises(InstallError) as caught:
        install_models((MODEL,), tmp_path, source)
    assert caught.value.code == "untrusted_source"
    assert location not in source.urls
    assert not model_dir(tmp_path, MODEL).exists()


def test_redirect_loops_are_bounded(tmp_path: Path) -> None:
    loop = "https://huggingface.co/example/voice-model/resolve/x/weights.bin"
    source = FakeSource(redirect={"weights.bin": loop})
    with pytest.raises(InstallError) as caught:
        install_models((MODEL,), tmp_path, source)
    assert caught.value.code == "too_many_redirects"
    assert len(source.urls) == install_mod.MAX_REDIRECTS + 1


def test_a_server_error_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(InstallError) as caught:
        install_models((MODEL,), tmp_path, FakeSource(status=500))
    assert caught.value.code == "bad_response"
    assert not model_dir(tmp_path, MODEL).exists()


def test_the_whole_install_has_a_deadline(tmp_path: Path) -> None:
    ticks = iter(range(0, 10_000, 100))
    with pytest.raises(InstallError) as caught:
        install_models(
            (MODEL,),
            tmp_path,
            FakeSource(),
            deadline_seconds=150,
            clock=lambda: float(next(ticks)),
        )
    assert caught.value.code == "timeout"
    assert not model_dir(tmp_path, MODEL).exists()


def test_the_installer_never_starts_by_itself(tmp_path: Path) -> None:
    made: list[FakeSource] = []

    def factory() -> FakeSource:
        made.append(FakeSource())
        return made[-1]

    installer = VoiceModelInstaller(tmp_path, (MODEL,), source_factory=factory)
    assert installer.status().state is InstallState.NOT_INSTALLED
    assert made == []  # constructing it downloads nothing
    installer.start()
    installer.join(10)
    status = installer.status()
    assert status.state is InstallState.INSTALLED
    assert status.bytes_done == status.bytes_total == MODEL.total_bytes
    installer.start()  # already installed: nothing new happens
    installer.join(10)
    assert len(made) == 1


def test_a_failed_install_reports_a_content_free_reason(tmp_path: Path) -> None:
    installer = VoiceModelInstaller(
        tmp_path,
        (MODEL,),
        source_factory=lambda: FakeSource(
            redirect={"weights.bin": "https://x.example/"}
        ),
    )
    installer.start()
    installer.join(10)
    status = installer.status()
    assert status.state is InstallState.FAILED
    assert status.reason_code == "untrusted_source"
    assert not model_dir(tmp_path, MODEL).exists()


# ------------------------------------------------------ setup states + API


def _with_installer(bridge: Bridge, tmp_path: Path, **kw: Any) -> VoiceModelInstaller:
    installer = VoiceModelInstaller(
        tmp_path, (MODEL,), source_factory=lambda: FakeSource(**kw)
    )
    bridge.runtime.voice_installer = installer
    bridge.runtime.voice_readiness = "models_missing"
    return installer


def test_missing_models_are_their_own_state_with_an_install_action(
    tmp_path: Path,
) -> None:
    bridge = Bridge()
    installer = _with_installer(bridge, tmp_path)
    body = bridge.get("/voice/identity").json()
    assert body["setup_state"] == "models_missing"
    assert body["models"]["state"] == "not_installed"
    assert body["models"]["bytes_total"] == MODEL.total_bytes
    started = bridge.post("/voice/models/install", {"accept": True}).json()
    assert started["status"] == "ok"
    installer.join(10)
    body = bridge.get("/voice/identity").json()
    # Verified models activate through a backend restart, never hot-wired.
    assert body["setup_state"] == "restart_required"
    assert body["models"]["state"] == "installed"


def test_the_install_route_needs_explicit_acceptance_and_names_nothing(
    tmp_path: Path,
) -> None:
    bridge = Bridge()
    _with_installer(bridge, tmp_path)
    for body in ({}, {"accept": False}, {"accept": True, "url": "https://x"}):
        assert bridge.post("/voice/models/install", body).status_code == 422
    assert (
        bridge.post(
            "/voice/models/install", {"accept": True, "repo_id": "evil/model"}
        ).status_code
        == 422
    )


def test_voice_unavailable_is_distinct_from_missing_models() -> None:
    bridge = Bridge()
    bridge.runtime.voice_readiness = "keychain_unavailable"
    body = bridge.get("/voice/identity").json()
    assert body["setup_state"] == "voice_unavailable"
    assert (
        bridge.post("/voice/models/install", {"accept": True}).json()["status"]
        == "not_configured"
    )


def test_setup_required_then_not_enrolled_then_enrolled() -> None:
    written: list[str] = []
    bridge = IdentityBridge(with_secret=False)
    bridge.runtime._step_up_writer = written.append
    assert bridge.get("/voice/identity").json()["setup_state"] == "setup_required"

    begin = bridge.post(
        "/voice/identity/setup", {"step_up": SECRET, "confirm": SECRET}
    ).json()
    assert begin["status"] == "ok" and begin["session_id"]
    assert written == [SECRET]  # stored once, through the Keychain boundary
    assert bridge.get("/voice/identity").json()["setup_state"] == "not_enrolled"

    for _ in range(4):
        r = bridge.post(
            "/voice/identity/enroll/sample",
            {"session_id": begin["session_id"], "audio_base64": bridge.clip()},
        ).json()
        assert r["accepted"], r
    done = bridge.post(
        "/voice/identity/enroll/complete", {"session_id": begin["session_id"]}
    ).json()
    assert done["status"] == "ok"
    assert bridge.get("/voice/identity").json()["setup_state"] == "enrolled"
    # The new secret now works exactly like one from the Keychain at startup.
    assert bridge.runtime.check_step_up("probe", SECRET) == "ok"


def test_owner_setup_never_echoes_logs_or_persists_the_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge = IdentityBridge(with_secret=False)
    bridge.runtime._step_up_writer = lambda _v: None
    caplog.set_level("DEBUG")
    response = bridge.post(
        "/voice/identity/setup", {"step_up": SECRET, "confirm": SECRET}
    )
    assert SECRET not in response.text
    assert SECRET not in caplog.text
    activity = bridge.get("/activity").text
    assert SECRET not in activity
    assert "Owner security setup" in activity


@pytest.mark.parametrize(
    ("step_up", "confirm", "code"),
    [
        (SECRET, SECRET + "x", "step_up_mismatch"),
        ("short-secret", "short-secret", "step_up_too_short"),
        (" " * 20, " " * 20, "step_up_too_short"),
    ],
)
def test_owner_setup_rejects_bad_input_without_saving(
    step_up: str, confirm: str, code: str
) -> None:
    written: list[str] = []
    bridge = IdentityBridge(with_secret=False)
    bridge.runtime._step_up_writer = written.append
    response = bridge.post(
        "/voice/identity/setup", {"step_up": step_up, "confirm": confirm}
    )
    body = response.json()
    assert body["reason_code"] == code
    assert step_up not in response.text or not step_up.strip()
    assert written == []
    assert not bridge.runtime.step_up_configured()


def test_owner_setup_cannot_replace_an_existing_secret() -> None:
    written: list[str] = []
    bridge = IdentityBridge(with_secret=True)
    bridge.runtime._step_up_writer = written.append
    other = "a-completely-different-secret"
    body = bridge.post(
        "/voice/identity/setup", {"step_up": other, "confirm": other}
    ).json()
    assert body["reason_code"] == "step_up_already_set"
    assert written == []
    assert bridge.runtime.check_step_up("probe", SECRET) == "ok"
    assert bridge.runtime.check_step_up("probe2", other) == "failed"


def test_owner_setup_fails_closed_without_the_keychain() -> None:
    bridge = IdentityBridge(with_secret=False)
    assert bridge.runtime._step_up_writer is None  # tests never get a Keychain
    body = bridge.post(
        "/voice/identity/setup", {"step_up": SECRET, "confirm": SECRET}
    ).json()
    assert body["reason_code"] == "step_up_unavailable"

    def broken(_value: str) -> None:
        raise SecretUnavailable("keychain_write_failed")

    bridge.runtime._step_up_writer = broken
    body = bridge.post(
        "/voice/identity/setup", {"step_up": SECRET, "confirm": SECRET}
    ).json()
    assert body["reason_code"] == "step_up_store_error"
    assert not bridge.runtime.step_up_configured()


def test_guest_mode_cannot_reach_setup_or_install(tmp_path: Path) -> None:
    bridge = IdentityBridge(with_secret=True)
    bridge.enroll()
    _with_installer(bridge, tmp_path)
    assert bridge.start_guest()["status"] == "ok"
    written: list[str] = []
    bridge.runtime._step_up_writer = written.append
    setup = bridge.post("/voice/identity/setup", {"step_up": SECRET, "confirm": SECRET})
    install = bridge.post("/voice/models/install", {"accept": True})
    assert setup.status_code == 403 and install.status_code == 403
    assert written == []
    assert bridge.runtime.voice_installer is not None
    assert bridge.runtime.voice_installer.status().state is InstallState.NOT_INSTALLED


# ------------------------------------------- each safety layer on its own


def test_the_stream_check_alone_rejects_a_tampered_file(tmp_path: Path) -> None:
    spec = MODEL.files[0]
    tampered = FakeSource(files={"weights.bin": b"x" * len(ALPHA), "config.json": BETA})
    with pytest.raises(InstallError) as caught:
        install_mod.download_file(
            MODEL, spec, tmp_path / "weights.bin", tampered, deadline=float("inf")
        )
    assert caught.value.code == "verification_failed"


def test_an_endless_body_is_cut_off_at_the_pinned_size(tmp_path: Path) -> None:
    spec = MODEL.files[0]
    served = {"chunks": 0}

    @contextmanager
    def endless(url: str) -> Iterator[tuple[int, str | None, Iterator[bytes]]]:
        def chunks() -> Iterator[bytes]:
            # "Endless" for the installer, but bounded here (4x the pin) so a
            # regression that removed the cap fails the test instead of
            # filling the disk.
            for _ in range(4 * spec.size // 64):
                served["chunks"] += 1
                yield b"z" * 64

        yield 200, None, chunks()

    with pytest.raises(InstallError) as caught:
        install_mod.download_file(
            MODEL, spec, tmp_path / "weights.bin", endless, deadline=float("inf")
        )
    assert caught.value.code == "verification_failed"
    assert served["chunks"] <= spec.size // 64 + 2  # stopped right after the pin
    assert (tmp_path / "weights.bin").stat().st_size <= spec.size + 64


def test_nothing_unverified_is_ever_activated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the streaming check were wrong, the staged set is verified
    BEFORE it is renamed into place."""

    def faulty_download(model: Any, spec: Any, dest: Path, *_a: Any, **_k: Any) -> None:
        dest.write_bytes(b"not the pinned bytes")  # and no error raised

    monkeypatch.setattr(install_mod, "download_file", faulty_download)
    with pytest.raises(InstallError) as caught:
        install_models((MODEL,), tmp_path, FakeSource())
    assert caught.value.code == "verification_failed"
    target = model_dir(tmp_path, MODEL)
    assert not target.exists()
    assert not list(target.parent.glob(".staging-*"))


def test_the_http_client_ignores_the_environment_and_never_auto_redirects() -> None:
    source = install_mod._HttpxSource()
    try:
        assert source._client.trust_env is False  # no proxy / netrc / env CAs
        assert source._client.follow_redirects is False  # every hop is checked
    finally:
        source.close()
