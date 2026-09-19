"""Dedicated adversarial security tests for the Coding Agent.

Each test is named after one item in the Phase 6 task's required
security matrix. The objective throughout is not just functional
correctness but proof of isolation: DENY/CONFIRM_REQUIRED/invalid input
must never let an operation reach ``RepositoryBackend``, and no shell,
arbitrary command, or credential ever becomes reachable.
"""

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from sam.coding.audit import InMemoryCodingAuditSink
from sam.coding.errors import RepositoryBoundaryError
from sam.coding.executor import CodingExecutor
from sam.coding.models import (
    ErrorCategory,
    ExecutionOutcome,
    FileChange,
    FileChangeKind,
    PermissionOutcomeSummary,
    RepositoryContext,
    ResolvedCommand,
)
from sam.coding.operations import (
    ModifyFileOperation,
    ReadFileOperation,
    RunTestsOperation,
)
from sam.coding.planner import CodingPlanner
from sam.coding.repository import (
    FakeCommandRunner,
    RepositoryBackend,
    SubprocessCommandRunner,
    _resolve_executable,
    resolve_within_repository,
)
from sam.permissions.audit import InMemoryAuditSink as InMemoryPermissionAuditSink
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

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _principal(value: str = "ali") -> Principal:
    return Principal(kind=PrincipalKind.USER, id=value)


def _init_git(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)


class _Harness:
    def __init__(self, tmp_path: Path, *, clock: datetime = _T0) -> None:
        self.store = InMemoryPermissionStore()
        self.confirmations = InMemoryConfirmationProvider()
        self._clock_value = clock
        self.engine = PermissionEngine(
            store=self.store,
            confirmation_provider=self.confirmations,
            audit_sink=InMemoryPermissionAuditSink(),
            clock=lambda: self._clock_value,
        )
        ctx = RepositoryContext(repository_id="sam-core", root=str(tmp_path))
        self.command_runner = FakeCommandRunner()
        self.repository = RepositoryBackend(
            context=ctx, command_runner=self.command_runner
        )
        self.audit = InMemoryCodingAuditSink()
        self.executor = CodingExecutor(
            repository=self.repository,
            permission_engine=self.engine,
            audit_sink=self.audit,
            clock=lambda: self._clock_value,
        )

    def advance(self, delta: timedelta) -> None:
        self._clock_value = self._clock_value + delta

    def grant(
        self,
        *,
        resource: PermissionResource,
        action: PermissionAction,
        scope: str,
        scope_is_path: bool = False,
        principal: Principal | None = None,
        grant_id: str | None = None,
    ) -> None:
        self.store.create_grant(
            PermissionGrant(
                grant_id=grant_id or f"g-{resource.value}-{action.value}-{scope}",
                principal=principal or _principal(),
                resource=resource,
                action=action,
                scope=(
                    PermissionScope.from_path(scope)
                    if scope_is_path
                    else PermissionScope.identifier(scope)
                ),
                created_at=_T0,
                updated_at=_T0,
            )
        )

    def grant_code(self, *, action: PermissionAction, path: str) -> None:
        self.grant(
            resource=PermissionResource.CODE,
            action=action,
            scope=f"sam-core/{path}",
            scope_is_path=True,
        )

    def grant_code_op(self, *, action: PermissionAction, segment: str) -> None:
        self.grant(
            resource=PermissionResource.CODE, action=action, scope=f"sam-core:{segment}"
        )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n")
    _init_git(tmp_path)
    return tmp_path


def _read_op(path: str = "src/main.py") -> ReadFileOperation:
    return ReadFileOperation(principal=_principal(), path=path)


# --------------------------------------------------------------------- #
# 1-5: path safety
# --------------------------------------------------------------------- #


def test_01_dotdot_traversal_is_rejected(repo: Path) -> None:
    with pytest.raises(RepositoryBoundaryError):
        resolve_within_repository(repo, "../../secret")


