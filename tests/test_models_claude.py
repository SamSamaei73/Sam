"""The Claude SUBSCRIPTION adapter, driven against a fake ``claude`` script.

A real subprocess is started (so argv, stdin, environment, working directory,
timeouts, output bounds and cleanup are exercised for real), but the script is
a harmless stand-in. Nothing here contacts Anthropic, reads a credential, or
needs a login.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from sam.core.config import Settings
from sam.models.errors import ProviderFailure
from sam.models.models import (
    Availability,
    DeploymentMode,
    FailureCategory,
    ModelRequest,
    PrivacyClass,
    ProviderId,
    ProviderOutput,
    ReasonCode,
)
from sam.models.providers import claude_subscription as cs
from sam.models.providers.claude_subscription import (
    STRIPPED_ENV,
    ClaudeSubscriptionProvider,
    build_argv,
    build_child_env,
    parse_output,
    validate_executable,
)
from sam.models.registry import CLAUDE_SUBSCRIPTION_MODEL, build_registry
from tests.models_support import (
    FAKE_API_KEY,
    FAKE_GEMINI_KEY,
    PRIVATE_TEXT,
    Holder,
    attested,
    audit_json,
    make_rig,
    request,
)

FAKE_SCRIPT = """#!{python}
import json, os, pathlib, subprocess, sys, time

HELP = {help_text!r}
SUBSCRIPTION = {{"loggedIn": True, "authMethod": "claude.ai",
                 "apiProvider": "firstParty", "email": "owner@example.invalid",
                 "orgId": "org", "orgName": "org", "subscriptionType": "max"}}

here = pathlib.Path(os.path.realpath(__file__)).parent
args = sys.argv[1:]
if args[:1] == ["--version"]:
    override = here / "version.txt"
    print(override.read_text() if override.exists() else "2.1.278 (Claude Code)")
    sys.exit(0)
if args[:1] == ["--help"]:
    override = here / "help.txt"
    print(override.read_text() if override.exists() else HELP)
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    with (here / "probes.log").open("a") as log:
        log.write(json.dumps({{"argv": args, "env": dict(os.environ)}}) + "\\n")
    override = here / "auth.json"
    print(override.read_text() if override.exists() else json.dumps(SUBSCRIPTION))
    exit_file = here / "auth.exit"
    sys.exit(int(exit_file.read_text()) if exit_file.exists() else 0)
data = sys.stdin.read()
record = {{
    "argv": sys.argv[1:],
    "env": dict(os.environ),
    "cwd": os.getcwd(),
    "listing": sorted(os.listdir(".")),
    "stdin": data,
}}
(here / "record.json").write_text(json.dumps(record))


def emit(obj):
    print(json.dumps(obj), flush=True)


INIT = {{"type": "system", "subtype": "init", "apiKeySource": "oauth",
         "tools": [], "mcp_servers": [], "model": "fake"}}
OK = {{"type": "result", "subtype": "success", "is_error": False,
       "result": "Hello from the fake claude",
       "usage": {{"input_tokens": 5, "output_tokens": 2}}}}
mode = next((m for m in ("SLEEP", "BIG", "USAGE", "AUTH", "REFUSAL", "TOOLS", "KEYSRC",
                         "MCP", "NOINIT", "JUNK", "EXIT1", "EMPTY")
            if "MODE_" + m in data), "OK")
if mode == "SLEEP":
    child = subprocess.Popen(["sleep", "60"])
    pids = {{"self": os.getpid(), "child": child.pid}}
    (here / "pids.json").write_text(json.dumps(pids))
    time.sleep(60)
elif mode == "BIG":
    emit(INIT)
    sys.stdout.write("x" * 3_000_000)
    sys.stdout.flush()
elif mode == "USAGE":
    emit(INIT)
    emit({{"type": "result", "is_error": True,
          "result": "Claude usage limit reached. It resets later."}})
    sys.exit(1)
elif mode == "AUTH":
    emit({{"type": "result", "is_error": True,
          "result": "Not logged in - Please run /login"}})
    sys.exit(1)
elif mode == "REFUSAL":
    emit(INIT)
    emit({{"type": "assistant",
          "message": {{"stop_reason": "refusal", "content": []}}}})
    emit({{"type": "result", "is_error": True, "result": "declined"}})
    sys.exit(1)
elif mode == "TOOLS":
    emit({{**INIT, "tools": ["Bash", "Read"]}})
    emit(OK)
elif mode == "KEYSRC":
    emit({{**INIT, "apiKeySource": "user"}})
    emit(OK)
elif mode == "MCP":
    emit({{**INIT, "mcp_servers": [{{"name": "x", "status": "connected"}}]}})
    emit(OK)
elif mode == "NOINIT":
    emit(OK)
elif mode == "JUNK":
    print("this is not json at all")
elif mode == "EXIT1":
    emit(INIT)
    emit(OK)
    sys.exit(3)
