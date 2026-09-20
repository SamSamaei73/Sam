"""The explicit, offline Obsidian mirror (scripts/obsidian_sync.py).

Every test uses temporary repositories and vaults. Nothing here reads, writes
or depends on the real Obsidian/ vault; the real repository is only inspected
read-only for its ignore rule and tracked-file count.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "obsidian_sync.py"
GIT = "/usr/bin/git"

pytestmark = pytest.mark.skipif(
    not os.path.exists(GIT), reason="the sync tool requires /usr/bin/git"
)

_spec = importlib.util.spec_from_file_location("obsidian_sync_under_test", SCRIPT)
assert _spec is not None and _spec.loader is not None
sync_mod: Any = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sync_mod  # dataclasses need the module registered
_spec.loader.exec_module(sync_mod)
SyncError = sync_mod.SyncError

BASE_SPEC: dict[str, Any] = {
    "project": {"name": "Sam", "version": "0.1.0", "type": "personal_ai_agent"},
    "phases": [
        {
            "id": 1,
            "name": "foundation",
            "status": "approved",
            "scope": ["FastAPI", "configuration"],
        },
        {
            "id": 2,
            "name": "agent_core",
            "status": "in_progress",
            "scope": ["provider abstraction"],
            "scope_note": "The agent core keeps a single provider boundary.",
        },
        {"id": 3, "name": "memory", "status": "not_started"},
    ],
    "current_state": {"phase": 2, "status": "in_progress"},
    "security": {
        "principles": ["least_privilege", "deny_by_default"],
        "never_allow_by_default": ["force_push", "credential extraction"],
    },
    "risk_levels": {
        "LOW": {
            "description": "Safe read-only operations",
            "default_confirmation": False,
        },
        "HIGH": {"description": "Sensitive operations", "default_confirmation": True},
    },
    "architecture": {
        "layers": ["interface", "agent_core"],
        "flow": ["input", "result"],
    },
    "development_rules": {
        "before_each_phase": ["inspect_current_repository"],
        "after_each_phase": ["run_tests", "stop"],
        "important": "Never silently continue into the next phase.",
    },
    "technology": {"backend": {"language": "Python", "framework": "FastAPI"}},
    "mcp_integrations": {
        "gmail": {"purpose": "email access", "default_permissions": ["read", "draft"]}
    },
}


def run_git(repo: Path, *args: str) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(repo),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run([GIT, *args], cwd=repo, env=env, check=True, capture_output=True)


def make_repo(
    tmp_path: Path,
    spec: dict[str, Any] | None = None,
    gitignore: str | None = "Obsidian/\n",
) -> Path:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    write_spec(repo, spec if spec is not None else BASE_SPEC)
    files = ["PROJECT_SPEC.json"]
    if gitignore is not None:
        (repo / ".gitignore").write_text(gitignore)
        files.append(".gitignore")
    run_git(repo, "add", *files)
    run_git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture")
    return repo


def write_spec(repo: Path, spec: dict[str, Any]) -> None:
    (repo / "PROJECT_SPEC.json").write_text(json.dumps(spec))


def make_workspace(repo: Path, *, registry: Path | None = None) -> Any:
    vault = repo / "Obsidian"
    (vault / ".obsidian").mkdir(parents=True)
    return sync_mod.Workspace(repo, vault, registry)


def snapshot(root: Path) -> dict[str, bytes | None]:
    """Every file (bytes) and directory (None) below ``root``."""
    result: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        result[key] = path.read_bytes() if path.is_file() else None
    return result


def statuses(changes: list[Any]) -> set[str]:
    return {c.status for c in changes}


@pytest.fixture
def ws(tmp_path: Path) -> Any:
    workspace = make_workspace(make_repo(tmp_path))
    real = (ROOT / "Obsidian").resolve()
    assert real not in workspace.vault.resolve().parents and workspace.vault != real
    return workspace


# ------------------------------------------------------ 1. vault validation


def test_vault_must_exist_and_carry_an_obsidian_marker(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    missing = sync_mod.Workspace(repo, repo / "Obsidian", None)
    with pytest.raises(SyncError):
        missing.validate_vault()
    (repo / "Obsidian").mkdir()  # exists but is not an Obsidian vault
    with pytest.raises(SyncError):
        missing.validate_vault()
    (repo / "Obsidian" / ".obsidian").mkdir()
    assert missing.validate_vault() == ".obsidian directory"


def test_an_exact_open_registry_entry_can_stand_in_for_the_marker(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    vault = repo / "Obsidian"
    vault.mkdir()
    registry = tmp_path / "obsidian.json"
    registry.write_text(
        json.dumps({"vaults": {"a": {"path": str(vault), "open": True}}})
    )
    assert (
        sync_mod.Workspace(repo, vault, registry).validate_vault().startswith("exact")
    )
    registry.write_text(json.dumps({"vaults": {"a": {"path": str(vault)}}}))  # not open
    with pytest.raises(SyncError):
        sync_mod.Workspace(repo, vault, registry).validate_vault()


def test_a_symlinked_vault_or_marker_is_rejected(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    real_vault = tmp_path / "real-vault"
    (real_vault / ".obsidian").mkdir(parents=True)
    link = tmp_path / "linked-vault"
    link.symlink_to(real_vault)
    with pytest.raises(SyncError):
        sync_mod.Workspace(repo, link, None).validate_vault()
    vault = tmp_path / "vault2"
    vault.mkdir()
    (vault / ".obsidian").symlink_to(real_vault / ".obsidian")
    with pytest.raises(SyncError):
        sync_mod.Workspace(repo, vault, None).validate_vault()


def test_resolve_vault_defaults_normalizes_and_confines_repository_vaults(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    assert sync_mod.resolve_vault(None, repo) == repo / "Obsidian"
    messy = str(tmp_path / "a" / ".." / "vault")
    assert sync_mod.resolve_vault(messy, repo) == tmp_path / "vault"
    assert sync_mod.resolve_vault(str(repo / "Obsidian"), repo) == repo / "Obsidian"
    for bad in (
        str(repo),
        str(repo / "docs"),
        str(repo / "Obsidian" / "Sam"),
        "",
        "  ",
    ):
        with pytest.raises(SyncError):
            sync_mod.resolve_vault(bad, repo)
    with pytest.raises(SyncError):
        sync_mod.resolve_vault("bad\0path", repo)


# ----------------------------------------------- 2. Obsidian/ stays ignored


def test_the_real_repository_ignores_obsidian_and_tracks_nothing_in_it() -> None:
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert "Obsidian/" in lines
    tracked = subprocess.run(
        [GIT, "ls-files", "--", "Obsidian"], cwd=ROOT, capture_output=True, check=True
    )
    assert tracked.stdout == b""
    ignored = subprocess.run([GIT, "check-ignore", "-q", "Obsidian"], cwd=ROOT)
    assert ignored.returncode == 0


def test_obsidian_app_settings_directories_are_ignored_too() -> None:
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert ".obsidian/" in lines
    for path in ("scripts/.obsidian/workspace.json", ".obsidian/app.json"):
        ignored = subprocess.run([GIT, "check-ignore", "-q", path], cwd=ROOT)
        assert ignored.returncode == 0, path


def test_sync_refuses_when_the_ignore_rule_is_missing(tmp_path: Path) -> None:
    workspace = make_workspace(make_repo(tmp_path, gitignore=None))
    with pytest.raises(SyncError):
        sync_mod.sync(workspace, dry_run=True)
    assert snapshot(workspace.vault) == {".obsidian": None}


def test_sync_refuses_when_obsidian_paths_are_tracked_or_staged(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    workspace = make_workspace(repo)
    (repo / "Obsidian" / "private.md").write_text("private note")
    run_git(repo, "add", "-f", "Obsidian/private.md")  # staged
    with pytest.raises(SyncError):
        sync_mod.sync(workspace, dry_run=True)
    run_git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "oops")  # tracked
    with pytest.raises(SyncError):
        sync_mod.sync(workspace, dry_run=True)


# ------------------------------------------ 3-8. managed blocks, idempotence


def test_managed_block_is_created_with_frontmatter(ws: Any) -> None:
    changes = sync_mod.sync(ws)
    assert statuses(changes) == {"created"}
    text = (ws.destination / "Dashboard.md").read_text()
    assert text.startswith("---\nsam_managed: true\n---\n<!-- SAM:AUTO:START -->\n")
    assert text.endswith("<!-- SAM:AUTO:END -->\n")
    assert text.count("SAM:AUTO:START") == 1 and text.count("SAM:AUTO:END") == 1
    assert (ws.destination / "Roadmap.md").is_file()
    assert (ws.destination / "Phases" / "Phase 02 - Agent Core.md").is_file()
    assert (ws.destination / "Security" / "Security Principles.md").is_file()


def test_only_the_managed_block_is_replaced_and_human_text_survives(
    ws: Any, tmp_path: Path
) -> None:
    sync_mod.sync(ws)
    note = ws.destination / "Roadmap.md"
    original = note.read_text()
    human_before = "My own intro, written by me.\n\n"
    human_after = "\n\n## My notes\nLinks and thoughts that are mine.\n"
    edited = original.replace(
        "<!-- SAM:AUTO:START -->", human_before + "<!-- SAM:AUTO:START -->"
    )
    edited = edited.replace(
        "<!-- SAM:AUTO:END -->", "<!-- SAM:AUTO:END -->" + human_after
    )
    note.write_text(edited)

    spec = copy.deepcopy(BASE_SPEC)
    spec["phases"][2]["status"] = "in_progress"
    write_spec(ws.repo, spec)
    changes = sync_mod.sync(ws)

    updated = note.read_text()
    assert updated != edited
    START, END = "<!-- SAM:AUTO:START -->", "<!-- SAM:AUTO:END -->"
    assert updated.split(START)[0] == edited.split(START)[0]  # human text above
    assert updated.split(END)[1] == edited.split(END)[1]  # human text below
    assert human_before in updated and "## My notes" in updated
    assert "| 3 |" in updated and "in_progress" in updated.split("| 3 |")[1]
    assert {c.rel: c.status for c in changes}["Roadmap.md"] == "updated"


def test_a_second_sync_is_idempotent(ws: Any) -> None:
    sync_mod.sync(ws)
    before = snapshot(ws.vault)
    changes = sync_mod.sync(ws)
    assert statuses(changes) == {"unchanged"}
    assert snapshot(ws.vault) == before


def test_output_is_deterministic_across_vaults(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    first = make_workspace(repo)
    sync_mod.sync(first)
    second_vault = tmp_path / "second-vault"
    (second_vault / ".obsidian").mkdir(parents=True)
    second = sync_mod.Workspace(repo, second_vault, None)
    sync_mod.sync(second)
    assert snapshot(first.destination) == snapshot(second.destination)


@pytest.mark.parametrize(
    "broken",
    [
        "text\n<!-- SAM:AUTO:START -->\nno end marker\n",
        "<!-- SAM:AUTO:END -->\nend first\n<!-- SAM:AUTO:START -->\n",
        "<!-- SAM:AUTO:START -->\na\n<!-- SAM:AUTO:START -->\nb\n"
        "<!-- SAM:AUTO:END -->\n",
        "<!-- SAM:AUTO:START -->\na\n<!-- SAM:AUTO:END -->\n<!-- SAM:AUTO:END -->\n",
    ],
    ids=["missing-end", "reversed", "duplicate-start", "duplicate-end"],
)
def test_malformed_or_duplicate_markers_fail_closed(ws: Any, broken: str) -> None:
    sync_mod.sync(ws)
    victim = ws.destination / "Roadmap.md"
    victim.write_text(broken)
    before = snapshot(ws.vault)
    spec = copy.deepcopy(BASE_SPEC)
    spec["phases"][2]["status"] = "approved"
    write_spec(ws.repo, spec)
    with pytest.raises(SyncError):
        sync_mod.sync(ws)
    assert snapshot(ws.vault) == before  # nothing changed, human text intact


def test_a_user_note_without_markers_is_left_intact(ws: Any) -> None:
    ws.destination.mkdir()
    mine = ws.destination / "Dashboard.md"
    mine.write_text("# My own dashboard\nno markers here\n")
    before = snapshot(ws.vault)
    with pytest.raises(SyncError, match="left intact"):
        sync_mod.sync(ws)
    assert snapshot(ws.vault) == before


# -------------------------------------- 9-12. secrets and private material

REJECTED = [
    "password: hunter2hunter2",
    "api_key = " + "sk-" + "ant-abcdefghijklmnopqrstuvwx",
    "Authorization: Bearer abcdefghijklmnop",
    "AI" + "zaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456",
    "gh" + "p_abcdefghijklmnopqrstuvwxyz0123456789",
    "xo" + "xb-1234567890-abcdefghij",
    "ey"
    + "JhbGciOiJIUzI1NiJ9."
    + "ey"
    + "JzdWIiOiIxMjM0NTY3ODkwIn0."
    + "abcdefghijklmnop",
    "-----BEGIN RSA PRIVATE " + "KEY-----",
    "biometric embeddings: stored locally",
    "speaker embeddings = abc123",
    "voice template is enrolled",
    "owner proof: abcdef",
    "desktop step-up secret: abcdefgh",
    "0.11, 0.12, 0.13, 0.14, 0.15, 0.16, 0.17, 0.18, 0.19",
    "see recording sam-persian.m4a",
    "captured at /tmp/capture.wav",
    "raw recordings: kept",
]


@pytest.mark.parametrize("payload", REJECTED)
def test_secret_biometric_and_recording_material_is_rejected_whole(
    ws: Any, payload: str
) -> None:
    spec = copy.deepcopy(BASE_SPEC)
    spec["phases"][2]["scope"] = ["harmless", payload]
    write_spec(ws.repo, spec)
    before = snapshot(ws.vault)
    with pytest.raises(SyncError) as caught:
        sync_mod.sync(ws)
    assert payload not in str(caught.value)  # the error never echoes the material
    assert snapshot(ws.vault) == before  # nothing partially published


def test_a_secret_hidden_in_user_text_stops_the_note_from_being_touched(
    ws: Any,
) -> None:
    sync_mod.sync(ws)
    note = ws.destination / "Roadmap.md"
    note.write_text(
        note.read_text()
        + "\nmy token: "
        + "gh"
        + "p_abcdefghijklmnopqrstuvwxyz0123456789\n"
    )
    before = snapshot(ws.vault)
    with pytest.raises(SyncError):
        sync_mod.sync(ws)
    assert snapshot(ws.vault) == before


def test_rejection_of_one_note_publishes_no_other_note(ws: Any) -> None:
    spec = copy.deepcopy(BASE_SPEC)
    spec["mcp_integrations"]["gmail"]["purpose"] = "token: abcdef0123456789abcdef"
    write_spec(ws.repo, spec)
    with pytest.raises(SyncError):
        sync_mod.sync(ws)
    assert not ws.destination.exists()


def test_ordinary_sensitive_words_in_prose_are_not_blocked(ws: Any) -> None:
    """The detector targets secret-shaped values, not vocabulary."""
    spec = copy.deepcopy(BASE_SPEC)
    spec["phases"][2]["scope"] = [
        "credential isolation",
        "token budgeting policy",
        "voice identity is an authentication signal",
    ]
    write_spec(ws.repo, spec)
    assert statuses(sync_mod.sync(ws)) == {"created"}


# -------------------------------------------------------------- 13. dry-run


def test_dry_run_reports_but_writes_nothing(ws: Any) -> None:
    before = snapshot(ws.vault)
    changes = sync_mod.sync(ws, dry_run=True)
    assert statuses(changes) == {"created"} and len(changes) > 5
    assert snapshot(ws.vault) == before
    assert not ws.destination.exists()  # not even the directory

    sync_mod.sync(ws)
    assert statuses(sync_mod.sync(ws, dry_run=True)) == {"unchanged"}
    spec = copy.deepcopy(BASE_SPEC)
    spec["phases"][2]["status"] = "approved"
    write_spec(ws.repo, spec)
    before = snapshot(ws.vault)
    assert "updated" in statuses(sync_mod.sync(ws, dry_run=True))
    assert snapshot(ws.vault) == before


def test_dry_run_still_enforces_every_safety_check(ws: Any) -> None:
    spec = copy.deepcopy(BASE_SPEC)
    spec["phases"][2]["scope"] = ["password: hunter2hunter2"]
    write_spec(ws.repo, spec)
    with pytest.raises(SyncError):
        sync_mod.sync(ws, dry_run=True)


# -------------------------------------------------------------- 14. network


def test_sync_makes_no_network_calls(ws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("network access attempted")

    for name in ("connect", "connect_ex", "bind", "listen", "sendto"):
        monkeypatch.setattr(socket.socket, name, refuse)
    for name in ("create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, name, refuse)
    assert statuses(sync_mod.sync(ws)) == {"created"}


def test_script_has_no_network_daemon_watcher_or_git_write_capability() -> None:
    tree = ast.parse(SCRIPT.read_text())
    tree.body = [
        n
        for n in tree.body
        if not (
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        )
    ]  # drop the module docstring: only executable code is scanned
    source = ast.unparse(tree)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported <= {
        "__future__",
        "argparse",
        "json",
        "os",
        "re",
        "secrets",
        "selectors",
        "stat",
        "subprocess",
        "sys",
        "time",
        "unicodedata",
        "collections.abc",
        "contextlib",
        "dataclasses",
        "datetime",
        "pathlib",
        "typing",
        "sam.memory.sanitization",
    }, imported
    banned = (
        "socket",
        "urllib",
        "http",
        "httpx",
        "requests",
        "ssl",
        "asyncio",
        "threading",
        "multiprocessing",
        "watchdog",
        "fsevents",
        "sched",
        "launchd",
        "crontab",
        "shell=True",
        "os.system",
        "os.fork",
        "time.sleep",
    )
    for token in banned:
        assert token not in source, token
    # Only one subprocess call site: the fixed, read-only git helper.
    assert source.count("subprocess.Popen(") == 1 and "'/usr/bin/git'" in source
    git_subcommands = {
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "git"
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
    }
    assert git_subcommands <= {"rev-parse", "ls-files", "diff", "check-ignore"}
    options = set(re.findall(r"add_argument\(\s*'(--[a-z-]+)'", source))
    assert options == {"--vault", "--dry-run", "--verification"}


def test_script_never_names_the_owners_real_vault_or_personal_paths() -> None:
    assert "/Users/" not in SCRIPT.read_text()


# ------------------------------------------- 16-17. traversal and symlinks


@pytest.mark.parametrize(
    "bad",
    [
        "../escape.md",
        "/abs/path.md",
        "a/b/c.md",
        "Phases/../../x.md",
        ".hidden.md",
        "Phases/.hidden.md",
        "note.txt",
        "note",
        "we\\ird.md",
        "sp*ce.md",
        "",
    ],
)
def test_note_paths_cannot_traverse_or_be_unsafe(bad: str) -> None:
    with pytest.raises(SyncError):
        sync_mod.relative_note(bad)


def test_a_traversal_phase_name_is_rejected_before_anything_is_written(
    ws: Any,
) -> None:
    spec = copy.deepcopy(BASE_SPEC)
    spec["phases"][0]["name"] = "../../escape"
    write_spec(ws.repo, spec)
    with pytest.raises(SyncError):
        sync_mod.sync(ws)
    assert not ws.destination.exists()


def test_a_symlinked_destination_directory_cannot_escape_the_vault(
    ws: Any, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    ws.destination.symlink_to(outside)
    with pytest.raises(SyncError):
        sync_mod.sync(ws)
    assert list(outside.iterdir()) == []


def test_a_symlinked_subdirectory_or_note_cannot_escape_the_vault(
    ws: Any, tmp_path: Path
) -> None:
    sync_mod.sync(ws)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_target = outside / "target.md"
    secret_target.write_text("outside content\n")

    phases = ws.destination / "Phases"
    moved = ws.destination / "Phases-real"
    phases.rename(moved)
    phases.symlink_to(outside)
    spec = copy.deepcopy(BASE_SPEC)
    spec["phases"][2]["status"] = "approved"
    write_spec(ws.repo, spec)
    with pytest.raises(SyncError):
        sync_mod.sync(ws)
    assert sorted(p.name for p in outside.iterdir()) == ["target.md"]
    phases.unlink()
    moved.rename(phases)

    roadmap = ws.destination / "Roadmap.md"
    roadmap.unlink()
    roadmap.symlink_to(secret_target)
    with pytest.raises(SyncError):
        sync_mod.sync(ws)
    assert secret_target.read_text() == "outside content\n"


def test_a_hard_linked_note_is_rejected(ws: Any, tmp_path: Path) -> None:
    sync_mod.sync(ws)
    twin = tmp_path / "twin.md"
    os.link(ws.destination / "Roadmap.md", twin)
    with pytest.raises(SyncError):
        sync_mod.sync(ws)


def test_atomic_write_refuses_a_note_edited_meanwhile_and_leaves_no_temp_file(
    ws: Any,
) -> None:
    ws.destination.mkdir()
    note = ws.destination / "Dashboard.md"
    note.write_text("changed by the owner meanwhile\n")
    with pytest.raises(SyncError, match="changed during sync"):
        sync_mod.atomic_write(note, None, b"generated\n")
    assert note.read_text() == "changed by the owner meanwhile\n"
    assert [p.name for p in ws.destination.iterdir()] == ["Dashboard.md"]


# ------------------------------------------------------ verification report


def head_of(repo: Path) -> str:
    out = subprocess.run(
        [GIT, "rev-parse", "HEAD"], cwd=repo, capture_output=True, check=True
    )
    return out.stdout.decode().strip()


def test_verification_report_is_shown_only_when_it_matches_head(
    ws: Any, tmp_path: Path
) -> None:
    report = tmp_path / "report.json"
    good = {
        "head": head_of(ws.repo),
        "recorded_at": "2026-09-21T10:00:00+00:00",
        "checks": [
            {"name": "python_tests", "status": "passed", "count": 42},
            {"name": "ruff", "status": "passed", "count": 0},
        ],
    }
    report.write_text(json.dumps(good))
    sync_mod.sync(ws, verification_path=report)
    dashboard = (ws.destination / "Dashboard.md").read_text()
    assert "python_tests: passed (42)" in dashboard and "ruff: passed" in dashboard

    mutations: list[Callable[[dict[str, Any]], object]] = [
        lambda d: d.update(head="0" * 40),
        lambda d: d["checks"].append(
            {"name": "made_up", "status": "passed", "count": 1}
        ),
        lambda d: d["checks"].append({"name": "mypy", "status": "failed", "count": 9}),
        lambda d: d.update(recorded_at="2026-09-21T10:00:00"),  # no timezone
    ]
    for mutate in mutations:
        bad = copy.deepcopy(good)
        mutate(bad)
        report.write_text(json.dumps(bad))
        with pytest.raises(SyncError):
            sync_mod.sync(ws, dry_run=True, verification_path=report)


# ------------------------------------------------------------------- the CLI


def test_cli_dry_run_prints_names_and_counts_but_no_note_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    workspace = make_workspace(repo)
    monkeypatch.setattr(sync_mod, "REPOSITORY", repo)
    monkeypatch.setattr(sync_mod, "REGISTRY", tmp_path / "none.json")
    before = snapshot(workspace.vault)
    assert sync_mod.main(["--vault", str(workspace.vault), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would create: Sam/Dashboard.md" in out and "dry run: nothing written" in out
    for content in ("Status:", "least_privilege", "Repository:", "email access"):
        assert content not in out
    assert snapshot(workspace.vault) == before

    assert sync_mod.main(["--vault", str(workspace.vault)]) == 0
    assert "created: Sam/Dashboard.md" in capsys.readouterr().out
    assert sync_mod.main(["--vault", str(workspace.vault)]) == 0
    assert "0 created, 0 updated" in capsys.readouterr().out


def test_cli_refusal_exits_nonzero_without_echoing_the_rejected_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = copy.deepcopy(BASE_SPEC)
    spec["phases"][2]["scope"] = ["api_key = " + "sk-" + "ant-abcdefghijklmnopqrstuvwx"]
    repo = make_repo(tmp_path, spec)
    workspace = make_workspace(repo)
    monkeypatch.setattr(sync_mod, "REPOSITORY", repo)
    monkeypatch.setattr(sync_mod, "REGISTRY", tmp_path / "none.json")
    assert sync_mod.main(["--vault", str(workspace.vault)]) == 2
    captured = capsys.readouterr()
    assert "sk-ant" not in captured.out + captured.err
    assert "refused" in captured.err
    assert not workspace.destination.exists()


def test_cli_rejects_a_vault_that_is_not_an_obsidian_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    plain = tmp_path / "plain-folder"
    plain.mkdir()
    monkeypatch.setattr(sync_mod, "REPOSITORY", repo)
    monkeypatch.setattr(sync_mod, "REGISTRY", tmp_path / "none.json")
    assert sync_mod.main(["--vault", str(plain)]) == 2
    assert list(plain.iterdir()) == []
    capsys.readouterr()


def test_the_real_vault_is_never_used_by_these_tests(tmp_path: Path) -> None:
    real = ROOT / "Obsidian"
    assert real.resolve() not in tmp_path.resolve().parents
    assert tmp_path.resolve() != real.resolve()