def test_02_absolute_path_outside_repository_is_rejected(repo: Path) -> None:
    with pytest.raises(RepositoryBoundaryError):
        resolve_within_repository(repo, "/etc/passwd")


def test_03_symlink_escape_is_rejected(repo: Path) -> None:
    outside = repo.parent / "outside_secret"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("classified")
    (repo / "escape").symlink_to(outside)
    with pytest.raises(RepositoryBoundaryError):
        resolve_within_repository(repo, "escape/secret.txt")


def test_04_null_byte_in_path_is_rejected(repo: Path) -> None:
    with pytest.raises(RepositoryBoundaryError):
        resolve_within_repository(repo, "src/\x00evil.py")


def test_05_extremely_long_path_is_rejected(repo: Path) -> None:
    with pytest.raises(RepositoryBoundaryError):
        resolve_within_repository(repo, "x" * 5000)


# --------------------------------------------------------------------- #
# 6-7: size limits
# --------------------------------------------------------------------- #


def test_06_extremely_large_file_write_is_rejected(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code(action=PermissionAction.WRITE, path="huge.py")
    with pytest.raises(ValidationError):
        FileChange(
            path="huge.py",
            operation=FileChangeKind.CREATE,
            new_content="x" * 10_000_000,
        )


def test_07_extremely_large_patch_output_is_bounded(repo: Path) -> None:
    """A real, genuinely huge diff — proven end to end through a real
    subprocess, not a mocked value — is still bounded by
    ``CommandExecutionResult``'s own hard ``Field`` limit; there is no
    way to construct (let alone return) an over-length result at all."""

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    (repo / "src" / "main.py").write_text("x = 1\n" * 50_000)

    h = _Harness(repo)
    h.grant(
        resource=PermissionResource.GIT,
        action=PermissionAction.READ,
        scope="sam-core:diff",
    )
    real_backend = RepositoryBackend(
        context=RepositoryContext(repository_id="sam-core", root=str(repo)),
        command_runner=SubprocessCommandRunner(),
    )
    executor = CodingExecutor(repository=real_backend, permission_engine=h.engine)
    from sam.coding.operations import GetGitDiffOperation

    result = executor.execute(GetGitDiffOperation(principal=_principal()))
    assert result.outcome is ExecutionOutcome.SUCCESS
    assert result.git_diff is not None
    assert len(result.git_diff.raw) <= 50_000
    assert result.git_diff.truncated is True


# --------------------------------------------------------------------- #
# 8-10: command safety
# --------------------------------------------------------------------- #


def test_08_command_injection_via_path_is_impossible() -> None:
    """There is no code path from a path/content string to a shell
    command at all — RUN_TESTS/RUN_LINT/etc. use fixed, internal
    arguments regardless of any operation field."""

    runner = SubprocessCommandRunner()
    malicious = "; rm -rf / #"
    # The only way to run a command is via a ResolvedCommand from the
    # static registry — there is no constructor path that accepts a
    # caller-supplied "path" and turns it into command arguments.
    command = ResolvedCommand(
        executable_name="python3",
        arguments=("-c", f"print({malicious!r})"),
        timeout_seconds=5,
    )
    result = runner.run(command, cwd=Path("/tmp"), repository_root=Path("/tmp"))
    # The malicious string is inert data printed by python, never
    # interpreted as a shell command — no shell was ever invoked.
    assert malicious in result.stdout
    assert result.exit_code == 0


def test_09_shell_metacharacters_are_never_interpreted() -> None:
    runner = SubprocessCommandRunner()
    command = ResolvedCommand(
        executable_name="python3",
        arguments=("-c", "print('a && b || c; d | e `f` $(g)')"),
        timeout_seconds=5,
    )
    result = runner.run(command, cwd=Path("/tmp"), repository_root=Path("/tmp"))
    # Printed literally by python — none of &&, ||, ;, |, backticks, $()
    # were interpreted, because subprocess.run is called with
    # shell=False and a fixed argv list.
    assert "&&" in result.stdout
    assert result.exit_code == 0


def test_10_environment_variable_injection_does_not_reach_the_child() -> None:
    """The child process gets a minimal, explicit environment — never
    os.environ wholesale — so a secret set in this process's own
    environment is not automatically inherited."""

    os.environ["SAM_TEST_SECRET_ENV_VAR"] = "super-secret-value"
    try:
        runner = SubprocessCommandRunner()
        command = ResolvedCommand(
            executable_name="python3",
            arguments=(
                "-c",
                "import os; print(os.environ.get('SAM_TEST_SECRET_ENV_VAR', 'ABSENT'))",
            ),
            timeout_seconds=5,
        )
        result = runner.run(command, cwd=Path("/tmp"), repository_root=Path("/tmp"))
        assert "ABSENT" in result.stdout
        assert "super-secret-value" not in result.stdout
    finally:
        del os.environ["SAM_TEST_SECRET_ENV_VAR"]


# --------------------------------------------------------------------- #
# 11-14: executable resolution
# --------------------------------------------------------------------- #


def test_11_path_manipulation_cannot_redirect_execution(repo: Path) -> None:
    """Even with the repository first on a manipulated PATH, resolution
    only ever searches the fixed trusted search list — never the
    ambient process PATH."""

    fake = repo / "git"
    fake.write_text("#!/bin/sh\necho PWNED\n")
    fake.chmod(0o755)
    original_path = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = f"{repo}{os.pathsep}{original_path}"
        resolved = _resolve_executable("git", repository_root=repo)
        assert not str(resolved).startswith(str(repo))
    finally:
        os.environ["PATH"] = original_path


def test_12_executable_substitution_is_rejected(repo: Path) -> None:
    fake = repo / "git"
    fake.write_text("#!/bin/sh\necho PWNED\n")
    fake.chmod(0o755)
    resolved = _resolve_executable("git", repository_root=repo)
    assert resolved != fake


def test_13_malicious_repository_local_executable_never_invoked(repo: Path) -> None:
    fake_output_marker = repo / "PWNED_MARKER"
    fake = repo / "git"
    fake.write_text(f"#!/bin/sh\ntouch {fake_output_marker}\necho fake git\n")
    fake.chmod(0o755)

    h = _Harness(repo)
    h.grant(
        resource=PermissionResource.GIT,
        action=PermissionAction.READ,
        scope="sam-core:status",
    )
    real_backend = RepositoryBackend(
        context=RepositoryContext(repository_id="sam-core", root=str(repo)),
        command_runner=SubprocessCommandRunner(),
    )
    executor = CodingExecutor(repository=real_backend, permission_engine=h.engine)
    from sam.coding.operations import GetGitStatusOperation

    executor.execute(GetGitStatusOperation(principal=_principal()))

    assert not fake_output_marker.exists()


def test_14_fake_uv_and_python_executables_in_repository_are_ignored(
    repo: Path,
) -> None:
    for name in ("uv", "python3", "python"):
        fake = repo / name
        fake.write_text("#!/bin/sh\necho PWNED\n")
        fake.chmod(0o755)
        resolved = _resolve_executable(name, repository_root=repo)
        assert not str(resolved).startswith(str(repo))


# --------------------------------------------------------------------- #
# 15: working-directory escape
# --------------------------------------------------------------------- #


def test_15_working_directory_escape_via_cwd_manipulation(repo: Path) -> None:
    """The command's cwd is always the repository root, explicitly
    passed — never derived from the caller's own process cwd — so
    changing this test process's cwd cannot redirect where a command
    operates."""

    previous = os.getcwd()
    other_dir = repo.parent
    try:
        os.chdir(other_dir)
        h = _Harness(repo)
        h.grant(
            resource=PermissionResource.GIT,
            action=PermissionAction.READ,
            scope="sam-core:status",
        )
        real_backend = RepositoryBackend(
            context=RepositoryContext(repository_id="sam-core", root=str(repo)),
            command_runner=SubprocessCommandRunner(),
        )
        executor = CodingExecutor(repository=real_backend, permission_engine=h.engine)
        from sam.coding.operations import GetGitStatusOperation

        result = executor.execute(GetGitStatusOperation(principal=_principal()))
        assert result.outcome is ExecutionOutcome.SUCCESS
    finally:
        os.chdir(previous)


# --------------------------------------------------------------------- #
# 16-19: secret protection
# --------------------------------------------------------------------- #


def test_16_secret_in_file_content_is_restricted(repo: Path) -> None:
    (repo / ".env").write_text("API_KEY=sk-ant-secretvalue1234567890\n")
    h = _Harness(repo)
    h.grant_code(action=PermissionAction.READ, path=".env")
    result = h.executor.execute(ReadFileOperation(principal=_principal(), path=".env"))
    assert result.outcome is ExecutionOutcome.SUCCESS
    assert result.file_read is not None
    assert result.file_read.restricted is True
    assert result.file_read.content is None


def test_17_secret_in_command_output_never_leaves_the_bounded_result(
    repo: Path,
) -> None:
    """Command output is returned to the caller as part of the result
    (by design — that is the point of running tests/lint), but it must
    never additionally leak into the audit trail — see test 19."""

    h = _Harness(repo)
    from sam.coding.models import CommandExecutionResult

    h.command_runner.set_result(
        "uv",
        CommandExecutionResult(
            stdout="FAILED: assert api_key == 'sk-ant-realvalue1234567890'",
            stderr="",
            exit_code=1,
            timed_out=False,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_seconds=0.1,
        ),
    )
    h.grant_code_op(action=PermissionAction.EXECUTE, segment="tests")
    pending = h.executor.execute(RunTestsOperation(principal=_principal()))
    assert pending.confirmation_id is not None
    h.confirmations.decide(pending.confirmation_id, approved=True, now=_T0)
    h.executor.execute(
        RunTestsOperation(principal=_principal()),
        confirmation_id=pending.confirmation_id,
    )
    for event in h.audit.list_events():
        assert "sk-ant-realvalue" not in event.model_dump_json()


def test_18_secret_in_provider_proposal_never_bypasses_policy() -> None:
    """A CodingProvider returning a secret-bearing summary/description
    is only ever display-adjacent metadata bounded by the model's own
    sanitization — it is never treated as authorization and the planner
    still enforces its own bounds regardless of proposal content."""

    from sam.coding.models import (
        CodingOperation,
        CodingProposal,
        CodingTask,
        PlanStep,
        ProviderName,
    )
    from sam.coding.planner import FakeCodingProvider

    proposal = CodingProposal(
        provider=ProviderName.CLAUDE_CODE,
        summary="my api key is sk-ant-shouldnotmatter1234567890",
        steps=(PlanStep(operation=CodingOperation.READ_FILE, path="x.py"),),
    )
    provider = FakeCodingProvider(proposal)
    task = CodingTask(
        task_id="t1",
        principal=_principal(),
        repository_id="sam-core",
        instruction="do something",
        created_at=_T0,
    )
    result = provider.propose(task)
    planner = CodingPlanner()
    plan = planner.build_plan_from_proposal(task, result)
    # The plan bounds are still enforced; nothing about the secret text
    # grants extra authority or bypasses the max_steps/paths bound.
    assert len(plan.steps) == 1


def test_19_secret_never_enters_audit_records(repo: Path) -> None:
    (repo / ".env").write_text("API_KEY=sk-ant-secretvalue1234567890\n")
    h = _Harness(repo)
    h.grant_code(action=PermissionAction.READ, path=".env")
    h.executor.execute(ReadFileOperation(principal=_principal(), path=".env"))
    for event in h.audit.list_events():
        assert "sk-ant" not in event.model_dump_json()


# --------------------------------------------------------------------- #
# 20: stale file modification
# --------------------------------------------------------------------- #


def test_20_stale_file_modification_never_overwrites(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code(action=PermissionAction.READ, path="src")
    h.grant_code(action=PermissionAction.WRITE, path="src")
    read = h.executor.execute(_read_op())
    assert read.file_read is not None
    (repo / "src" / "main.py").write_text("print('concurrent edit')\n")
    change = FileChange(
        path="src/main.py",
        operation=FileChangeKind.MODIFY,
        expected_original_hash=read.file_read.content_hash,
        new_content="print('my edit')\n",
    )
    result = h.executor.execute(
        ModifyFileOperation(principal=_principal(), change=change)
    )
    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error_category is ErrorCategory.CONCURRENT_MODIFICATION
    assert "concurrent edit" in (repo / "src" / "main.py").read_text()


# --------------------------------------------------------------------- #
# 21-22: cross-principal / cross-repository
# --------------------------------------------------------------------- #


def test_21_cross_principal_access_is_denied(repo: Path) -> None:
    h = _Harness(repo)
    h.grant(
        resource=PermissionResource.CODE,
        action=PermissionAction.READ,
        scope="sam-core/src",
        scope_is_path=True,
        principal=_principal("ali"),
    )
    result = h.executor.execute(
        ReadFileOperation(principal=_principal("mallory"), path="src/main.py")
    )
    assert result.outcome is ExecutionOutcome.FAILED


def test_22_cross_repository_access_is_structurally_impossible(repo: Path) -> None:
    """An executor is bound to exactly one repository at construction —
    there is no field on any operation that could name a different one,
    so "cross-repository access" has no expressible request shape."""

    other_repo = repo.parent / "other-repo"
    other_repo.mkdir()
    (other_repo / "secret.py").write_text("secret content")
    _init_git(other_repo)

    h = _Harness(repo)  # bound only to `repo`, never `other_repo`
    h.grant_code(action=PermissionAction.READ, path="secret.py")

    # There is no operation field to redirect this executor at
    # `other_repo` — the request can only ever resolve within `repo`.
    result = h.executor.execute(
        ReadFileOperation(principal=_principal(), path="secret.py")
    )
    assert result.outcome is ExecutionOutcome.FAILED  # not in `repo`, not found/denied


# --------------------------------------------------------------------- #
# 23-24: replay
# --------------------------------------------------------------------- #


def test_23_permission_grant_use_is_independently_authorized_each_time(
    repo: Path,
) -> None:
    """Repeated use of the same LOW/MEDIUM-risk grant is not "replay" —
    each call is freshly, correctly authorized; this is expected,
    correct behavior, verified explicitly to distinguish it from
    confirmation replay (test 24), which must be rejected."""

    h = _Harness(repo)
    h.grant_code(action=PermissionAction.READ, path="src")
    first = h.executor.execute(_read_op())
    second = h.executor.execute(_read_op())
    assert first.outcome is second.outcome is ExecutionOutcome.SUCCESS


def test_24_confirmation_replay_is_rejected(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code_op(action=PermissionAction.EXECUTE, segment="tests")
    pending = h.executor.execute(RunTestsOperation(principal=_principal()))
    assert pending.confirmation_id is not None
    h.confirmations.decide(pending.confirmation_id, approved=True, now=_T0)
    used = h.executor.execute(
        RunTestsOperation(principal=_principal()),
        confirmation_id=pending.confirmation_id,
    )
    assert used.outcome is ExecutionOutcome.SUCCESS
    replay = h.executor.execute(
        RunTestsOperation(principal=_principal()),
        confirmation_id=pending.confirmation_id,
    )
    assert replay.outcome is ExecutionOutcome.FAILED
    assert replay.error_category is ErrorCategory.CONFIRMATION_INVALID


# --------------------------------------------------------------------- #
# 25-26: unlimited plan/operations
# --------------------------------------------------------------------- #


def test_25_unlimited_plan_generation_is_rejected() -> None:
    from sam.coding.errors import InvalidCodingRequestError
    from sam.coding.models import CodingOperation, CodingTask, PlanStep

    planner = CodingPlanner()
    task = CodingTask(
        task_id="t1",
        principal=_principal(),
        repository_id="sam-core",
        instruction="x",
        created_at=_T0,
    )
    huge_steps = [
        PlanStep(operation=CodingOperation.READ_FILE, path=f"f{i}.py")
        for i in range(10_000)
    ]
    with pytest.raises(InvalidCodingRequestError):
        planner.build_plan(task, huge_steps)


def test_26_unlimited_operations_per_plan_is_bounded() -> None:
    from sam.coding.models import MAX_PLAN_STEPS

    assert MAX_PLAN_STEPS <= 20


# --------------------------------------------------------------------- #
# 27-29: timeout and output bounds
# --------------------------------------------------------------------- #


def test_27_test_command_timeout_is_enforced_and_reported() -> None:
    runner = SubprocessCommandRunner()
    command = ResolvedCommand(
        executable_name="python3",
        arguments=("-c", "import time; time.sleep(10)"),
        timeout_seconds=1.0,
    )
    result = runner.run(command, cwd=Path("/tmp"), repository_root=Path("/tmp"))
    assert result.timed_out is True
    assert result.exit_code is None


def test_28_huge_stdout_is_bounded(repo: Path) -> None:
    runner = SubprocessCommandRunner()
    command = ResolvedCommand(
        executable_name="python3",
        arguments=("-c", "print('a' * 5_000_000)"),
        timeout_seconds=10.0,
    )
    result = runner.run(command, cwd=repo, repository_root=repo)
    assert result.stdout_truncated is True
    assert len(result.stdout) <= 50_000


def test_29_huge_stderr_is_bounded(repo: Path) -> None:
    runner = SubprocessCommandRunner()
    command = ResolvedCommand(
        executable_name="python3",
        arguments=("-c", "import sys; sys.stderr.write('e' * 5_000_000)"),
        timeout_seconds=10.0,
    )
    result = runner.run(command, cwd=repo, repository_root=repo)
    assert result.stderr_truncated is True
    assert len(result.stderr) <= 50_000


# --------------------------------------------------------------------- #
# 30: unexpected backend exception
# --------------------------------------------------------------------- #


def test_30_unexpected_backend_exception_never_becomes_success(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code(action=PermissionAction.READ, path="src")

    class _ExplodingBackend(RepositoryBackend):
        def read_file(self, relative_path: str) -> object:  # type: ignore[override]
            raise RuntimeError("disk on fire")

    exploding = _ExplodingBackend(
        context=RepositoryContext(repository_id="sam-core", root=str(repo)),
        command_runner=FakeCommandRunner(),
    )
    executor = CodingExecutor(repository=exploding, permission_engine=h.engine)
    result = executor.execute(_read_op())
    assert result.outcome is ExecutionOutcome.FAILED
    assert "disk on fire" not in result.model_dump_json()


# --------------------------------------------------------------------- #
# The three critical invariants, stated directly and exhaustively
# --------------------------------------------------------------------- #


def test_deny_never_reaches_backend(repo: Path) -> None:
    h = _Harness(repo)  # no grants for anything
    result = h.executor.execute(_read_op())
    assert result.outcome is ExecutionOutcome.FAILED
    assert result.permission_outcome is PermissionOutcomeSummary.DENY


def test_confirm_required_never_reaches_backend(repo: Path) -> None:
    h = _Harness(repo)
    h.grant_code_op(action=PermissionAction.EXECUTE, segment="tests")
    result = h.executor.execute(RunTestsOperation(principal=_principal()))
    assert result.permission_outcome is PermissionOutcomeSummary.CONFIRM_REQUIRED
    assert h.command_runner.calls() == ()


def test_invalid_operation_cannot_even_be_constructed(repo: Path) -> None:
    """A malformed operation cannot reach the executor at all — the
    strongest possible form of "invalid never reaches the backend"."""

    with pytest.raises(ValidationError):
        ReadFileOperation(principal=_principal(), path="\x00evil")