elif mode == "EMPTY":
    emit(INIT)
    emit({{**OK, "result": "   "}})
else:
    emit(INIT)
    block = {{"type": "text", "text": "hi"}}
    emit({{"type": "assistant", "message": {{"content": [block]}}}})
    emit(OK)
"""

HELP_TEXT = "\n".join(cs.REQUIRED_HELP_FLAGS)

PARENT_ENV_SECRETS = {
    **{name: "leak-" + name for name in STRIPPED_ENV},
    "ANTHROPIC_API_KEY": FAKE_API_KEY,
    "OPENAI_API_KEY": "sk-openai-not-real",
    "GEMINI_API_KEY": FAKE_GEMINI_KEY,
    "XAI_API_KEY": "xai-not-real",
    "AWS_ACCESS_KEY_ID": "AKIAFAKE",
    "GOOGLE_APPLICATION_CREDENTIALS": "/nope",
    "HTTPS_PROXY": "http://evil.invalid:8080",
    "HTTP_PROXY": "http://evil.invalid:8080",
    "SSL_CERT_FILE": "/nope",
}


class Fake:
    def __init__(self, tmp_path: Path, home: Path) -> None:
        self.dir = tmp_path / "claude-install" / "bin"  # "claude*" path component
        self.dir.mkdir(parents=True)
        self.exe = self.dir / "claude"
        self.exe.write_text(
            FAKE_SCRIPT.format(python=sys.executable, help_text=HELP_TEXT)
        )
        self.exe.chmod(0o755)
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.env = {
            "HOME": str(home),
            "USER": "tester",
            "LANG": "en_US.UTF-8",
            **PARENT_ENV_SECRETS,
        }

    def provider(self, **extra: Any) -> ClaudeSubscriptionProvider:
        options: dict[str, Any] = {
            "managed_policy_paths": (),  # this machine's real policy is not consulted
            **extra,
        }
        return ClaudeSubscriptionProvider(
            executable=str(self.exe),
            environ=self.env,
            temp_root=str(self.work),
            **options,
        )

    def set_auth(self, document: object) -> None:
        (self.dir / "auth.json").write_text(json.dumps(document))

    def inference_ran(self) -> bool:
        return (self.dir / "record.json").exists()

    def record(self) -> dict[str, Any]:
        return json.loads((self.dir / "record.json").read_text())  # type: ignore[no-any-return]

    def run(self, text: str = "hello", timeout: float = 20.0) -> Any:
        return self.provider().complete(
            request(text), model_id=CLAUDE_SUBSCRIPTION_MODEL, timeout_seconds=timeout
        )

    def expect(
        self, marker: str, category: FailureCategory, code: str | None = None
    ) -> ProviderFailure:
        with pytest.raises(ProviderFailure) as caught:
            self.run("please MODE_" + marker)
        assert caught.value.category is category, caught.value.code
        if code:
            assert caught.value.code == code
        return caught.value


@pytest.fixture
def fake(tmp_path: Path) -> Fake:
    home = tmp_path / "home"
    home.mkdir()
    return Fake(tmp_path, home)


# ----------------------------------------------- happy path and argv/stdin


def test_success_returns_text_with_the_subscription_model_label(fake: Fake) -> None:
    out = fake.run("hello Sam")
    assert (
        out.text == "Hello from the fake claude"
        and out.model_id == CLAUDE_SUBSCRIPTION_MODEL
    )
    assert out.usage is not None and (
        out.usage.input_tokens,
        out.usage.output_tokens,
    ) == (5, 2)


def test_argv_disables_tools_mcp_commands_and_never_skips_permissions(
    fake: Fake,
) -> None:
    fake.run(PRIVATE_TEXT)
    argv = fake.record()["argv"]
    assert argv[0] == "-p" and "--safe-mode" in argv and "--restricted" in argv
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--tools") + 1] == ""  # all built-in tools disabled
    assert argv[argv.index("--disallowedTools") + 1] == "*"  # every tool removed
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--max-turns") + 1] == "1"
    for flag in (
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--no-chrome",
        "--verbose",
    ):
        assert flag in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    for forbidden in (
        "--dangerously-skip-permissions",
        "--allowedTools",
        "--allowed-tools",
        "--add-dir",
        "--mcp-config",
        "--bare",
        "--agents",
        "--plugin-dir",
        "--settings",
        "--resume",
        "--continue",
        "--chrome",
    ):
        assert forbidden not in argv, forbidden
    assert not any(
        part for part in argv if "Bash" in part or "Read" in part or "Edit" in part
    )


def test_the_prompt_travels_on_stdin_not_in_argv(fake: Fake) -> None:
    fake.run(PRIVATE_TEXT)
    record = fake.record()
    assert PRIVATE_TEXT in record["stdin"] and record["stdin"].startswith(
        "Continue this conversation"
    )
    assert PRIVATE_TEXT not in " ".join(record["argv"])
    assert "[User]" in record["stdin"]


def test_subprocess_is_started_with_a_list_and_shell_false(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    real = subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen["args"], seen["kwargs"] = args, kwargs
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    fake.run()
    assert isinstance(seen["args"][0], list) and seen["kwargs"]["shell"] is False
    assert (
        seen["kwargs"]["start_new_session"] is True
        and seen["kwargs"]["close_fds"] is True
    )
    assert all(isinstance(a, str) for a in seen["args"][0])


# --------------------------------------------- paid-API credential stripping


def test_parent_api_keys_and_cloud_variables_never_reach_the_claude_process(
    fake: Fake,
) -> None:
    fake.run()
    env = fake.record()["env"]
    for name in STRIPPED_ENV | set(PARENT_ENV_SECRETS):
        assert name not in env, name
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert not any(
        k.startswith(
            ("ANTHROPIC_", "CLAUDE_", "AWS_", "GOOGLE_", "OPENAI_", "GEMINI_", "XAI_")
        )
        for k in env
    )
    assert not any("PROXY" in k.upper() for k in env)
    blob = json.dumps(env)
    assert FAKE_API_KEY not in blob and FAKE_GEMINI_KEY not in blob
    assert env["HOME"] == fake.env["HOME"] and env["PATH"] == cs.SAFE_PATH


def test_build_child_env_is_an_allowlist_not_a_copy() -> None:
    env = build_child_env(
        {**PARENT_ENV_SECRETS, "HOME": "/Users/x", "RANDOM_VAR": "y"}, tmpdir="/t"
    )
    assert set(env) == {"HOME", "PATH", "TMPDIR", "TERM", "NO_COLOR"}
    assert "RANDOM_VAR" not in env
    with pytest.raises(ProviderFailure):
        build_child_env({}, tmpdir="/t")  # no HOME: fail rather than guess
    with pytest.raises(ProviderFailure):
        build_child_env({"HOME": "relative"}, tmpdir="/t")


def test_anthropic_key_and_auth_token_cannot_switch_the_provider_to_paid_api(
    fake: Fake,
) -> None:
    fake.env["ANTHROPIC_API_KEY"] = FAKE_API_KEY
    fake.env["ANTHROPIC_AUTH_TOKEN"] = "bearer-" + FAKE_API_KEY
    fake.run()
    env = fake.record()["env"]
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    # And if the run itself reports a non-subscription credential source, the
    # output is discarded and never returned.
    failure = fake.expect(
        "KEYSRC", FailureCategory.POLICY_BLOCKED, "not_subscription_auth"
    )
    assert "Hello from the fake claude" not in str(failure)


# --------------------------------------------- isolation and attestation


def test_claude_runs_in_an_empty_private_directory_that_is_removed_afterwards(
    fake: Fake,
) -> None:
    fake.run()
    record = fake.record()
    cwd = Path(record["cwd"])
    assert record["listing"] == []  # no repo files, CLAUDE.md, MCP config or user files
    assert fake.work in cwd.parents or cwd.parent == fake.work.resolve()
    repo = Path(__file__).resolve().parents[1]
    assert repo not in cwd.parents and cwd != repo
    assert not cwd.exists() and list(fake.work.iterdir()) == []
    assert record["env"]["TMPDIR"] == record["cwd"]


@pytest.mark.parametrize(
    ("marker", "code"),
    [
        ("TOOLS", "tools_not_disabled"),
        ("MCP", "mcp_not_disabled"),
        ("KEYSRC", "not_subscription_auth"),
        ("NOINIT", "attestation_missing"),
    ],
)
def test_the_run_must_prove_no_tools_no_mcp_and_subscription_auth(
    fake: Fake, marker: str, code: str
) -> None:
    failure = fake.expect(marker, FailureCategory.POLICY_BLOCKED, code)
    assert "Hello from the fake claude" not in str(failure)  # the text was discarded


def test_attestation_accepts_only_the_documented_subscription_sources() -> None:
    assert cs.SUBSCRIPTION_KEY_SOURCES == {"oauth", "none"}
    init = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}
    ok = (
        json.dumps({**init, "apiKeySource": "oauth"})
        + "\n"
        + json.dumps({"type": "result", "is_error": False, "result": "fine"})
    )
    assert parse_output(ok.encode(), 0).text == "fine"
    for source in ("user", "project", "org", "temporary", None, ""):
        bad = (
            json.dumps({**init, "apiKeySource": source})
            + "\n"
            + json.dumps({"type": "result", "result": "x"})
        )
        with pytest.raises(ProviderFailure) as caught:
            parse_output(bad.encode(), 0)
        assert caught.value.category is FailureCategory.POLICY_BLOCKED


# ------------------------------------------------- failure normalization


def test_usage_limit_is_a_rate_limit_that_never_reaches_the_paid_api(
    fake: Fake,
) -> None:
    failure = fake.expect("USAGE", FailureCategory.RATE_LIMIT, "usage_limit")
    assert "resets" not in str(failure)
    rig = make_rig(
        claude=fake.provider(),
        gemini_enabled=False,
    )
    result = rig.router.execute(request("please MODE_USAGE"))
    assert (
        result.status is FailureCategory.RATE_LIMIT
        and result.diagnostic_code == "usage_limit"
    )
    plan = rig.router.plan(request("again"))
    assert (
        ProviderId.CLAUDE_SUBSCRIPTION,
        ReasonCode.SUBSCRIPTION_LIMIT,
    ) in plan.excluded
    assert rig.paid_calls() == 0


def test_authentication_failure_is_normalized_and_can_fall_back_only_to_a_free_provider(
    fake: Fake,
) -> None:
    fake.expect("AUTH", FailureCategory.AUTH_FAILURE, "not_authenticated")
    rig = make_rig(claude=fake.provider())
    result = rig.router.execute(request("please MODE_AUTH"))
    assert (
        result.provider_id is ProviderId.GEMINI_FREE
        and result.fallback_used
        and rig.paid_calls() == 0
    )
    # Never asks for credentials: there is no code path that does.
    assert "paste" not in json.dumps(result.model_dump(mode="json")).lower()


def test_a_provider_refusal_is_normalized_and_is_not_worked_around(fake: Fake) -> None:
    fake.expect("REFUSAL", FailureCategory.REFUSAL, "provider_refusal")
    rig = make_rig(claude=fake.provider())
    result = rig.router.execute(request("please MODE_REFUSAL"))
    assert result.status is FailureCategory.REFUSAL and result.provider_policy_limited
    assert (
        rig.gemini.call_count == 0
        and ReasonCode.REFUSAL_NO_FAILOVER in result.route_reasons
    )


@pytest.mark.parametrize("marker", ["JUNK", "EXIT1", "EMPTY"])
def test_malformed_or_inconsistent_output_fails_closed(fake: Fake, marker: str) -> None:
    with pytest.raises(ProviderFailure) as caught:
        fake.run("please MODE_" + marker)
    assert caught.value.category in {
        FailureCategory.UNAVAILABLE,
        FailureCategory.INVALID_RESPONSE,
    }


# ------------------------------------------------ bounds, timeout, cleanup


def test_timeout_kills_the_whole_process_group_and_cleans_up(fake: Fake) -> None:
    started = time.monotonic()
    with pytest.raises(ProviderFailure) as caught:
        fake.run("please MODE_SLEEP", timeout=2.0)
    assert (
        caught.value.category is FailureCategory.TIMEOUT
        and time.monotonic() - started < 10
    )
    pids = json.loads((fake.dir / "pids.json").read_text())
    time.sleep(0.3)
    for pid in (pids["self"], pids["child"]):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # neither the CLI nor its child outlived the call
    assert list(fake.work.iterdir()) == []  # temp directory removed


def test_oversized_output_is_rejected_and_the_process_is_stopped(fake: Fake) -> None:
    started = time.monotonic()
    fake.expect("BIG", FailureCategory.INVALID_RESPONSE, "too_large")
    assert time.monotonic() - started < 15 and list(fake.work.iterdir()) == []


def test_oversized_input_is_refused_before_any_process_starts(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cs, "MAX_STDIN_BYTES", 50)
    with pytest.raises(ProviderFailure) as caught:
        fake.run("x" * 200)
    assert (
        caught.value.code == "request_too_large"
        and not (fake.dir / "record.json").exists()
    )


def test_a_missing_or_wrong_model_id_is_rejected(fake: Fake) -> None:
    with pytest.raises(ProviderFailure) as caught:
        fake.provider().complete(
            request(), model_id="claude-opus-anything", timeout_seconds=5
        )
    assert (
        caught.value.category is FailureCategory.POLICY_BLOCKED
        and not (fake.dir / "record.json").exists()
    )


# ------------------------------------------- executable trust and config


def test_arbitrary_executables_are_rejected(tmp_path: Path) -> None:
    install = tmp_path / "claude-install"
    install.mkdir()
    good = install / "claude"
    good.write_text("#!/bin/sh\n")
    good.chmod(0o755)
    assert validate_executable(good) == good
    # not named claude / relative / traversal / not a file / missing
    other = install / "not-claude"
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    assert validate_executable(other) is None
    assert validate_executable("claude") is None
    assert (
        validate_executable(str(install / ".." / "claude-install" / "claude")) is None
    )
    assert (
        validate_executable("/bin/sh") is None
        and validate_executable("/usr/bin/env") is None
    )
    assert (
        validate_executable(install) is None
        and validate_executable(tmp_path / "nope" / "claude") is None
    )
    # world-writable / not executable
    good.chmod(0o757)
    assert validate_executable(good) is None
    good.chmod(0o644)
    assert validate_executable(good) is None
    # a symlink merely NAMED claude that points at another program
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    (link_dir / "claude").symlink_to("/bin/sh")
    assert validate_executable(link_dir / "claude") is None


def test_a_bad_configured_path_makes_the_provider_unavailable_and_spawns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*a: Any, **k: Any) -> None:
        raise AssertionError("a process was started")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    provider = ClaudeSubscriptionProvider(executable="/bin/sh")
    assert provider.availability() is Availability.NOT_CONFIGURED
    with pytest.raises(ProviderFailure) as caught:
        provider.complete(
            request(), model_id=CLAUDE_SUBSCRIPTION_MODEL, timeout_seconds=5
        )
    assert (
        caught.value.category is FailureCategory.UNAVAILABLE
        and caught.value.code == "not_installed"
    )


def test_disabled_provider_spawns_no_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*a: Any, **k: Any) -> None:
        raise AssertionError("a process was started")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    rig = make_rig(claude_enabled=False, gemini_enabled=False)
    assert rig.router.execute(request()).status is FailureCategory.COST_BLOCKED


def test_argv_builder_is_fixed_and_takes_no_caller_controlled_text() -> None:
    a = build_argv(Path("/x/claude"))
    b = build_argv(Path("/x/claude"))
    assert a == b and a[0] == "/x/claude"
    import inspect

    assert list(inspect.signature(build_argv).parameters) == ["executable"]


def _code_only(path: Path) -> str:
    """The module's executable code with every docstring removed."""

    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# --------------------------------------- Sam never touches Claude credentials

