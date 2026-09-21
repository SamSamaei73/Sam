"""Claude via the owner's SUBSCRIPTION, through the local Claude Code CLI.

Verified against the official docs on 2026-09-20 (docs/model-routing.md):

* ``claude -p`` (non-interactive) is the supported programmatic entry point and
  reads the prompt from stdin.
* ``--bare`` never reads the subscription login, so it cannot be used.
  ``--safe-mode`` disables CLAUDE.md, skills, plugins, hooks, MCP servers,
  custom commands and agents but keeps authentication, so it is used instead.
* ``--tools ""`` disables all built-in tools; ``--disallowedTools "*"`` removes
  every tool as a second layer.
* Authentication precedence puts cloud-provider variables, ``ANTHROPIC_AUTH_TOKEN``,
  ``ANTHROPIC_API_KEY`` and ``apiKeyHelper`` ABOVE the subscription login, and in
  ``-p`` mode an ``ANTHROPIC_API_KEY`` is always used when present. Any of them
  would silently switch this provider to paid API billing.

Phase 13 review remediation adds three more gates, all FAIL CLOSED:

* ``--restricted`` (Claude Code >= 2.1.248, meant for harnesses on shared
  machines) plus ``--permission-prompts none`` (>= 2.1.259) are MANDATORY. Sam
  verifies the installed CLI supports them and verifies the argv it is about to
  run; a missing flag means the provider is unavailable, never weaker isolation.
* Managed policy (MDM profile, managed-settings file/directory) can still run
  hooks or helpers, cannot be disabled from ``--settings`` and is not observable,
  so if ANY managed-policy source exists the provider is unavailable.
* Subscription auth must be PROVEN by the official CLI itself (``claude auth
  status``, same sanitized environment): a claude.ai login on a Pro/Max plan and
  nothing else. A request that merely succeeded proves nothing. Sam never reads
  credential files, the Keychain or tokens; it keeps only four safe fields.

So, defence in depth:

1. the child gets an ALLOWLISTED environment, never a copy of ours, so no
   Anthropic/cloud/gateway/proxy variable can reach it;
2. only a trusted ``claude`` executable is run, by argv (no shell), with an empty
   private working directory, prompt on stdin, all tools removed, one turn, no
   session persistence, and unprompted permissions denied;
3. the run's own ``system/init`` message is ATTESTED: the credential source must
   be the subscription login, and no tool or MCP server may be present. If it is
   not, the output is discarded and never returned.

Sam never reads Claude credential files, the Keychain, cookies, or any token; it
never calls a private claude.ai API and never asks for credentials. Provider
output is untrusted text.

Anthropic's terms bar third-party products from OFFERING claude.ai login. This
adapter is for the owner's own local tool using the owner's own login only; see
docs/model-routing.md.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sam.agent.models import MessageRole
from sam.models.errors import ProviderFailure
from sam.models.models import (
    Availability,
    DeploymentMode,
    FailureCategory,
    ModelRequest,
    ProviderId,
    ProviderOutput,
    UsageMetadata,
)
from sam.models.registry import CLAUDE_SUBSCRIPTION_MODEL

MAX_STDIN_BYTES = 400_000
MAX_STDOUT_BYTES = 1_048_576
MAX_STDERR_BYTES = 32_768
MAX_EVENTS = 5_000
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"

# Anything here can move Claude Code off the subscription onto paid API billing,
# or redirect it. They are STRIPPED explicitly on top of the env allowlist, and
# a test proves none survives even when set in the parent environment.
STRIPPED_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CONFIG_DIR",
    }
)
_ALLOWED_ENV = ("HOME", "USER", "LOGNAME", "LANG")

# What ``apiKeySource`` may say for a subscription login. Anything else (a key
# from user/project settings, org, or a temporary key) means API billing.
SUBSCRIPTION_KEY_SOURCES = frozenset({"oauth", "none"})

# Minimum Claude Code that supports --restricted (2.1.248) and
# --permission-prompts (2.1.259). Older or unknown versions are unavailable.
MIN_CLAUDE_VERSION = (2, 1, 259)
# ``--max-turns`` is a print-mode-only flag that ``--help`` does not list (2.1.278),
# so it is enforced in argv by ``verify_isolation`` instead: a CLI that rejects it
# makes the run fail, never run weaker.
REQUIRED_HELP_FLAGS = (
    "--restricted",
    "--permission-prompts",
    "--safe-mode",
    "--tools",
    "--disallowedTools",
    "--strict-mcp-config",
    "--no-chrome",
    "--no-session-persistence",
    "--permission-mode",
    "--disable-slash-commands",
    "--output-format",
    "--system-prompt",
)
# Managed policy sources (docs: Deploy managed settings). Existence only: Sam
# never reads them. Any of them present => claude_subscription is unavailable.
MANAGED_POLICY_PATHS = (
    Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    Path("/Library/Application Support/ClaudeCode/managed-settings.d"),
    Path("/Library/Application Support/ClaudeCode/managed-mcp.json"),
    Path("/Library/Managed Preferences/com.anthropic.claudecode.plist"),
    Path("/etc/claude-code/managed-settings.json"),
)
# ``claude auth status`` fields Sam understands. Anything else is ambiguous.
KNOWN_STATUS_KEYS = frozenset(
    {
        "loggedIn",
        "authMethod",
        "apiProvider",
        "analyticsDisabled",
        "projectsDirectory",
        "configDirectory",
        "email",
        "orgId",
        "orgName",
        "subscriptionType",
    }
)
SUBSCRIPTION_PLANS = frozenset({"pro", "max"})
PROBE_TIMEOUT = 20.0
PROBE_MAX_STDOUT = 262_144
READY_TTL = 120.0  # a proven subscription login is re-verified after this
NOT_READY_TTL = 30.0
CLI_TTL = 3600.0

SYSTEM_PROMPT = (
    "You are the language model behind a personal assistant called Sam. You have "
    "no tools, files, shell or internet access. Reply with the assistant's next "
    "message as plain text only."
)

_USAGE_LIMIT = re.compile(r"usage limit|limit reached|out of usage|reached your", re.I)
_AUTH = re.compile(r"not logged in|/login|login expired|authenticat|unauthori", re.I)

_ROLE_LABEL = {
    MessageRole.SYSTEM: "[Sam instructions]",
    MessageRole.USER: "[User]",
    MessageRole.ASSISTANT: "[Assistant]",
    MessageRole.TOOL: "[Tool]",
}


def validate_executable(candidate: str | Path) -> Path | None:
    """A trusted Claude executable or ``None``. Never accepts an arbitrary path:
    it must be absolute, be named exactly ``claude``, resolve to a regular
    executable file owned by this user or root, and not be world-writable."""

    try:
        path = Path(candidate)
        if not path.is_absolute() or path.name != "claude" or ".." in path.parts:
            return None
        real = Path(os.path.realpath(path))
        info = real.stat()
        if not stat.S_ISREG(info.st_mode) or not os.access(real, os.X_OK):
            return None
        if info.st_mode & 0o002 or info.st_uid not in (os.getuid(), 0):
            return None
        # A symlink merely NAMED claude must not point at some other program:
        # the resolved location has to be inside a Claude install directory.
        if not any(part.lower().startswith("claude") for part in real.parts[:-1]):
            return None
        return path
    except OSError:
        return None


def trusted_candidates() -> tuple[Path, ...]:
    return (
        Path.home() / ".local/bin/claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    )


def resolve_executable(configured: str | None = None) -> Path | None:
    """The owner's configured path (validated) or the first trusted location."""

    if configured:
        return validate_executable(configured)
    for candidate in trusted_candidates():
        found = validate_executable(candidate)
        if found is not None:
            return found
    return None


