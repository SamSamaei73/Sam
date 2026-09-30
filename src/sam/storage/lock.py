"""One Sam owner runtime per data directory.

An exclusive, non-blocking ``flock`` on a Sam-owned lock file, held for the
whole process lifetime. The kernel releases it when the process exits or
crashes, so there are no stale PID files to guess about. A second Sam process
(or a restore while Sam runs) fails to acquire it and stops. This is the
first layer; the database's own UNIQUE constraints are the second, so even
two processes that somehow share a database cannot create duplicate
consequential attempts.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from sam.storage.paths import ensure_private_file


class InstanceLocked(RuntimeError):
    def __init__(self) -> None:
        super().__init__("another_instance_running")
        self.code = "another_instance_running"


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def claim(self) -> InstanceLock:
        ensure_private_file(self.path)
        fd = os.open(self.path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise InstanceLocked() from None
        self._fd = fd
        return self

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> InstanceLock:
        return self.claim()

    def __exit__(self, *_: object) -> None:
        self.release()


__all__ = ["InstanceLock", "InstanceLocked"]
