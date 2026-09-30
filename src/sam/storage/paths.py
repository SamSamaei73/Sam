"""Where Sam keeps durable data, and how those paths are made owner-only.

The data directory is the platform application-data directory for the desktop
app identity (macOS: ``~/Library/Application Support/app.sam.desktop``),
computed from the running user's home directory, never hard-coded, and never
inside the Git repository. It can be overridden by trusted local
configuration (``SAM_DATA_DIR``) only.

Every Sam-owned directory is created ``0700`` and every file ``0600``; both are
verified after creation (the umask is not trusted), and a symlink anywhere in
the final path component is refused, so another local user or a planted link
cannot redirect Sam's database or backups.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

APP_IDENTIFIER = "app.sam.desktop"
DATABASE_NAME = "sam.sqlite3"
LOCK_NAME = "sam.lock"
BACKUP_DIR = "backups"
LOG_DIR = "logs"
DIR_MODE = 0o700
FILE_MODE = 0o600


class UnsafeStoragePath(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def default_data_dir(platform: str | None = None, home: Path | None = None) -> Path:
    base = home if home is not None else Path.home()
    if (platform or sys.platform) == "darwin":
        return base / "Library" / "Application Support" / APP_IDENTIFIER
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg and Path(xdg).is_absolute() else base / ".local" / "share"
    return root / APP_IDENTIFIER


def _check_owned(path: Path, info: os.stat_result, mode: int) -> None:
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise UnsafeStoragePath("storage_not_owned_by_user")
    if stat.S_IMODE(info.st_mode) & ~mode:
        raise UnsafeStoragePath("storage_permissions_too_open")


def ensure_private_dir(path: Path) -> Path:
    """Create (if needed) and verify an owner-only directory."""

    if not path.is_absolute():
        raise UnsafeStoragePath("storage_path_not_absolute")
    if path.is_symlink():
        raise UnsafeStoragePath("storage_path_is_symlink")
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise UnsafeStoragePath("storage_path_is_not_directory")
    os.chmod(path, DIR_MODE, follow_symlinks=False)
    _check_owned(path, os.lstat(path), DIR_MODE)
    return path


def ensure_private_file(path: Path) -> Path:
    """Create (if needed, without following links) and verify an owner-only
    regular file."""

    if path.is_symlink():
        raise UnsafeStoragePath("storage_path_is_symlink")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, FILE_MODE)
    except OSError:
        raise UnsafeStoragePath("storage_file_unavailable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeStoragePath("storage_path_is_not_file")
        os.fchmod(fd, FILE_MODE)
        _check_owned(path, os.fstat(fd), FILE_MODE)
    finally:
        os.close(fd)
    return path


def verify_private_file(path: Path) -> None:
    """Re-check an existing file (e.g. SQLite's own -wal / -shm files)."""

    if path.is_symlink():
        raise UnsafeStoragePath("storage_path_is_symlink")
    if path.exists():
        os.chmod(path, FILE_MODE, follow_symlinks=False)
        _check_owned(path, os.lstat(path), FILE_MODE)


__all__ = [
    "APP_IDENTIFIER",
    "BACKUP_DIR",
    "DATABASE_NAME",
    "DIR_MODE",
    "FILE_MODE",
    "LOCK_NAME",
    "LOG_DIR",
    "UnsafeStoragePath",
    "default_data_dir",
    "ensure_private_dir",
    "ensure_private_file",
    "verify_private_file",
]