def build_child_env(parent: Mapping[str, str], *, tmpdir: str) -> dict[str, str]:
    """A fresh allowlisted environment. Nothing is inherited implicitly."""

    env = {name: parent[name] for name in _ALLOWED_ENV if name in parent}
    env["PATH"] = SAFE_PATH
    env["TMPDIR"] = tmpdir
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    if not env.get("HOME", "").startswith("/"):
        raise ProviderFailure(FailureCategory.UNAVAILABLE, "no_home")
    if STRIPPED_ENV & set(env):  # defence in depth: can never trigger
        raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "env_not_clean")
    return env


def build_argv(executable: Path) -> list[str]:
    return [
        str(executable),
        "-p",
        "--restricted",
        "--safe-mode",
        "--tools",
        "",
        "--disallowedTools",
        "*",
        "--strict-mcp-config",
        "--permission-prompts",
        "none",
        "--permission-mode",
        "dontAsk",
        "--disable-slash-commands",
        "--max-turns",
        "1",
        "--no-session-persistence",
        "--no-chrome",
        "--output-format",
        "stream-json",
        "--verbose",
        "--system-prompt",
        SYSTEM_PROMPT,
    ]


_REQUIRED_SWITCHES = (
    "-p",
    "--restricted",
    "--safe-mode",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--no-session-persistence",
    "--no-chrome",
)
_REQUIRED_PAIRS = (
    ("--tools", ""),
    ("--disallowedTools", "*"),
    ("--permission-prompts", "none"),
    ("--permission-mode", "dontAsk"),
    ("--max-turns", "1"),
)
_FORBIDDEN_FLAGS = frozenset(
    {
        "--dangerously-skip-permissions",
        "--allowedTools",
        "--allowed-tools",
        "--add-dir",
        "--mcp-config",
        "--bare",
        "--settings",
        "--resume",
        "--continue",
        "--chrome",
    }
)


