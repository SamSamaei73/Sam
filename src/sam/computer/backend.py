"""The OS-interaction boundary.

``ComputerBackend`` is a ``Protocol`` exposing exactly the ten operations
Phase 5's capabilities need — never ``run``/``shell``/``execute``/``eval``.
``ComputerController`` (see ``sam.computer.controller``) owns policy and
safety; a backend only performs the literal OS call it is asked for and
raises a typed ``BackendError`` on failure. It never sees a
``PermissionRequest`` or a ``PermissionDecision`` — by the time the
controller calls a backend method, authorization has already happened.

Phase 5 ships two backends, deliberately no real OS backend:

- ``FakeComputerBackend`` — a deterministic test double that records
  every call it receives (as bounded, content-free structured records —
  never raw typed text) and returns configurable results.
- ``UnimplementedComputerBackend`` — a structurally complete backend that
  always raises ``BackendUnavailableError``. It exists so the full
  policy → Permission Engine → controller → backend pipeline is real and
  testable end-to-end even though no real OS automation is wired in yet.

See ``docs/computer-control.md``'s "Why no real OS backend" section for
the reasoning: this session runs headless (no interactive display to
drive or verify against), and a screen/mouse/keyboard automation
dependency is exactly the kind of new, large-surface, native-code
dependency that deserves its own deliberate review — not one bundled
into an already-large phase. A future ``MacOSBackend`` (or
``WindowsBackend`` / ``LinuxBackend`` / ``RemoteComputerBackend``) only
has to implement this ten-method Protocol; nothing above it changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Protocol

from sam.computer.errors import BackendUnavailableError
from sam.computer.models import (
    ActiveWindow,
    ModifierKey,
    MouseButton,
    ScreenshotResult,
    ScreenSize,
    ScrollDirection,
    utc_now,
)


class ComputerBackend(Protocol):
    """The only surface a concrete OS backend implements.

    Every method performs exactly one bounded OS operation and returns or
    raises — there is no ``run``/``shell``/``execute`` escape hatch
    anywhere on this Protocol.
    """

    def get_screen_size(self) -> ScreenSize: ...

    def get_active_window(self) -> ActiveWindow | None: ...

    def screenshot(self) -> ScreenshotResult: ...

    def move_mouse(self, *, x: int, y: int) -> None: ...

    def click(self, *, x: int, y: int, button: MouseButton) -> None: ...

    def double_click(self, *, x: int, y: int, button: MouseButton) -> None: ...

    def scroll(
        self, *, x: int, y: int, direction: ScrollDirection, amount: int
    ) -> None: ...

    def press_key(self, *, key: str) -> None: ...

    def hotkey(self, *, modifiers: tuple[ModifierKey, ...], key: str) -> None: ...

    def type_text(self, *, text: str) -> None: ...


class UnimplementedComputerBackend:
    """Every method is wired but raises ``BackendUnavailableError``.

    Not a policy decision, not a permanent design choice — an honest
    placeholder until a real per-OS backend is implemented (Phase 6+).
    Using it end-to-end proves the rest of the pipeline (validation,
    permission mapping, confirmation, audit, fail-closed behavior) works
    correctly without pretending a real screen is being controlled.
    """

    def get_screen_size(self) -> ScreenSize:
        raise BackendUnavailableError("no real OS backend is configured")

    def get_active_window(self) -> ActiveWindow | None:
        raise BackendUnavailableError("no real OS backend is configured")

    def screenshot(self) -> ScreenshotResult:
        raise BackendUnavailableError("no real OS backend is configured")

    def move_mouse(self, *, x: int, y: int) -> None:
        raise BackendUnavailableError("no real OS backend is configured")

    def click(self, *, x: int, y: int, button: MouseButton) -> None:
        raise BackendUnavailableError("no real OS backend is configured")

    def double_click(self, *, x: int, y: int, button: MouseButton) -> None:
        raise BackendUnavailableError("no real OS backend is configured")

    def scroll(
        self, *, x: int, y: int, direction: ScrollDirection, amount: int
    ) -> None:
        raise BackendUnavailableError("no real OS backend is configured")

    def press_key(self, *, key: str) -> None:
        raise BackendUnavailableError("no real OS backend is configured")

    def hotkey(self, *, modifiers: tuple[ModifierKey, ...], key: str) -> None:
        raise BackendUnavailableError("no real OS backend is configured")

    def type_text(self, *, text: str) -> None:
        raise BackendUnavailableError("no real OS backend is configured")


@dataclass(frozen=True)
class RecordedCall:
    """One structured, content-safe record of a backend call.

    ``type_text`` is recorded as a length only — see the module and
    ``docs/computer-control.md``'s secret-handling section — never the
    text itself.
    """

    method: str
    x: int | None = None
    y: int | None = None
    button: MouseButton | None = None
    direction: ScrollDirection | None = None
    amount: int | None = None
    key: str | None = None
    modifiers: tuple[ModifierKey, ...] | None = None
    text_length: int | None = None


class FakeComputerBackend:
    """A deterministic, in-memory backend for tests.

    Not a global singleton — each instance owns its own call log and
    configuration behind a lock, so two instances (and therefore two
    tests) never see each other's calls.
    """

    def __init__(
        self,
        *,
        screen_size: ScreenSize | None = None,
        active_window: ActiveWindow | None = None,
    ) -> None:
        self._screen_size = screen_size or ScreenSize(width=1920, height=1080)
        self._active_window = active_window
        self._calls: list[RecordedCall] = []
        self._lock = RLock()
        self._raise_on: set[str] = set()

    def fail_next(self, method: str) -> None:
        """Test helper: make the next call to ``method`` raise, to prove
        the controller never reports a backend failure as success."""

        with self._lock:
            self._raise_on.add(method)

    def calls(self) -> tuple[RecordedCall, ...]:
        with self._lock:
            return tuple(self._calls)

    def _maybe_fail(self, method: str) -> None:
        with self._lock:
            if method in self._raise_on:
                self._raise_on.discard(method)
                raise BackendUnavailableError(f"configured to fail: {method}")

    def _record(self, call: RecordedCall) -> None:
        with self._lock:
            self._calls.append(call)

    def get_screen_size(self) -> ScreenSize:
        self._maybe_fail("get_screen_size")
        self._record(RecordedCall(method="get_screen_size"))
        return self._screen_size

    def get_active_window(self) -> ActiveWindow | None:
        self._maybe_fail("get_active_window")
        self._record(RecordedCall(method="get_active_window"))
        return self._active_window

    def screenshot(self) -> ScreenshotResult:
        self._maybe_fail("screenshot")
        self._record(RecordedCall(method="screenshot"))
        return ScreenshotResult(
            width=self._screen_size.width,
            height=self._screen_size.height,
            data=b"",  # a fake, empty image — never real screen content
            captured_at=utc_now(),
        )

    def move_mouse(self, *, x: int, y: int) -> None:
        self._maybe_fail("move_mouse")
        self._record(RecordedCall(method="move_mouse", x=x, y=y))

    def click(self, *, x: int, y: int, button: MouseButton) -> None:
        self._maybe_fail("click")
        self._record(RecordedCall(method="click", x=x, y=y, button=button))

    def double_click(self, *, x: int, y: int, button: MouseButton) -> None:
        self._maybe_fail("double_click")
        self._record(RecordedCall(method="double_click", x=x, y=y, button=button))

    def scroll(
        self, *, x: int, y: int, direction: ScrollDirection, amount: int
    ) -> None:
        self._maybe_fail("scroll")
        self._record(
            RecordedCall(
                method="scroll", x=x, y=y, direction=direction, amount=amount
            )
        )

    def press_key(self, *, key: str) -> None:
        self._maybe_fail("press_key")
        self._record(RecordedCall(method="press_key", key=key))

    def hotkey(self, *, modifiers: tuple[ModifierKey, ...], key: str) -> None:
        self._maybe_fail("hotkey")
        self._record(RecordedCall(method="hotkey", modifiers=modifiers, key=key))

    def type_text(self, *, text: str) -> None:
        self._maybe_fail("type_text")
        # Never store the raw text — length only, even in the fake.
        self._record(RecordedCall(method="type_text", text_length=len(text)))
