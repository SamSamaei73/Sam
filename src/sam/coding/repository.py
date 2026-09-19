"""The repository boundary: path containment, file I/O, and the only
place any external process is ever invoked.

Two structurally separate concerns live here:

1. **Path containment** (``resolve_within_repository``) — every file
   operation resolves its target through this function, which uses real
   path resolution (``Path.resolve()``, following symlinks) plus
   ``Path.relative_to()`` containment checking, never string-prefix
   matching. A traversal, an absolute path, a null byte, or a symlink
   that resolves outside the repository root is rejected before any
   filesystem call touches it.

2. **Command execution** (``CommandRunner``/``SubprocessCommandRunner``)
   — the single most security-sensitive piece in Phase 6. There is no
   path from any caller/LLM input to a shell string: every invocation is
   a ``ResolvedCommand`` built from ``_COMMAND_REGISTRY``, a static,
   code-reviewed table of (executable name, fixed arguments, timeout) —
   see that registry for the complete, closed set. Executables are
   resolved once, to an absolute path, via a trusted search list that
   deliberately excludes the repository root and the ambient process
   ``PATH`` — see ``_resolve_executable`` — so a malicious repository-
   local file named ``uv``/``git``/``python`` is never the one invoked.
   Subprocesses run with ``shell=False`` (no shell parsing, no
   metacharacters, no injection surface), a minimal explicit
   environment (never ``os.environ`` wholesale), a bounded timeout, and
   bounded, always-flagged-if-truncated output.

``RepositoryBackend`` composes both: it is the "Safe Repository Backend"
in the Phase 6 architecture diagram. It performs no authorization
itself — ``sam.coding.executor.CodingExecutor`` is the only caller, and
only after ``PermissionEngine.evaluate`` has already returned ``ALLOW``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Protocol

from sam.coding.errors import (
    CommandPolicyError,
    ConcurrentModificationError,
    RepositoryBoundaryError,
    RepositoryNotFoundError,
    UnsafeOperationError,
)
from sam.coding.models import (
    MAX_COMMAND_OUTPUT_CHARS,
    MAX_DIRECTORY_DEPTH,
    MAX_FILE_READ_BYTES,
    MAX_FILE_WRITE_BYTES,
    MAX_FILES_PER_LISTING,
    MAX_PATH_LENGTH,
    CodingOperation,
    CommandExecutionResult,
    DirectoryEntry,
    DirectoryListing,
    FileChange,
    FileChangeKind,
    FileReadResult,
    FileWriteResult,
    GitDiffResult,
    GitStatusResult,
    RepositoryContext,
    ResolvedCommand,
)

# --------------------------------------------------------------------- #
# Path containment
# --------------------------------------------------------------------- #


def resolve_within_repository(
    root: Path, relative_path: str, *, allow_root: bool = False
) -> Path:
    """Resolve ``relative_path`` against ``root``, guaranteeing the
    result is within ``root`` — via real path resolution and
    ``relative_to`` containment, never string-prefix matching. Follows
    symlinks (``Path.resolve()``), so a symlink whose real target lands
    outside ``root`` is rejected the same as a literal ``..`` escape.

    Raises ``RepositoryBoundaryError`` for: a null byte, an absolute
    path, an over-length path, a path that resolves outside ``root``,
    or (unless ``allow_root=True``, used only for directory listing) a
    path that resolves to ``root`` itself.
    """

    if "\x00" in relative_path:
        raise RepositoryBoundaryError("path contains a null byte")
    if len(relative_path) > MAX_PATH_LENGTH:
        raise RepositoryBoundaryError("path exceeds the maximum length")
    if relative_path.startswith("/"):
        raise RepositoryBoundaryError("absolute paths are not allowed")
    if not relative_path.strip():
        if allow_root:
            return root
        raise RepositoryBoundaryError("path must not be blank")

    depth = len([part for part in relative_path.split("/") if part not in ("", ".")])
    if depth > MAX_DIRECTORY_DEPTH:
        raise RepositoryBoundaryError("path exceeds the maximum directory depth")

    resolved_root = root.resolve(strict=True)
    resolved_target = (resolved_root / relative_path).resolve()

    try:
        relative = resolved_target.relative_to(resolved_root)
    except ValueError as error:
        raise RepositoryBoundaryError("path escapes the repository root") from error

    if not allow_root and relative == Path("."):
        raise RepositoryBoundaryError("path must not resolve to the repository root")

    return resolved_target


# --------------------------------------------------------------------- #
# Command execution — the only place a subprocess is ever started
# --------------------------------------------------------------------- #


class CommandRunner(Protocol):
    """Contract ``RepositoryBackend`` depends on for process execution."""

    def run(
        self, command: ResolvedCommand, *, cwd: Path, repository_root: Path
    ) -> CommandExecutionResult: ...


# A deliberately small, fixed set of directories that may contain a
# trusted executable — never the repository root, never the process's
# current working directory, and never the ambient inherited PATH
# (which a compromised parent process/shell could have manipulated).
def _trusted_search_path() -> str:
    candidates = [
        os.path.dirname(sys.executable),
        "/usr/local/bin",
        "/opt/homebrew/bin",
        "/usr/bin",
        "/bin",
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".cargo" / "bin"),
    ]
    return os.pathsep.join(dict.fromkeys(d for d in candidates if os.path.isdir(d)))


def _resolve_executable(name: str, *, repository_root: Path) -> Path:
    """Resolve ``name`` to a trusted absolute path.

    Searches only ``_trusted_search_path()`` — never the ambient
    process ``PATH`` and never the repository directory — and rejects
    the result outright if it resolves to anything inside
    ``repository_root`` (a malicious repository-local file named e.g.
    ``uv`` or ``git`` is never the one returned).
    """

    found = shutil.which(name, path=_trusted_search_path())
    if found is None:
        raise CommandPolicyError(f"executable not found in trusted locations: {name!r}")
    resolved = Path(found).resolve()
    try:
        resolved.relative_to(repository_root.resolve())
    except ValueError:
        return resolved
    raise CommandPolicyError(f"refusing repository-local executable: {name!r}")


@dataclass(frozen=True)
class RecordedCommand:
    """One structured, content-safe record of a command invocation —
    used by ``FakeCommandRunner`` in tests. Records the executable name
    and argument *count* only, never full argument text (arguments are
    always internally fixed and non-sensitive today, but this keeps the
    fake consistent with the "never log full content" discipline)."""

    executable_name: str
    argument_count: int
    cwd: Path


class SubprocessCommandRunner:
    """The real implementation: ``subprocess.run`` with no shell, a
    trusted-resolved absolute executable path, a minimal explicit
    environment, a bounded timeout, and bounded output."""

    def run(
        self, command: ResolvedCommand, *, cwd: Path, repository_root: Path
    ) -> CommandExecutionResult:
        executable = _resolve_executable(
            command.executable_name, repository_root=repository_root
        )
        argv = [str(executable), *command.arguments]
        env = {"PATH": _trusted_search_path(), "HOME": os.environ.get("HOME", "")}
        # Never os.environ wholesale — see docs/coding-agent.md's
        # "Environment safety" section. LANG/LC_ALL kept only for
        # predictable command output encoding, not because they carry
        # any credential.
        for key in ("LANG", "LC_ALL"):
            if key in os.environ:
                env[key] = os.environ[key]

        start = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - argv is a fixed list, shell=False, no user string
                argv,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=command.timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            duration = time.monotonic() - start
            return CommandExecutionResult(
                stdout=_bounded(_decode(error.stdout)),
                stderr=_bounded(_decode(error.stderr)),
                exit_code=None,
                timed_out=True,
                stdout_truncated=_is_truncated(_decode(error.stdout)),
                stderr_truncated=_is_truncated(_decode(error.stderr)),
                duration_seconds=duration,
            )
        duration = time.monotonic() - start
        return CommandExecutionResult(
            stdout=_bounded(completed.stdout or ""),
            stderr=_bounded(completed.stderr or ""),
            exit_code=completed.returncode,
            timed_out=False,
            stdout_truncated=_is_truncated(completed.stdout or ""),
            stderr_truncated=_is_truncated(completed.stderr or ""),
            duration_seconds=duration,
        )


def _decode(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _is_truncated(text: str) -> bool:
    return len(text) > MAX_COMMAND_OUTPUT_CHARS


def _bounded(text: str) -> str:
    if len(text) <= MAX_COMMAND_OUTPUT_CHARS:
        return text
    return text[:MAX_COMMAND_OUTPUT_CHARS]


class FakeCommandRunner:
    """A deterministic, in-memory command runner for tests. Never spawns
    a real process. Each instance owns its own state — no global
    mutable state, no shared call log between instances."""

    def __init__(self) -> None:
        self._calls: list[RecordedCommand] = []
        self._results: dict[str, CommandExecutionResult] = {}
        self._lock = RLock()

    def set_result(self, executable_name: str, result: CommandExecutionResult) -> None:
        with self._lock:
            self._results[executable_name] = result

    def calls(self) -> tuple[RecordedCommand, ...]:
        with self._lock:
            return tuple(self._calls)

    def run(
        self, command: ResolvedCommand, *, cwd: Path, repository_root: Path
    ) -> CommandExecutionResult:
        with self._lock:
            self._calls.append(
                RecordedCommand(
                    executable_name=command.executable_name,
                    argument_count=len(command.arguments),
                    cwd=cwd,
                )
            )
            configured = self._results.get(command.executable_name)
        if configured is not None:
            return configured
        return CommandExecutionResult(
            stdout="",
            stderr="",
            exit_code=0,
            timed_out=False,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_seconds=0.0,
        )


# --------------------------------------------------------------------- #
# The static, closed command registry — never caller-influenced
# --------------------------------------------------------------------- #

_COMMAND_REGISTRY: dict[CodingOperation, ResolvedCommand] = {
    CodingOperation.GET_GIT_STATUS: ResolvedCommand(
        executable_name="git",
        arguments=("status", "--porcelain"),
        timeout_seconds=30.0,
    ),
    CodingOperation.GET_GIT_DIFF: ResolvedCommand(
        executable_name="git", arguments=("diff",), timeout_seconds=30.0
    ),
    CodingOperation.VERIFY_DIFF: ResolvedCommand(
        executable_name="git", arguments=("diff", "--check"), timeout_seconds=30.0
    ),
    CodingOperation.RUN_TESTS: ResolvedCommand(
        executable_name="uv", arguments=("run", "pytest"), timeout_seconds=180.0
    ),
    CodingOperation.RUN_LINT: ResolvedCommand(
        executable_name="uv",
        arguments=("run", "ruff", "check", "."),
        timeout_seconds=60.0,
    ),
    CodingOperation.RUN_TYPECHECK: ResolvedCommand(
        executable_name="uv",
        arguments=("run", "mypy", "src", "tests"),
        timeout_seconds=120.0,
    ),
}


def command_for(operation: CodingOperation) -> ResolvedCommand:
    """The fixed command for one operation. Raises ``CommandPolicyError``
    for an operation with no command (file I/O operations have none —
    they never invoke a subprocess)."""

    command = _COMMAND_REGISTRY.get(operation)
    if command is None:
        raise CommandPolicyError(f"{operation.value} has no associated command")
    return command


# --------------------------------------------------------------------- #
# Binary detection
# --------------------------------------------------------------------- #

_BINARY_SNIFF_BYTES = 8_000


def _looks_binary(data: bytes) -> bool:
    sample = data[:_BINARY_SNIFF_BYTES]
    return b"\x00" in sample


# --------------------------------------------------------------------- #
# The repository backend
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class _ListState:
    entries: list[DirectoryEntry] = field(default_factory=list)
    truncated: bool = False


class RepositoryBackend:
    """The safe repository backend: file I/O bounded by
    ``resolve_within_repository``, command execution bounded by
    ``CommandRunner`` + the static registry. Performs no authorization —
    see the module docstring."""

    def __init__(
        self, *, context: RepositoryContext, command_runner: CommandRunner
    ) -> None:
        self._context = context
        self._runner = command_runner
        self._root = Path(context.root)
        if not self._root.exists() or not self._root.is_dir():
            raise RepositoryBoundaryError(
                f"repository root does not exist or is not a directory: {context.root}"
            )

    @property
    def context(self) -> RepositoryContext:
        return self._context

    def _resolve(self, relative_path: str, *, allow_root: bool = False) -> Path:
        target = resolve_within_repository(
            self._root, relative_path, allow_root=allow_root
        )
        if self._context.allowed_paths is not None:
            self._check_allowed_paths(target)
        return target

    def _check_allowed_paths(self, target: Path) -> None:
        allowed = self._context.allowed_paths
        if allowed is None:
            return
        relative = target.relative_to(self._root.resolve())
        if relative == Path("."):
            return
        top_level = relative.parts[0] if relative.parts else ""
        if top_level not in allowed:
            raise RepositoryBoundaryError(
                f"path is outside the repository's allowed_paths: {relative}"
            )

    def read_file(self, relative_path: str) -> FileReadResult:
        target = self._resolve(relative_path)
        if not target.exists():
            raise RepositoryNotFoundError(f"no file at {relative_path!r}")
        if target.is_dir():
            raise RepositoryBoundaryError(
                f"{relative_path!r} is a directory, not a file"
            )
        raw = target.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        is_binary = _looks_binary(raw)
        truncated = len(raw) > MAX_FILE_READ_BYTES
        content: str | None = None
        if not is_binary:
            bounded = raw[:MAX_FILE_READ_BYTES]
            content = bounded.decode("utf-8", errors="replace")
        return FileReadResult(
            path=relative_path,
            content=content,
            content_hash=digest,
            size_bytes=len(raw),
            is_binary=is_binary,
            truncated=truncated,
        )

    def list_files(self, relative_path: str | None) -> DirectoryListing:
        target = self._resolve(relative_path or "", allow_root=True)
        if not target.exists() or not target.is_dir():
            raise RepositoryNotFoundError(f"no directory at {relative_path!r}")
        entries: list[DirectoryEntry] = []
        truncated = False
        root_resolved = self._root.resolve()
        for path in sorted(target.rglob("*")):
            if ".git" in path.relative_to(root_resolved).parts:
                continue
            if len(entries) >= MAX_FILES_PER_LISTING:
                truncated = True
                break
            relative = path.relative_to(root_resolved)
            entries.append(
                DirectoryEntry(
                    path=str(relative),
                    is_directory=path.is_dir(),
                    size_bytes=None if path.is_dir() else path.stat().st_size,
                )
            )
        return DirectoryListing(entries=tuple(entries), truncated=truncated)

    def create_file(self, change: FileChange) -> FileWriteResult:
        if change.operation is not FileChangeKind.CREATE:
            raise UnsafeOperationError("create_file requires a CREATE FileChange")
        target = self._resolve(change.path)
        if target.exists():
            raise ConcurrentModificationError(
                f"{change.path!r} already exists; use modify_file instead"
            )
        return self._write(target, change)

    def modify_file(self, change: FileChange) -> FileWriteResult:
        if change.operation is not FileChangeKind.MODIFY:
            raise UnsafeOperationError("modify_file requires a MODIFY FileChange")
        target = self._resolve(change.path)
        if not target.exists():
            raise RepositoryNotFoundError(f"no file at {change.path!r}")
        current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if current_hash != change.expected_original_hash:
            raise ConcurrentModificationError(
                f"{change.path!r} changed since it was read; refusing to overwrite"
            )
        return self._write(target, change)

    def _write(self, target: Path, change: FileChange) -> FileWriteResult:
        payload = change.new_content.encode("utf-8")
        if len(payload) > MAX_FILE_WRITE_BYTES:
            raise UnsafeOperationError("new content exceeds the maximum write size")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change.new_content, encoding="utf-8")
        written = target.read_bytes()
        return FileWriteResult(
            path=change.path,
            operation=change.operation,
            new_hash=hashlib.sha256(written).hexdigest(),
            bytes_written=len(written),
        )

    def git_status(self) -> GitStatusResult:
        result = self._run(CodingOperation.GET_GIT_STATUS)
        return GitStatusResult(raw=result.stdout, truncated=result.stdout_truncated)

    def git_diff(self) -> GitDiffResult:
        result = self._run(CodingOperation.GET_GIT_DIFF)
        return GitDiffResult(raw=result.stdout, truncated=result.stdout_truncated)

    def run_command(self, operation: CodingOperation) -> CommandExecutionResult:
        return self._run(operation)

    def _run(self, operation: CodingOperation) -> CommandExecutionResult:
        command = command_for(operation)
        return self._runner.run(command, cwd=self._root, repository_root=self._root)


__all__ = [
    "CommandRunner",
    "FakeCommandRunner",
    "RepositoryBackend",
    "SubprocessCommandRunner",
    "command_for",
    "resolve_within_repository",
]