def verify_isolation(argv: Sequence[str]) -> None:
    """Refuse to run unless EVERY mandatory isolation flag is present. Weaker
    isolation is never an acceptable degradation."""

    missing = [flag for flag in _REQUIRED_SWITCHES if flag not in argv]
    for flag, value in _REQUIRED_PAIRS:
        if flag not in argv:
            missing.append(flag)
        else:
            index = argv.index(flag)
            if index + 1 >= len(argv) or argv[index + 1] != value:
                missing.append(flag)
    if missing or _FORBIDDEN_FLAGS & set(argv):
        raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "isolation_flags_missing")


def managed_policy_present(paths: Sequence[Path] | None = None) -> bool:
    """True if any managed-policy source exists (existence only, never read).
    Sam cannot disable or observe managed hooks, so it fails closed."""

    for path in paths if paths is not None else _policy_paths():
        try:
            if path.exists():
                return True
        except OSError:
            return True  # cannot tell: fail closed
    return False


def _policy_paths() -> tuple[Path, ...]:
    user = os.environ.get("USER", "")
    extra = (
        (
            Path("/Library/Managed Preferences")
            / user
            / "com.anthropic.claudecode.plist",
        )
        if user
        else ()
    )
    return (*MANAGED_POLICY_PATHS, *extra)


def render_prompt(request: ModelRequest) -> bytes:
    """The whole conversation as stdin text, so nothing private is in argv."""

    lines = [
        "Continue this conversation. Reply with the assistant's next message only.",
        "",
    ]
    for message in request.messages:
        lines.append(_ROLE_LABEL[message.role])
        lines.append(message.content)
        lines.append("")
    data = "\n".join(lines).encode("utf-8")
    if len(data) > MAX_STDIN_BYTES:
        raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "request_too_large")
    return data


Runner = Callable[..., tuple[int, bytes, bytes]]


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)  # start_new_session: pid == pgid
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


