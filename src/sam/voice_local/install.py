"""Owner-started installation of Sam's trusted local voice models.

This is the in-app counterpart of the ``sam.voice_local.setup`` admin CLI. It
runs ONLY when the owner explicitly starts it from the desktop app (one
owner-only route); nothing here runs at startup, on a timer, or during a
request.

Trust model:

* WHAT is fetched comes only from the trusted registry: fixed repository ids,
  commit revisions (never a movable tag), file names, sizes and SHA-256s. No
  request, model, prompt or document can name a model, a URL or a file.
* WHERE from: ``https://huggingface.co/<repo>/resolve/<commit>/<file>``.
  Redirects are followed manually, HTTPS only, to ``huggingface.co`` or its
  ``*.hf.co`` CDN, at most ``MAX_REDIRECTS`` times. Proxies and other
  environment settings are ignored (``trust_env=False``).
* Integrity does not depend on the transport: every byte is counted against
  the pinned size (a larger body is cut off) and hashed while streaming. A
  file whose SHA-256 differs is discarded.
* Atomic: files are written to a private staging directory next to the
  final location. Only a complete, fully verified set is renamed into place,
  so an interrupted or failed download can never become active.
* Bounded: finite connect/read timeouts and an overall deadline.
* No code is executed from a download (weights are loaded later, by Sam's own
  loaders, with ``weights_only=True``), and nothing is sent anywhere but the
  file requests themselves: no telemetry, account or token.

A failure leaves voice unavailable; Sam itself (text, History, every other
view) keeps working.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from sam.core.config import Settings
from sam.voice_local.registry import (
    HF_ENDPOINT,
    SPEAKER_MODEL,
    ModelFile,
    TrustedModel,
    model_dir,
    stt_model,
    verify_model,
)

MAX_REDIRECTS = 5
CONNECT_TIMEOUT_SECONDS = 15.0
READ_TIMEOUT_SECONDS = 60.0
INSTALL_DEADLINE_SECONDS = 60 * 60  # the whole install, all files
_CHUNK = 1024 * 1024
_ALLOWED_HOST = "huggingface.co"
_ALLOWED_SUFFIXES = (".huggingface.co", ".hf.co")


class InstallError(Exception):
    """A content-free reason code; never a URL, path or server message."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InstallState(StrEnum):
    NOT_INSTALLED = "not_installed"
    INSTALLING = "installing"
    INSTALLED = "installed"
    FAILED = "failed"


@dataclass(frozen=True)
class InstallStatus:
    state: InstallState
    bytes_done: int
    bytes_total: int
    reason_code: str | None = None


def required_models(settings: Settings) -> tuple[TrustedModel, ...]:
    """Exactly the models voice needs: owner identity + local speech (the same
    recognizer serves the wake word). Chosen by Sam configuration only."""

    return (SPEAKER_MODEL, stt_model(settings.local_stt_model))


def models_installed(root: Path, models: tuple[TrustedModel, ...]) -> bool:
    return all(verify_model(model_dir(root, m), m) for m in models)