_AUDITED: list[str] = []
_ACTIVE = False


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if _ACTIVE and event == "open" and args and isinstance(args[0], str):
        _AUDITED.append(args[0])


sys.addaudithook(_hook)


def test_sam_opens_no_claude_credential_or_config_file_while_calling_claude(
    fake: Fake,
) -> None:
    global _ACTIVE
    _AUDITED.clear()
    _ACTIVE = True
    try:
        fake.run()
    finally:
        _ACTIVE = False
    touched = [
        p
        for p in _AUDITED
        if ".claude" in p or "credentials" in p.lower() or "Keychain" in p
    ]
    assert touched == []


def test_source_never_reads_credentials_or_uses_private_claude_apis() -> None:
    root = Path(cs.__file__).resolve().parents[1]
    banned = (
        ".credentials.json",
        "Keychain",
        "find-generic-password",
        "~/.claude",
        "cookies",
        "claude.ai/api",
        "setup-token",
        "sessionKey",
        "refresh_token",
        "anthropic.com/v1",
    )
    for path in root.rglob("*.py"):
        code = _code_only(path)
        for token in banned:
            assert token not in code, (path.name, token)
        assert "shell=True" not in code and "os.system" not in code, path.name


def test_only_the_claude_adapter_starts_processes_or_reads_the_environment() -> None:
    root = Path(cs.__file__).resolve().parents[1]
    for path in root.rglob("*.py"):
        code = _code_only(path)
        if path.name != "claude_subscription.py":
            assert "subprocess" not in code and "os.environ" not in code, path.name
        if path.name not in {"gemini.py", "factory.py"}:
            assert "httpx" not in code, path.name
        # No Anthropic/OpenAI/xAI SDK is imported anywhere in the model layer.
        assert not re.search(r"^\s*(import|from)\s+(anthropic|openai|xai)", code, re.M)