def run_bounded(
    argv: list[str],
    data: bytes,
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout: float,
    max_stdout: int = MAX_STDOUT_BYTES,
    max_stderr: int = MAX_STDERR_BYTES,
) -> tuple[int, bytes, bytes]:
    """Run once: argv list, no shell, bounded stdin/stdout/stderr, finite
    timeout, and the whole process group is killed on any failure."""

    try:
        process = subprocess.Popen(  # noqa: S603  (fixed trusted argv, shell=False)
            argv,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        raise ProviderFailure(FailureCategory.UNAVAILABLE, "launch_failed") from None
    out, err = bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    try:
        assert process.stdin and process.stdout and process.stderr
        stdout_pipe, stderr_pipe = process.stdout, process.stderr
        selector = selectors.DefaultSelector()
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, "out")
        selector.register(process.stderr, selectors.EVENT_READ, "err")
        pending = memoryview(data)
        if pending:
            selector.register(process.stdin, selectors.EVENT_WRITE, "in")
        else:
            process.stdin.close()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderFailure(FailureCategory.TIMEOUT, "timeout")
            for key, _ in selector.select(remaining):
                if key.data == "in":
                    try:
                        sent = os.write(process.stdin.fileno(), pending[:65_536])
                        pending = pending[sent:]
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        pending = pending[len(pending) :]
                    if not pending:
                        selector.unregister(process.stdin)
                        process.stdin.close()
                else:
                    fd = stdout_pipe if key.data == "out" else stderr_pipe
                    try:
                        chunk = os.read(fd.fileno(), 65_536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(fd)
                        continue
                    sink, limit = (
                        (out, max_stdout) if key.data == "out" else (err, max_stderr)
                    )
                    sink.extend(chunk)
                    if len(sink) > limit:
                        raise ProviderFailure(
                            FailureCategory.INVALID_RESPONSE, "too_large"
                        )
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _kill_group(process)
        process.wait()
        raise ProviderFailure(FailureCategory.TIMEOUT, "timeout") from None
    except BaseException:
        _kill_group(process)
        process.wait()
        raise
    finally:
        _kill_group(process)  # nothing may outlive the call
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()
    return code, bytes(out), bytes(err)


def _events(stdout: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in stdout.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
        if len(events) > MAX_EVENTS:
            raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "too_many_events")
    return events


def _attest(events: list[dict[str, Any]]) -> None:
    """The run must PROVE it used the subscription login with no tools."""

    init = next(
        (e for e in events if e.get("type") == "system" and e.get("subtype") == "init"),
        None,
    )
    if init is None:
        raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "attestation_missing")
    if init.get("apiKeySource") not in SUBSCRIPTION_KEY_SOURCES:
        raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "not_subscription_auth")
    tools = init.get("tools")
    if not isinstance(tools, list) or tools:
        raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "tools_not_disabled")
    servers = init.get("mcp_servers", [])
    if not isinstance(servers, list) or servers:
        raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "mcp_not_disabled")


def _classify_failure(
    events: list[dict[str, Any]], result_text: str
) -> ProviderFailure:
    for event in events:
        if event.get("type") == "assistant":
            message = event.get("message")
            if isinstance(message, dict) and message.get("stop_reason") == "refusal":
                return ProviderFailure(FailureCategory.REFUSAL, "provider_refusal")
    for event in events:
        if (
            event.get("stop_reason") == "refusal"
            or event.get("terminal_reason") == "refusal"
        ):
            return ProviderFailure(FailureCategory.REFUSAL, "provider_refusal")
    categories = {
        e.get("error")
        for e in events
        if e.get("type") == "system" and e.get("subtype") == "api_retry"
    }
    if _USAGE_LIMIT.search(result_text):
        return ProviderFailure(FailureCategory.RATE_LIMIT, "usage_limit")
    if "rate_limit" in categories:
        return ProviderFailure(FailureCategory.RATE_LIMIT, "rate_limited")
    if categories & {
        "authentication_failed",
        "oauth_org_not_allowed",
        "account_on_hold",
    }:
        return ProviderFailure(FailureCategory.AUTH_FAILURE, "not_authenticated")
    if "billing_error" in categories:
        return ProviderFailure(FailureCategory.AUTH_FAILURE, "billing_error")
    if _AUTH.search(result_text):
        return ProviderFailure(FailureCategory.AUTH_FAILURE, "not_authenticated")
    return ProviderFailure(FailureCategory.UNAVAILABLE, "claude_error")


def parse_output(stdout: bytes, returncode: int) -> ProviderOutput:
    events = _events(stdout)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    result_text = result.get("result") if isinstance(result, dict) else None
    text = result_text if isinstance(result_text, str) else ""
    failed = returncode != 0 or result is None or bool(result.get("is_error"))
    if failed:
        raise _classify_failure(events, text[:2000])
    _attest(events)  # BEFORE any text is trusted
    if not text.strip():
        raise ProviderFailure(FailureCategory.INVALID_RESPONSE, "no_text")
    usage = None
    raw = result.get("usage") if result else None
    if isinstance(raw, dict):

        def count(key: str) -> int | None:
            v = raw.get(key)
            return v if isinstance(v, int) and not isinstance(v, bool) else None

        try:
            usage = UsageMetadata(
                input_tokens=count("input_tokens"), output_tokens=count("output_tokens")
            )
        except ValueError:
            usage = None
    return ProviderOutput(
        text=text.strip(), model_id=CLAUDE_SUBSCRIPTION_MODEL, usage=usage
    )