def is_allowed_url(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    if parts.port not in (None, 443):
        return False
    return host == _ALLOWED_HOST or host.endswith(_ALLOWED_SUFFIXES)


def file_url(model: TrustedModel, spec: ModelFile) -> str:
    return f"{HF_ENDPOINT}/{model.repo_id}/resolve/{model.revision}/{spec.name}"


class ByteSource(Protocol):
    """Opens a URL (already allowlisted) and yields ``(status, location,
    chunks)``. Swapped for a fake in tests; production uses httpx."""

    def __call__(
        self, url: str
    ) -> AbstractContextManager[tuple[int, str | None, Iterator[bytes]]]: ...


class _HttpxSource:
    def __init__(self) -> None:
        self._client = httpx.Client(
            follow_redirects=False,  # every hop is checked by us
            trust_env=False,  # no proxy / netrc / env CA overrides
            timeout=httpx.Timeout(
                READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS
            ),
            headers={"User-Agent": "Sam-voice-installer"},
        )

    def __call__(
        self, url: str
    ) -> AbstractContextManager[tuple[int, str | None, Iterator[bytes]]]:
        client = self._client

        class _Ctx:
            def __enter__(self) -> tuple[int, str | None, Iterator[bytes]]:
                self._stream = client.stream("GET", url)
                response = self._stream.__enter__()
                return (
                    response.status_code,
                    response.headers.get("location"),
                    response.iter_bytes(_CHUNK),
                )

            def __exit__(self, *exc: object) -> None:
                self._stream.__exit__(None, None, None)

        return _Ctx()

    def close(self) -> None:
        self._client.close()


def download_file(
    model: TrustedModel,
    spec: ModelFile,
    destination: Path,
    source: ByteSource,
    *,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    progress: Callable[[int], None] = lambda _n: None,
) -> None:
    """Stream one pinned file to ``destination``; raise unless it matches the
    pinned size AND SHA-256 exactly."""

    url = file_url(model, spec)
    for _hop in range(MAX_REDIRECTS + 1):
        if not is_allowed_url(url):
            raise InstallError("untrusted_source")
        if clock() > deadline:
            raise InstallError("timeout")
        with source(url) as (status, location, chunks):
            if status in (301, 302, 303, 307, 308):
                if not location:
                    raise InstallError("bad_response")
                url = urljoin(url, location)
                continue
            if status != 200:
                raise InstallError("bad_response")
            digest = hashlib.sha256()
            written = 0
            with destination.open("wb") as handle:
                for chunk in chunks:
                    written += len(chunk)
                    if written > spec.size:
                        raise InstallError("verification_failed")
                    if clock() > deadline:
                        raise InstallError("timeout")
                    digest.update(chunk)
                    handle.write(chunk)
                    progress(len(chunk))
            if written != spec.size or digest.hexdigest() != spec.sha256:
                raise InstallError("verification_failed")
            return
    raise InstallError("too_many_redirects")


def install_models(
    models: tuple[TrustedModel, ...],
    root: Path,
    source: ByteSource,
    *,
    deadline_seconds: float = INSTALL_DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    progress: Callable[[int], None] = lambda _n: None,
) -> None:
    """Install every model that is not already present and verified. Each
    model becomes active only as a complete, verified directory."""

    deadline = clock() + deadline_seconds
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for model in models:
        target = model_dir(root, model)
        if verify_model(target, model):
            progress(model.total_bytes)
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=target.parent))
        try:
            for spec in model.files:
                download_file(
                    model,
                    spec,
                    staging / spec.name,
                    source,
                    deadline=deadline,
                    clock=clock,
                    progress=progress,
                )
            if not verify_model(staging, model):  # belt and braces
                raise InstallError("verification_failed")
            if target.exists():
                shutil.rmtree(target)
            os.rename(staging, target)
        except InstallError:
            raise
        except httpx.TimeoutException:
            raise InstallError("timeout") from None
        except (httpx.HTTPError, OSError):
            raise InstallError("download_failed") from None
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if not verify_model(target, model):
            raise InstallError("verification_failed")


class VoiceModelInstaller:
    """One owner-started install at a time, in a background thread, with a
    content-free status for the UI. Never started automatically."""

    def __init__(
        self,
        root: Path,
        models: tuple[TrustedModel, ...],
        *,
        source_factory: Callable[[], ByteSource] | None = None,
        deadline_seconds: float = INSTALL_DEADLINE_SECONDS,
        installed: bool | None = None,
    ) -> None:
        self._root = root
        self._models = models
        self._source_factory = source_factory or _HttpxSource
        self._deadline_seconds = deadline_seconds
        self._lock = threading.Lock()
        present = models_installed(root, models) if installed is None else installed
        self._state = InstallState.INSTALLED if present else InstallState.NOT_INSTALLED
        self._done = 0
        self._reason: str | None = None
        self._thread: threading.Thread | None = None

    @property
    def total_bytes(self) -> int:
        return sum(m.total_bytes for m in self._models)

    @property
    def models(self) -> tuple[TrustedModel, ...]:
        return self._models

    def status(self) -> InstallStatus:
        with self._lock:
            return InstallStatus(
                self._state, self._done, self.total_bytes, self._reason
            )

    def start(self) -> InstallStatus:
        """Begin installing unless already installed or in progress."""

        with self._lock:
            if self._state in (InstallState.INSTALLING, InstallState.INSTALLED):
                return InstallStatus(
                    self._state, self._done, self.total_bytes, self._reason
                )
            self._state = InstallState.INSTALLING
            self._done = 0
            self._reason = None
            self._thread = threading.Thread(
                target=self._run, name="sam-voice-install", daemon=True
            )
            self._thread.start()
            return InstallStatus(self._state, 0, self.total_bytes, None)

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _progress(self, n: int) -> None:
        with self._lock:
            self._done = min(self._done + n, self.total_bytes)

    def _run(self) -> None:
        source = self._source_factory()
        try:
            install_models(
                self._models,
                self._root,
                source,
                deadline_seconds=self._deadline_seconds,
                progress=self._progress,
            )
        except InstallError as error:
            with self._lock:
                self._state, self._reason = InstallState.FAILED, error.code
            return
        except Exception:
            with self._lock:
                self._state, self._reason = InstallState.FAILED, "download_failed"
            return
        finally:
            close = getattr(source, "close", None)
            if callable(close):
                close()
        with self._lock:
            self._state = InstallState.INSTALLED
            self._done = self.total_bytes


__all__ = [
    "INSTALL_DEADLINE_SECONDS",
    "MAX_REDIRECTS",
    "ByteSource",
    "InstallError",
    "InstallState",
    "InstallStatus",
    "VoiceModelInstaller",
    "download_file",
    "file_url",
    "install_models",
    "is_allowed_url",
    "models_installed",
    "required_models",
]