def test_the_paid_anthropic_api_provider_is_not_reachable_from_production_wiring() -> (
    None
):
    src = Path(cs.__file__).resolve().parents[2]  # src/sam
    for path in src.rglob("*.py"):
        if path.name == "claude.py" and path.parent.name == "agent":
            continue
        text = path.read_text()
        assert "sam.agent.claude" not in text and "ClaudeProvider(" not in text, path
    main = (src / "main.py").read_text()
    assert "ClaudeProvider" not in main and "build_model_router" in main


def test_private_content_marker_never_appears_in_audit_or_results(fake: Fake) -> None:
    rig = make_rig(claude=fake.provider())
    result = rig.router.execute(request(PRIVATE_TEXT, privacy=PrivacyClass.PRIVATE))
    assert (
        result.status is FailureCategory.SUCCESS
        and result.provider_id is ProviderId.CLAUDE_SUBSCRIPTION
    )
    assert (
        PRIVATE_TEXT not in audit_json(rig)
        and PRIVATE_TEXT not in result.model_dump_json()
    )


# ===================== Phase 13 review remediation: isolation, auth, deployment

MANDATORY_SWITCHES = (
    "--restricted",
    "--safe-mode",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--no-session-persistence",
    "--no-chrome",
)
MANDATORY_PAIRS = (
    ("--tools", ""),
    ("--disallowedTools", "*"),
    ("--permission-prompts", "none"),
    ("--permission-mode", "dontAsk"),
    ("--max-turns", "1"),
)