@dataclass(frozen=True)
class AuthVerdict:
    """The only thing Sam keeps from ``claude auth status``: whether the ACTIVE
    authentication is the owner's claude.ai subscription login. Identity fields
    (email, org, directories) are dropped at parse time and never stored."""

    ok: bool
    code: str
    plan: str | None = None


def classify_auth(document: object) -> AuthVerdict:
    """Positive identification only. Observed real values (CLI 2.1.278):
    subscription login -> ``claude.ai``/``firstParty``; API key -> ``api_key``;
    auth token or OAuth-token env -> ``oauth_token``; Bedrock/Vertex ->
    ``third_party``. Everything not exactly a Pro/Max claude.ai login (Console,
    apiKeyHelper, profile/federation, gateway, unknown) is rejected."""

    if not isinstance(document, dict):
        return AuthVerdict(False, "ambiguous_auth")
    method = document.get("authMethod")
    provider = document.get("apiProvider")
    if method == "api_key":
        return AuthVerdict(False, "api_key_auth")
    if method == "oauth_token":
        return AuthVerdict(False, "token_auth")
    if method == "third_party" or provider not in (None, "firstParty"):
        return AuthVerdict(False, "third_party_auth")
    if document.get("loggedIn") is not True or method in (None, "none"):
        return AuthVerdict(False, "not_logged_in")
    if method != "claude.ai" or provider != "firstParty":
        return AuthVerdict(False, "ambiguous_auth")
    if set(document) - KNOWN_STATUS_KEYS:
        return AuthVerdict(False, "ambiguous_auth")
    plan = document.get("subscriptionType")
    if plan not in SUBSCRIPTION_PLANS:
        return AuthVerdict(False, "unsupported_plan")
    return AuthVerdict(True, "subscription_login", str(plan))


@dataclass(frozen=True)
class Readiness:
    availability: Availability
    code: str


_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


