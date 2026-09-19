"""Tests for path containment, file I/O, and command execution."""

import os
import subprocess
from pathlib import Path

import pytest

from sam.coding.errors import (
    CommandPolicyError,
    ConcurrentModificationError,
    RepositoryBoundaryError,
    RepositoryNotFoundError,
)
from sam.coding.models import (
    MAX_FILE_WRITE_BYTES,
    CodingOperation,
    FileChange,
    FileChangeKind,
    RepositoryContext,
    ResolvedCommand,
)
from sam.coding.repository import (
    CommandRunner,
    FakeCommandRunner,
    RepositoryBackend,
    SubprocessCommandRunner,
    _resolve_executable,
    command_for,
    resolve_within_repository,
)


def _init_repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


def _backend(
    tmp_path: Path, *, runner: CommandRunner | None = None
) -> RepositoryBackend:
    ctx = RepositoryContext(repository_id="test-repo", root=str(tmp_path))
    return RepositoryBackend(context=ctx, command_runner=runner or FakeCommandRunner())


# --------------------------------------------------------------------- #
# Path containment
# --------------------------------------------------------------------- #


def test_normal_path_resolves(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    result = resolve_within_repository(tmp_path, "src/main.py")
    assert result.name == "main.py"


def test_traversal_is_rejected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    with pytest.raises(RepositoryBoundaryError):
        resolve_within_repository(tmp_path, "../../etc/passwd")


def test_absolute_path_is_rejected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    with pytest.raises(RepositoryBoundaryError):
        resolve_within_repository(tmp_path, "/etc/passwd")


def test_null_byte_is_rejected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    with pytest.raises(RepositoryBoundaryError):
        resolve_within_repository(tmp_path, "src/\x00evil")


def test_excessively_long_path_is_rejected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    with pytest.raises(RepositoryBoundaryError):
        resolve_within_repository(tmp_path, "x" * 2000)


def test_excessive_directory_depth_is_rejected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    deep = "/".join(["d"] * 20) + "/f.py"
    with pytest.raises(RepositoryBoundaryError):
        resolve_within_repository(tmp_path, deep)


def test_traversal_that_stays_within_bounds_is_allowed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    result = resolve_within_repository(tmp_path, "src/../src/main.py")
    assert result.name == "main.py"


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("outside content")
    (tmp_path / "escape").symlink_to(outside)
    with pytest.raises(RepositoryBoundaryError):
        resolve_within_repository(tmp_path, "escape/secret.txt")


def test_blank_path_is_rejected_for_files(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    with pytest.raises(RepositoryBoundaryError):
        resolve_within_repository(tmp_path, "")


def test_root_itself_is_rejected_unless_allow_root(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    with pytest.raises(RepositoryBoundaryError):
        resolve_within_repository(tmp_path, ".")
    result = resolve_within_repository(tmp_path, "", allow_root=True)
    assert result == tmp_path


# --------------------------------------------------------------------- #
# File read/write
# --------------------------------------------------------------------- #


def test_read_file_returns_content_and_hash(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = _backend(tmp_path)
    result = backend.read_file("src/main.py")
    assert result.content == "print('hi')\n"
    assert len(result.content_hash) == 64
    assert not result.is_binary


def test_read_missing_file_raises(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = _backend(tmp_path)
    with pytest.raises(RepositoryNotFoundError):
        backend.read_file("does/not/exist.py")


def test_read_directory_raises(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = _backend(tmp_path)
    with pytest.raises(RepositoryBoundaryError):
        backend.read_file("src")


def test_binary_file_content_is_withheld(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02binary")
    backend = _backend(tmp_path)
    result = backend.read_file("binary.bin")
    assert result.is_binary is True
    assert result.content is None


def test_create_file_succeeds(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = _backend(tmp_path)
    change = FileChange(
        path="new.py", operation=FileChangeKind.CREATE, new_content="x = 1\n"
    )
    result = backend.create_file(change)
    assert result.bytes_written == 6
    assert (tmp_path / "new.py").read_text() == "x = 1\n"


def test_create_over_existing_file_raises(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = _backend(tmp_path)
    change = FileChange(
        path="src/main.py", operation=FileChangeKind.CREATE, new_content="x"
    )
    with pytest.raises(ConcurrentModificationError):
        backend.create_file(change)


def test_modify_with_correct_hash_succeeds(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = _backend(tmp_path)
    original = backend.read_file("src/main.py")
    change = FileChange(
        path="src/main.py",
        operation=FileChangeKind.MODIFY,
        expected_original_hash=original.content_hash,
        new_content="print('updated')\n",
    )
    result = backend.modify_file(change)
    assert result.new_hash != original.content_hash
    assert (tmp_path / "src" / "main.py").read_text() == "print('updated')\n"


def test_modify_with_stale_hash_is_rejected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = _backend(tmp_path)
    original = backend.read_file("src/main.py")
    # Simulate another process changing the file after it was read.
    (tmp_path / "src" / "main.py").write_text("print('someone else changed this')\n")
    change = FileChange(
        path="src/main.py",
        operation=FileChangeKind.MODIFY,
        expected_original_hash=original.content_hash,
        new_content="print('my change')\n",
    )
    with pytest.raises(ConcurrentModificationError):
        backend.modify_file(change)
    # The file must retain the concurrent change, not be overwritten.
    assert "someone else changed this" in (tmp_path / "src" / "main.py").read_text()


def test_modify_missing_file_raises(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = _backend(tmp_path)
    change = FileChange(
        path="missing.py",
        operation=FileChangeKind.MODIFY,
        expected_original_hash="a" * 64,
        new_content="x",
    )
    with pytest.raises(RepositoryNotFoundError):
        backend.modify_file(change)


def test_write_exceeding_max_bytes_is_rejected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = _backend(tmp_path)
    change = FileChange.model_construct(
        path="huge.py",
        operation=FileChangeKind.CREATE,
        expected_original_hash=None,
        new_content="x" * (MAX_FILE_WRITE_BYTES + 1),
    )
    with pytest.raises(Exception):  # noqa: B017 - Field bound or backend bound
        backend.create_file(change)


def test_write_creates_parent_directories(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = _backend(tmp_path)
    change = FileChange(
        path="a/b/c/new.py", operation=FileChangeKind.CREATE, new_content="x"
    )
    backend.create_file(change)
    assert (tmp_path / "a" / "b" / "c" / "new.py").exists()


# --------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------- #


def test_list_files_excludes_git_directory(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = _backend(tmp_path)
    listing = backend.list_files(None)
    assert all(".git" not in entry.path.split("/") for entry in listing.entries)


def test_list_files_respects_max_count(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    for i in range(1200):
        (tmp_path / f"f{i}.txt").write_text("x")
    backend = _backend(tmp_path)
    listing = backend.list_files(None)
    assert listing.truncated is True
    assert len(listing.entries) <= 1000


def test_list_missing_directory_raises(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = _backend(tmp_path)
    with pytest.raises(RepositoryNotFoundError):
        backend.list_files("does/not/exist")


# --------------------------------------------------------------------- #
# allowed_paths structural restriction
# --------------------------------------------------------------------- #


def test_allowed_paths_restricts_outside_top_level_dirs(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("docs")
    ctx = RepositoryContext(
        repository_id="test-repo", root=str(tmp_path), allowed_paths=("src",)
    )
    backend = RepositoryBackend(context=ctx, command_runner=FakeCommandRunner())
    backend.read_file("src/main.py")  # allowed
    with pytest.raises(RepositoryBoundaryError):
        backend.read_file("docs/readme.md")  # outside allowed_paths


# --------------------------------------------------------------------- #
# Command registry
# --------------------------------------------------------------------- #


def test_command_for_returns_fixed_spec() -> None:
    spec = command_for(CodingOperation.RUN_TESTS)
    assert spec.executable_name == "uv"
    assert spec.arguments == ("run", "pytest")


def test_command_for_file_operation_raises() -> None:
    with pytest.raises(CommandPolicyError):
        command_for(CodingOperation.READ_FILE)


def test_every_command_registry_entry_has_bounded_timeout() -> None:
    for operation in (
        CodingOperation.GET_GIT_STATUS,
        CodingOperation.GET_GIT_DIFF,
        CodingOperation.VERIFY_DIFF,
        CodingOperation.RUN_TESTS,
        CodingOperation.RUN_LINT,
        CodingOperation.RUN_TYPECHECK,
    ):
        spec = command_for(operation)
        assert 0 < spec.timeout_seconds <= 300


# --------------------------------------------------------------------- #
# Command execution — real subprocess
# --------------------------------------------------------------------- #


def test_real_git_status_executes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = _backend(tmp_path, runner=SubprocessCommandRunner())
    result = backend.git_status()
    # An untracked "src" directory shows up as its own porcelain entry
    # ("?? src/") since nothing has been committed yet — the point of
    # this test is that a real `git status` subprocess actually ran and
    # returned porcelain-shaped output, not the exact line content.
    assert "src" in result.raw


def test_real_git_diff_executes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = _backend(tmp_path, runner=SubprocessCommandRunner())
    result = backend.git_diff()
    assert isinstance(result.raw, str)


def test_subprocess_runner_enforces_timeout() -> None:
    runner = SubprocessCommandRunner()
    command = ResolvedCommand(
        executable_name="python3",
        arguments=("-c", "import time; time.sleep(5)"),
        timeout_seconds=1.0,
    )
    result = runner.run(command, cwd=Path("/tmp"), repository_root=Path("/tmp"))
    assert result.timed_out is True
    assert result.exit_code is None


def test_subprocess_runner_bounds_output() -> None:
    runner = SubprocessCommandRunner()
    command = ResolvedCommand(
        executable_name="python3",
        arguments=("-c", "print('x' * 200000)"),
        timeout_seconds=10.0,
    )
    result = runner.run(command, cwd=Path("/tmp"), repository_root=Path("/tmp"))
    assert result.stdout_truncated is True
    assert len(result.stdout) <= 50_000


def test_subprocess_runner_rejects_unknown_executable() -> None:
    runner = SubprocessCommandRunner()
    command = ResolvedCommand(
        executable_name="not-a-real-binary-xyz-123", arguments=(), timeout_seconds=5.0
    )
    with pytest.raises(CommandPolicyError):
        runner.run(command, cwd=Path("/tmp"), repository_root=Path("/tmp"))


# --------------------------------------------------------------------- #
# Executable resolution safety
# --------------------------------------------------------------------- #


def test_repository_local_fake_executable_is_never_resolved(tmp_path: Path) -> None:
    """A malicious file named "git" inside the repository must never be
    the one invoked — real git is found via the trusted search path,
    which never includes the repository root."""

    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\necho PWNED\n")
    fake.chmod(0o755)

    resolved = _resolve_executable("git", repository_root=tmp_path)

    assert resolved != fake
    assert not str(resolved).startswith(str(tmp_path))


def test_repository_local_fake_uv_is_never_resolved(tmp_path: Path) -> None:
    fake = tmp_path / "uv"
    fake.write_text("#!/bin/sh\necho PWNED\n")
    fake.chmod(0o755)

    resolved = _resolve_executable("uv", repository_root=tmp_path)

    assert not str(resolved).startswith(str(tmp_path))


def test_executable_resolution_ignores_cwd_relative_lookup(tmp_path: Path) -> None:
    """Even when invoked with cwd set to the repository (as every real
    command execution is), the resolved executable must still be the
    trusted one, never a "./git"-style relative resolution."""

    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\necho PWNED\n")
    fake.chmod(0o755)
    previous_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        resolved = _resolve_executable("git", repository_root=tmp_path)
        assert not str(resolved).startswith(str(tmp_path))
    finally:
        os.chdir(previous_cwd)


def test_unknown_executable_name_raises() -> None:
    with pytest.raises(CommandPolicyError):
        _resolve_executable(
            "definitely-not-a-real-binary", repository_root=Path("/tmp")
        )