def _without(argv: list[str], flag: str, *, value: bool) -> list[str]:
    index = argv.index(flag)
    return argv[:index] + argv[index + (2 if value else 1) :]


def test_restricted_is_mandatory_in_the_final_argv() -> None:
    argv = build_argv(Path("/x/claude"))
    assert "--restricted" in argv
    cs.verify_isolation(argv)
    with pytest.raises(ProviderFailure) as caught:
        cs.verify_isolation(_without(argv, "--restricted", value=False))
    assert caught.value.code == "isolation_flags_missing"


def test_permission_prompts_none_is_mandatory_in_the_final_argv() -> None:
    argv = build_argv(Path("/x/claude"))
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    for weaker in (
        _without(argv, "--permission-prompts", value=True),
        [
            *argv[: argv.index("--permission-prompts") + 1],
            "ask",
            *argv[argv.index("--permission-prompts") + 2 :],
        ],
    ):
        with pytest.raises(ProviderFailure) as caught:
            cs.verify_isolation(weaker)
        assert caught.value.code == "isolation_flags_missing"


@pytest.mark.parametrize("flag", MANDATORY_SWITCHES)
def test_every_mandatory_switch_is_verified(flag: str) -> None:
    argv = _without(build_argv(Path("/x/claude")), flag, value=False)
    with pytest.raises(ProviderFailure):
        cs.verify_isolation(argv)