class ClaudeSubscriptionProvider:
    provider_id = ProviderId.CLAUDE_SUBSCRIPTION

    def __init__(
        self,
        *,
        executable: str | None = None,
        environ: Mapping[str, str] | None = None,
        runner: Runner = run_bounded,
        temp_root: str | None = None,
        deployment_mode: DeploymentMode = DeploymentMode.OWNER_LOCAL,
        managed_policy_paths: Sequence[Path] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._configured = executable
        self._environ = environ
        self._runner = runner
        self._temp_root = temp_root
        self._deployment = deployment_mode
        self._policy_paths = managed_policy_paths
        self._clock = clock
        self._cli_ok_until = 0.0
        self._ready: tuple[float, Readiness] | None = None

    @property
    def disabled_detail(self) -> str | None:
        if self._deployment is not DeploymentMode.OWNER_LOCAL:
            return "deployment_not_owner_local"
        return None

    def detail(self) -> str | None:
        readiness = self._readiness()
        return (
            None if readiness.availability is Availability.AVAILABLE else readiness.code
        )

    def availability(self) -> Availability:
        return self._readiness().availability

    def __repr__(self) -> str:
        return "ClaudeSubscriptionProvider()"

    # ---------------------------------------------------------- readiness

    def _run_probe(self, argv: list[str]) -> bytes:
        parent = self._environ if self._environ is not None else os.environ
        workdir = tempfile.mkdtemp(prefix="sam-claude-probe-", dir=self._temp_root)
        try:
            os.chmod(workdir, 0o700)
            code, stdout, _err = self._runner(
                argv,
                b"",
                cwd=workdir,
                env=build_child_env(parent, tmpdir=workdir),
                timeout=PROBE_TIMEOUT,
                max_stdout=PROBE_MAX_STDOUT,
                max_stderr=MAX_STDERR_BYTES,
            )
            if code not in (0, 1):
                raise ProviderFailure(FailureCategory.UNAVAILABLE, "probe_failed")
            return stdout
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _cli_supported(self, executable: Path) -> bool:
        """The installed CLI must support every mandatory isolation flag."""

        if self._clock() < self._cli_ok_until:
            return True
        try:
            version = _VERSION.match(
                self._run_probe([str(executable), "--version"]).decode(
                    "utf-8", "replace"
                )
            )
            if version is None:
                return False
            if tuple(int(x) for x in version.groups()) < MIN_CLAUDE_VERSION:
                return False
            help_text = self._run_probe([str(executable), "--help"]).decode(
                "utf-8", "replace"
            )
        except (ProviderFailure, ValueError):
            return False
        if not all(flag in help_text for flag in REQUIRED_HELP_FLAGS):
            return False
        self._cli_ok_until = self._clock() + CLI_TTL
        return True

    def _auth(self, executable: Path) -> AuthVerdict:
        try:
            raw = self._run_probe([str(executable), "auth", "status", "--json"])
            document = json.loads(raw.decode("utf-8", "replace"))
        except (ProviderFailure, ValueError):
            return AuthVerdict(False, "auth_status_unreadable")
        return classify_auth(document)

    def _readiness(self, *, refresh: bool = False) -> Readiness:
        now = self._clock()
        if self._deployment is not DeploymentMode.OWNER_LOCAL:
            return Readiness(Availability.DISABLED, "deployment_not_owner_local")
        executable = resolve_executable(self._configured)
        if executable is None:
            return Readiness(Availability.NOT_CONFIGURED, "not_installed")
        if managed_policy_present(self._policy_paths):
            # Cannot disable or observe managed hooks/helpers: fail closed.
            return Readiness(Availability.UNAVAILABLE, "managed_policy_present")
        if not refresh and self._ready is not None and now < self._ready[0]:
            return self._ready[1]
        if not self._cli_supported(executable):
            readiness = Readiness(Availability.UNAVAILABLE, "cli_unsupported")
            ttl = NOT_READY_TTL
        else:
            verdict = self._auth(executable)
            if verdict.ok:
                readiness, ttl = (
                    Readiness(Availability.AVAILABLE, verdict.code),
                    READY_TTL,
                )
            elif verdict.code == "not_logged_in":
                readiness = Readiness(Availability.UNAUTHORIZED, verdict.code)
                ttl = NOT_READY_TTL
            else:
                readiness = Readiness(Availability.UNAVAILABLE, verdict.code)
                ttl = NOT_READY_TTL
        self._ready = (now + ttl, readiness)
        return readiness

    # ---------------------------------------------------------- inference

    def complete(
        self, request: ModelRequest, *, model_id: str, timeout_seconds: float
    ) -> ProviderOutput:
        if model_id != CLAUDE_SUBSCRIPTION_MODEL:
            raise ProviderFailure(FailureCategory.POLICY_BLOCKED, "model_not_allowed")
        if self._deployment is not DeploymentMode.OWNER_LOCAL:
            raise ProviderFailure(
                FailureCategory.POLICY_BLOCKED, "deployment_not_owner_local"
            )
        data = render_prompt(request)
        readiness = self._readiness()
        if readiness.availability is not Availability.AVAILABLE:
            category = {
                "not_logged_in": FailureCategory.AUTH_FAILURE,
            }.get(readiness.code, FailureCategory.UNAVAILABLE)
            raise ProviderFailure(category, readiness.code)
        executable = resolve_executable(self._configured)
        if executable is None:
            raise ProviderFailure(FailureCategory.UNAVAILABLE, "not_configured")
        argv = build_argv(executable)
        verify_isolation(argv)  # mandatory flags, or no process at all
        parent = self._environ if self._environ is not None else os.environ
        # A Sam-owned, empty, private directory: Claude Code discovers no repo
        # files, CLAUDE.md, MCP config or user files unless Sam sends them.
        workdir = tempfile.mkdtemp(prefix="sam-claude-", dir=self._temp_root)
        try:
            os.chmod(workdir, 0o700)
            env = build_child_env(parent, tmpdir=workdir)
            code, stdout, _stderr = self._runner(
                argv, data, cwd=workdir, env=env, timeout=timeout_seconds
            )
            try:
                return parse_output(stdout, code)
            except ProviderFailure as failure:
                if failure.category is FailureCategory.AUTH_FAILURE:
                    self._ready = None  # re-verify the login before the next use
                raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


__all__ = [
    "MANAGED_POLICY_PATHS",
    "MIN_CLAUDE_VERSION",
    "REQUIRED_HELP_FLAGS",
    "STRIPPED_ENV",
    "SUBSCRIPTION_KEY_SOURCES",
    "AuthVerdict",
    "ClaudeSubscriptionProvider",
    "build_argv",
    "classify_auth",
    "managed_policy_present",
    "verify_isolation",
    "build_child_env",
    "parse_output",
    "render_prompt",
    "resolve_executable",
    "run_bounded",
    "validate_executable",
]