@pytest.mark.parametrize(("flag", "value"), MANDATORY_PAIRS)
def test_every_mandatory_flag_value_pair_is_verified(flag: str, value: str) -> None:
    argv = build_argv(Path("/x/claude"))
    with pytest.raises(ProviderFailure):
        cs.verify_isolation(_without(argv, flag, value=True))
    changed = list(argv)
    changed[changed.index(flag) + 1] = "changed"
    with pytest.raises(ProviderFailure):
        cs.verify_isolation(changed)


@pytest.mark.parametrize(
    "extra",
    [
        "--dangerously-skip-permissions",
        "--allowedTools",
        "--mcp-config",
        "--bare",
        "--settings",
        "--chrome",
    ],
)
def test_weakening_flags_are_refused(extra: str) -> None:
    with pytest.raises(ProviderFailure):
        cs.verify_isolation([*build_argv(Path("/x/claude")), extra])


def test_a_weakened_argv_starts_no_process_at_all(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = cs.build_argv
    monkeypatch.setattr(
        cs, "build_argv", lambda exe: _without(real(exe), "--restricted", value=False)
    )
    with pytest.raises(ProviderFailure) as caught:
        fake.run()
    assert caught.value.code == "isolation_flags_missing" and not fake.inference_ran()
    monkeypatch.setattr(
        cs,
        "build_argv",
        lambda exe: _without(real(exe), "--permission-prompts", value=True),
    )
    with pytest.raises(ProviderFailure):
        fake.run()
    assert not fake.inference_ran()


@pytest.mark.parametrize(
    "missing", ["--restricted", "--permission-prompts", "--safe-mode", "--no-chrome"]
)
def test_a_cli_lacking_an_isolation_flag_is_unavailable_never_weaker(
    fake: Fake, missing: str
) -> None:
    (fake.dir / "help.txt").write_text(
        "\n".join(f for f in cs.REQUIRED_HELP_FLAGS if f != missing)
    )
    provider = fake.provider()
    assert provider.availability() is Availability.UNAVAILABLE
    assert provider.detail() == "cli_unsupported"
    with pytest.raises(ProviderFailure) as caught:
        provider.complete(
            request(), model_id=CLAUDE_SUBSCRIPTION_MODEL, timeout_seconds=5
        )
    assert caught.value.code == "cli_unsupported" and not fake.inference_ran()


@pytest.mark.parametrize("version", ["2.1.258 (Claude Code)", "1.0.0", "garbage", ""])
def test_an_old_or_unreadable_cli_version_is_unavailable(
    fake: Fake, version: str
) -> None:
    (fake.dir / "version.txt").write_text(version)
    provider = fake.provider()
    assert provider.availability() is Availability.UNAVAILABLE
    assert provider.detail() == "cli_unsupported" and not fake.inference_ran()


# ------------------------------------------------------ managed policy


def test_managed_policy_present_fails_closed_without_reading_it(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / "managed-settings.json"
    policy.write_text('{"hooks": {"anything": []}}')
    opened: list[str] = []
    real_open = Path.read_text

    def spy(self: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    provider = fake.provider(managed_policy_paths=(policy,))
    assert provider.availability() is Availability.UNAVAILABLE
    assert provider.detail() == "managed_policy_present"
    with pytest.raises(ProviderFailure) as caught:
        provider.complete(
            request(), model_id=CLAUDE_SUBSCRIPTION_MODEL, timeout_seconds=5
        )
    assert caught.value.code == "managed_policy_present" and not fake.inference_ran()
    assert str(policy) not in opened  # existence only; contents never read


def test_absent_managed_policy_is_not_a_blocker(fake: Fake, tmp_path: Path) -> None:
    provider = fake.provider(managed_policy_paths=(tmp_path / "nope.json",))
    assert provider.availability() is Availability.AVAILABLE
    assert cs.managed_policy_present([tmp_path / "nope.json"]) is False
    assert cs.managed_policy_present([tmp_path]) is True  # a directory counts too
    assert any("managed-settings" in str(p) for p in cs.MANAGED_POLICY_PATHS)


# ------------------------------------------------- auth classification

SUBSCRIPTION_DOC: dict[str, Any] = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "email": "owner@example.invalid",
    "orgId": "org",
    "orgName": "org",
    "subscriptionType": "max",
}


def test_exact_subscription_login_is_accepted() -> None:
    for plan in ("pro", "max"):
        verdict = cs.classify_auth({**SUBSCRIPTION_DOC, "subscriptionType": plan})
        assert (
            verdict.ok and verdict.code == "subscription_login" and verdict.plan == plan
        )
    # Identity fields are dropped: the verdict carries no email or organization.
    assert "owner@example.invalid" not in repr(cs.classify_auth(SUBSCRIPTION_DOC))


REJECTED = {
    "api_key": (
        {"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"},
        "api_key_auth",
    ),
    "auth_token": (
        {"loggedIn": True, "authMethod": "oauth_token", "apiProvider": "firstParty"},
        "token_auth",
    ),
    "oauth_token_env": ({"loggedIn": True, "authMethod": "oauth_token"}, "token_auth"),
    "cloud_bedrock": (
        {"loggedIn": True, "authMethod": "third_party", "apiProvider": "bedrock"},
        "third_party_auth",
    ),
    "cloud_vertex": (
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "vertex"},
        "third_party_auth",
    ),
    "gateway_provider": (
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "gateway"},
        "third_party_auth",
    ),
    "not_logged_in": ({"loggedIn": False, "authMethod": "none"}, "not_logged_in"),
    "logged_in_missing_method": ({"loggedIn": True}, "not_logged_in"),
    "console_payg": ({**SUBSCRIPTION_DOC, "authMethod": "console"}, "ambiguous_auth"),
    "unknown_method": (
        {**SUBSCRIPTION_DOC, "authMethod": "something_new"},
        "ambiguous_auth",
    ),
    "profile_federation": (
        {**SUBSCRIPTION_DOC, "profile": "work", "federation": True},
        "ambiguous_auth",
    ),
    "api_key_helper": (
        {**SUBSCRIPTION_DOC, "apiKeyHelper": "/x/helper"},
        "ambiguous_auth",
    ),
    "extra_unknown_field": ({**SUBSCRIPTION_DOC, "newThing": 1}, "ambiguous_auth"),
    "no_plan": (
        {k: v for k, v in SUBSCRIPTION_DOC.items() if k != "subscriptionType"},
        "unsupported_plan",
    ),
    "enterprise_plan": (
        {**SUBSCRIPTION_DOC, "subscriptionType": "enterprise"},
        "unsupported_plan",
    ),
    "free_plan": ({**SUBSCRIPTION_DOC, "subscriptionType": "free"}, "unsupported_plan"),
    "logged_in_string": ({**SUBSCRIPTION_DOC, "loggedIn": "true"}, "not_logged_in"),
    "list_document": ([], "ambiguous_auth"),
    "null_document": (None, "ambiguous_auth"),
    "string_document": ("claude.ai", "ambiguous_auth"),
}


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_every_non_subscription_auth_source_is_rejected(name: str) -> None:
    document, code = REJECTED[name]
    verdict = cs.classify_auth(document)
    assert verdict.ok is False and verdict.code == code


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_a_rejected_auth_source_makes_the_provider_unavailable_and_runs_no_inference(
    fake: Fake, name: str
) -> None:
    document, code = REJECTED[name]
    fake.set_auth(document)
    provider = fake.provider()
    assert provider.availability() is not Availability.AVAILABLE
    assert provider.detail() == code
    with pytest.raises(ProviderFailure):
        provider.complete(
            request(), model_id=CLAUDE_SUBSCRIPTION_MODEL, timeout_seconds=5
        )
    # A run that would have SUCCEEDED is never made, so success cannot be
    # mistaken for proof of a subscription login.
    assert not fake.inference_ran()


@pytest.mark.parametrize("payload", ["not json", "", "{", "[1,2]"])
def test_unreadable_auth_status_fails_closed(fake: Fake, payload: str) -> None:
    (fake.dir / "auth.json").write_text(payload)
    provider = fake.provider()
    assert provider.availability() is Availability.UNAVAILABLE
    assert provider.detail() in {"auth_status_unreadable", "ambiguous_auth"}
    assert not fake.inference_ran()


def test_a_failing_auth_status_command_fails_closed(fake: Fake) -> None:
    (fake.dir / "auth.exit").write_text("7")
    provider = fake.provider()
    assert (
        provider.availability() is Availability.UNAVAILABLE and not fake.inference_ran()
    )


def test_a_successful_request_does_not_prove_subscription_login(fake: Fake) -> None:
    fake.set_auth({**SUBSCRIPTION_DOC, "apiKeyHelper": "/x/helper"})  # ambiguous
    with pytest.raises(ProviderFailure):
        fake.run()
    assert not fake.inference_ran()  # even though the fake would have answered "OK"


def test_the_auth_probe_uses_the_official_command_with_a_sanitized_environment(
    fake: Fake,
) -> None:
    fake.run()
    lines = [json.loads(x) for x in (fake.dir / "probes.log").read_text().splitlines()]
    assert lines and all(x["argv"] == ["auth", "status", "--json"] for x in lines)
    for probe in lines:
        env = probe["env"]
        assert not (STRIPPED_ENV | set(PARENT_ENV_SECRETS)) & set(env)
        assert env["PATH"] == cs.SAFE_PATH and env["HOME"] == fake.env["HOME"]
        assert not any(
            k.startswith(("ANTHROPIC_", "CLAUDE_", "AWS_", "GOOGLE_")) for k in env
        )


def test_the_auth_probe_never_returns_or_stores_identity_fields(fake: Fake) -> None:
    provider = fake.provider()
    provider.availability()
    assert "owner@example.invalid" not in repr(vars(provider))
    assert "owner@example.invalid" not in json.dumps(provider.detail())


def test_a_failed_auth_run_forces_reverification_before_the_next_use(
    fake: Fake,
) -> None:
    provider = fake.provider()
    assert provider.availability() is Availability.AVAILABLE
    with pytest.raises(ProviderFailure):
        provider.complete(
            request("please MODE_AUTH"),
            model_id=CLAUDE_SUBSCRIPTION_MODEL,
            timeout_seconds=10,
        )
    fake.set_auth({"loggedIn": False, "authMethod": "none"})
    assert provider.availability() is Availability.UNAUTHORIZED


# ------------------------------------------------------- OWNER_LOCAL gate


@pytest.mark.parametrize(
    "mode",
    [DeploymentMode.MULTI_USER, DeploymentMode.HOSTED, DeploymentMode.DISTRIBUTED],
)
def test_non_owner_local_deployment_disables_claude_subscription_and_spawns_nothing(
    fake: Fake, mode: DeploymentMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = fake.provider(deployment_mode=mode)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")),
    )
    assert provider.availability() is Availability.DISABLED
    assert provider.disabled_detail == "deployment_not_owner_local"
    with pytest.raises(ProviderFailure) as caught:
        provider.complete(
            request(), model_id=CLAUDE_SUBSCRIPTION_MODEL, timeout_seconds=5
        )
    assert caught.value.category is FailureCategory.POLICY_BLOCKED
    assert caught.value.code == "deployment_not_owner_local"
    registry = build_registry(gemini_configured=True, deployment_mode=mode)
    assert registry.get(ProviderId.CLAUDE_SUBSCRIPTION).enabled is False


def test_owner_local_is_the_only_mode_that_enables_claude_and_is_the_default() -> None:
    assert Settings(_env_file=None).deployment_mode is DeploymentMode.OWNER_LOCAL  # type: ignore[call-arg]
    for mode in DeploymentMode:
        enabled = (
            build_registry(gemini_configured=True, deployment_mode=mode)
            .get(ProviderId.CLAUDE_SUBSCRIPTION)
            .enabled
        )
        assert enabled is (mode is DeploymentMode.OWNER_LOCAL)
    assert (
        build_registry(gemini_configured=True, claude_enabled=False)
        .get(ProviderId.CLAUDE_SUBSCRIPTION)
        .enabled
        is False
    )


def test_deployment_mode_is_trusted_configuration_not_a_request_field() -> None:
    assert not any("deployment" in name for name in ModelRequest.model_fields)
    assert not any("deployment" in name for name in ProviderOutput.model_fields)
    text = Path(cs.__file__).resolve().parents[1].joinpath("adapter.py").read_text()
    assert "deployment" not in text  # nothing routed from a prompt can set it


def test_the_router_reports_the_deployment_reason_for_a_disabled_claude(
    fake: Fake,
) -> None:
    provider = fake.provider(deployment_mode=DeploymentMode.HOSTED)
    rig = make_rig(
        claude=provider,
        claude_enabled=False,
        holder=Holder(attested()),
    )
    assert (
        rig.router.provider_detail(ProviderId.CLAUDE_SUBSCRIPTION)
        == "deployment_not_owner_local"
    )
    result = rig.router.execute(request())
    assert result.provider_id is ProviderId.GEMINI_FREE and not fake.inference_ran()
