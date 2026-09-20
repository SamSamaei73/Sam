"""Explicit, offline, local-only Obsidian mirror. See docs/obsidian.md.

Run by hand, once, and it exits. There is no daemon, watcher, scheduler,
network access or Git write. It renders a few project-state notes from the
validated ``PROJECT_SPEC.json`` and read-only Git facts, validates ALL of them
(secrets, size, structure, paths), and only then writes. Sam-managed text lives
between ``<!-- SAM:AUTO:START/END -->`` markers; everything outside them is the
owner's and is never touched. Tests inject temporary repositories and vaults and
must never use the real vault.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import selectors
import stat
import subprocess
import sys
import time
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "src"))
from sam.memory.sanitization import looks_like_secret  # noqa: E402

REGISTRY = Path.home() / "Library/Application Support/obsidian/obsidian.json"
START = "<!-- SAM:AUTO:START -->"
END = "<!-- SAM:AUTO:END -->"
FRONTMATTER = "---\nsam_managed: true\n---\n"
MAX_INPUT = 262_144
MAX_GENERATED = 32_768
MAX_NOTE = 262_144
MAX_TOTAL = 4_194_304
MAX_NOTES = 128
STATUSES = {
    "planned",
    "pending",
    "not_started",
    "in_progress",
    "blocked",
    "approved",
    "completed",
    "implementation_completed_ready_for_review",
    "ready_for_review",
}
CHECKS = {
    "python_tests",
    "obsidian_tests",
    "ruff",
    "mypy",
    "git_diff",
    "uv_lock",
    "dependency_audit",
    "temporary_vault",
    "desktop_frontend",
    "desktop_rust",
}
EXTRA_SECRETS = re.compile(
    r"(?ix)(?:\b(?:oauth[_ -]?secret|client[_ -]?secret|refresh[_ -]?token|"
    r"id[_ -]?token|desktop[_ -]?(?:bridge[_ -]?token|step[_ -]?up[_ -]?secret)|"
    r"confirmation[_ -]?(?:token|secret)|authorization|private[_ -]?key|"
    r"biometric[_ -]?embeddings?|speaker[_ -]?embeddings?|voice[_ -]?templates?|"
    r"owner[_ -]?proof|raw[_ -]?recordings?|"
    r"student[_ -]?id|government[_ -]?id|phone(?:[_ -]?number)?)"
    r"\b[\s\"\x27]*(?:is|[:=])[\s\"\x27]*\S+|"
    r"\bAIza[A-Za-z0-9_-]{30,}|\bya29\.[A-Za-z0-9_-]{15,}|"
    r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bxox[abprs]-[A-Za-z0-9-]{10,}|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bBasic\s+[A-Za-z0-9+/=]{12,})"
)
# A secret-ish NAME followed by a long token-like VALUE (prose is unaffected).
GENERIC_SECRET = re.compile(
    r"(?i)\b[\w-]*(?:token|secret|passw(?:or)?d|api[_ -]?key|credential)s?\b"
    r"\s*[:=]\s*[\"\x27]?[A-Za-z0-9_+/=.-]{16,}"
)
# A run of many decimals looks like an embedding/template vector.
EMBEDDING = re.compile(r"(?:-?\d+\.\d+\s*[,;\s]\s*){7,}-?\d+\.\d+")
# Recording and audio files are never mirrored, not even their names.
AUDIO_PATH = re.compile(
    r"(?i)[\w./~-]+\.(?:wav|m4a|mp3|aiff?|caf|ogg|opus|flac|webm|amr)\b"
)


class SyncError(Exception):
    """Fixed, content-free error messages: never echo rejected material."""


def check_content(text: str, limit: int = MAX_GENERATED) -> None:
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        raise SyncError("Invalid UTF-8 content.") from None
    if len(raw) > limit:
        raise SyncError("Content exceeds its size bound.")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
        raise SyncError("Control characters are not allowed.")
    # Scan exact content AND a normalized view. Never store a redacted variant.
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(c for c in normalized if unicodedata.category(c) != "Cf")
    normalized = re.sub(r"<!--.*?-->", "", normalized, flags=re.S)
    for view in (text, normalized):
        if (
            looks_like_secret(view)
            or EXTRA_SECRETS.search(view)
            or GENERIC_SECRET.search(view)
            or EMBEDDING.search(view)
            or AUDIO_PATH.search(view)
        ):
            raise SyncError("Secret-looking or restricted personal content rejected.")


def scalar(value: Any, limit: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise SyncError("Malformed bounded metadata text.")
    check_content(value, limit * 4)
    if any(c in value for c in "\r\n\t<>[]`"):
        raise SyncError("Metadata must be plain single-line text.")
    return value


def string_list(value: Any, limit: int = 64) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise SyncError("Malformed metadata list.")
    return [scalar(item) for item in value]


def relative_note(value: str) -> PurePosixPath:
    p = PurePosixPath(value)
    if (
        not value
        or p.is_absolute()
        or str(p) != value
        or "\\" in value
        or any(part in (".", "..") or part.startswith(".") for part in p.parts)
        or p.suffix != ".md"
        or len(p.parts) > 2
        or len(value) > 200
        or not re.fullmatch(r"[A-Za-z0-9 /,._-]+", value)
    ):
        raise SyncError("Unsafe note path rejected.")
    return p


@contextmanager
def directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Walk absolute components with directory FDs; never follow a symlink."""
    if not path.is_absolute() or ".." in path.parts:
        raise SyncError("Unsafe directory path.")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=fd)
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_bytes(path: Path, limit: int = MAX_INPUT) -> bytes | None:
    try:
        with directory(path.parent) as parent:
            fd = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SyncError("Non-regular or hard-linked file rejected.")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read(limit + 1)
                if len(raw) > limit:
                    raise SyncError("Input exceeds its size bound.")
                return raw
            finally:
                os.close(fd)
    except FileNotFoundError:
        return None
    except OSError:  # symlink, permission, not-a-directory, ...
        raise SyncError("Unsafe or unreadable path rejected.") from None


def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeError:
        raise SyncError("Input is not UTF-8.") from None


def read_json(path: Path) -> dict[str, Any]:
    raw = read_bytes(path)
    if raw is None:
        raise SyncError("Required metadata file is missing.")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise SyncError("Duplicate metadata keys rejected.")
            result[key] = value
        return result

    try:
        data = json.loads(decode(raw), object_pairs_hook=pairs)
    except (ValueError, RecursionError):
        raise SyncError("Malformed JSON metadata.") from None
    if not isinstance(data, dict):
        raise SyncError("Metadata must be a JSON object.")
    return data


@dataclass(frozen=True)
class Workspace:
    repo: Path
    vault: Path
    registry: Path | None = None

    @property
    def destination(self) -> Path:
        return self.vault / "Sam"

    def validate_vault(self) -> str:
        try:
            with directory(self.vault):
                pass
            marker = self.vault / ".obsidian"
            # A present but unsafe marker must not fall through to the registry.
            if marker.exists() or marker.is_symlink():
                with directory(marker):
                    return ".obsidian directory"
        except OSError:
            raise SyncError("Vault path is missing, a symlink or unsafe.") from None
        if self.registry is not None:
            registry = read_json(self.registry)
            vaults = registry.get("vaults")
            if not isinstance(vaults, dict):
                raise SyncError("Malformed Obsidian registry.")
            for entry in vaults.values():
                if (
                    isinstance(entry, dict)
                    and entry.get("path") == str(self.vault)
                    and entry.get("open") is True
                    and str(self.vault.resolve()) == str(self.vault)
                ):
                    return "exact open Obsidian registry entry"
        raise SyncError("Vault lacks a marker and an exact open registry entry.")


def resolve_vault(argument: str | None, repo: Path) -> Path:
    """The vault is the ``--vault`` path, else the repository's ignored
    ``Obsidian/`` directory. A vault inside the repository must be exactly that
    ignored directory, so nothing else in the repository can become a vault."""

    if argument is None:
        return repo / "Obsidian"
    if "\0" in argument or not argument.strip():
        raise SyncError("Invalid vault path.")
    vault = Path(os.path.abspath(os.path.expanduser(argument)))
    if vault == repo or vault.is_relative_to(repo):
        if vault != repo / "Obsidian":
            raise SyncError("A vault inside the repository must be Obsidian/.")
    return vault


def git(repo: Path, *args: str) -> bytes:
    """Read-only Git calls with bounded output/time, no shell or ambient hooks."""
    command = [
        "/usr/bin/git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.pager=cat",
        *args,
    ]
    env = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    with subprocess.Popen(
        command,
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    ) as process:
        assert process.stdout is not None
        deadline = time.monotonic() + 15
        output = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise SyncError("Git read timed out.")
                    chunk = os.read(process.stdout.fileno(), 65_536)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > MAX_INPUT:
                        raise SyncError("Git output exceeds its size bound.")
            if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                raise SyncError("Git safety or metadata check failed.")
        except BaseException:
            process.kill()
            process.wait()
            raise
    return bytes(output)


def validate_git(workspace: Workspace, notes: list[str] | None = None) -> None:
    if decode(git(workspace.repo, "rev-parse", "--show-toplevel")).strip() != str(
        workspace.repo
    ):
        raise SyncError("Repository root does not match the workspace.")
    if git(workspace.repo, "ls-files", "-z", "--", "Obsidian"):
        raise SyncError("Tracked Obsidian paths exist. STOP; inspect git ls-files.")
    staged = git(
        workspace.repo,
        "diff",
        "--cached",
        "--name-only",
        "-z",
        "--no-ext-diff",
        "--no-textconv",
    )
    if any(p == b"Obsidian" or p.startswith(b"Obsidian/") for p in staged.split(b"\0")):
        raise SyncError("Staged Obsidian paths exist. Do not commit or push.")
    ignore = read_bytes(workspace.repo / ".gitignore")
    if ignore is None or "Obsidian/" not in decode(ignore).splitlines():
        raise SyncError("The exact Obsidian/ repository ignore rule is required.")
    candidates = ["Obsidian", "Obsidian/Sam/future-note.md"]
    candidates.extend("Obsidian/Sam/" + n for n in (notes or []))
    output = decode(
        git(workspace.repo, "check-ignore", "--no-index", "-v", "--", *candidates)
    ).splitlines()
    if len(output) != len(candidates) or any(
        not re.fullmatch(r"\.gitignore:\d+:Obsidian/\t" + re.escape(p), row)
        for p, row in zip(candidates, output, strict=True)
    ):
        raise SyncError("Obsidian ignore behavior is unsafe or overridden.")


PHASE_NAMES = {
    "foundation": "Foundation",
    "agent_core": "Agent Core",
    "permission_engine": "Permission Engine",
    "memory": "Memory",
    "computer_control": "Computer Control",
    "coding_agent": "Coding Agent",
    "knowledge_layer": "Knowledge",
    "MCP_integrations": "MCP Gateway",
    "voice": "Voice",
    "fish_audio": "TTS",
    "desktop_ui": "Desktop UI",
    "owner_voice_identity_persian_guest_mode": "Owner Voice, Persian, Guest Mode",
    "multi_model_router": "Multi-Model Router",
    "multi_model_router_personal_content_privacy_policy": (
        "Multi-Model Router and Content Policy"
    ),
    "professional_intelligence": "Professional Intelligence",
    "proactive_agent": "Proactive Agent",
    "career_and_phd_agent": "Career and PhD Agent",
    "production_integration_hardening": "Production Integration and Hardening",
}
# Repository documents a phase links to (paths only; content is never copied).
PHASE_DOCS = {
    1: ("README.md",),
    2: ("README.md",),
    3: ("docs/permissions.md",),
    4: ("docs/memory.md",),
    5: ("docs/computer-control.md",),
    6: ("docs/coding-agent.md",),
    7: ("docs/knowledge.md",),
    8: ("docs/mcp.md",),
    9: ("docs/voice.md",),
    10: ("docs/tts.md",),
    11: ("docs/desktop.md",),
    12: ("docs/voice_identity.md", "docs/persian.md"),
}


def load_spec(workspace: Workspace) -> dict[str, Any]:
    data = read_json(workspace.repo / "PROJECT_SPEC.json")
    phases, current = data.get("phases"), data.get("current_state")
    if not isinstance(phases, list) or not 1 <= len(phases) <= 32:
        raise SyncError("Malformed project phases.")
    if not isinstance(current, dict) or type(current.get("phase")) is not int:
        raise SyncError("Malformed current phase.")
    ids: set[int] = set()
    for phase in phases:
        if not isinstance(phase, dict):
            raise SyncError("Malformed phase metadata.")
        number = phase.get("id")
        if type(number) is not int or not 1 <= number <= 99 or number in ids:
            raise SyncError("Invalid or duplicate phase number.")
        ids.add(number)
        name = scalar(phase.get("name"), 80)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            raise SyncError("Unsafe phase name.")
        if phase.get("status", "planned") not in STATUSES:
            raise SyncError("Unknown phase status.")
        for field in ("scope", "must_not_include", "integrations"):
            values = phase.get(field, [])
            if not isinstance(values, list) or len(values) > 32:
                raise SyncError("Malformed phase scope.")
            for value in values:
                scalar(value)
        for field in ("scope_note", "rule", "security_rule"):
            if field in phase:
                scalar(phase[field], 2400)
    if current.get("status") not in STATUSES or current["phase"] not in ids:
        raise SyncError("Current state does not reference a valid phase.")
    phase = next(p for p in phases if p["id"] == current["phase"])
    if phase.get("status", "planned") != current["status"]:
        raise SyncError("Conflicting current phase statuses.")
    if phases != sorted(phases, key=lambda p: p["id"]):
        raise SyncError("Phase order is not deterministic.")
    return data


def phase_title(phase: dict[str, Any]) -> str:
    name = PHASE_NAMES.get(phase["name"], phase["name"].replace("_", " ").title())
    return f"Phase {phase['id']:02d} - {name}"


def timestamp(value: Any) -> datetime:
    scalar(value, 40)
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise SyncError("Invalid timestamp.") from None
    if result.tzinfo is None:
        raise SyncError("Timestamp must include a timezone.")
    return result


def verification(path: Path | None, head: str) -> dict[str, Any] | None:
    if path is None:
        return None
    data = read_json(path)
    if set(data) != {"head", "recorded_at", "checks"} or data["head"] != head:
        raise SyncError("Verification must match HEAD and the report schema.")
    timestamp(data["recorded_at"])
    checks = data["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= len(CHECKS):
        raise SyncError("Invalid verification checks.")
    names: set[str] = set()
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"name", "status", "count"}:
            raise SyncError("Invalid check schema.")
        if check["name"] not in CHECKS or check["name"] in names:
            raise SyncError("Unknown or duplicate verification check.")
        names.add(check["name"])
        if check["status"] not in {"passed", "failed", "not_run"}:
            raise SyncError("Invalid check result.")
        if type(check["count"]) is not int or not 0 <= check["count"] <= 1_000_000:
            raise SyncError("Invalid check count.")
        if check["status"] != "passed" and check["count"]:
            raise SyncError("A failed or unrun check cannot claim passed tests.")
    return data


def verification_text(report: dict[str, Any] | None) -> str:
    if report is None:
        return "Not recorded. No verification report was supplied to the sync."
    checks = "; ".join(
        f"{c['name']}: {c['status']}" + (f" ({c['count']})" if c["count"] else "")
        for c in report["checks"]
    )
    return (
        f"Operator-recorded at {report['recorded_at']}: {checks}. "
        "Results describe that working-tree snapshot; sync does not run tests "
        "or approve a phase."
    )


def managed(text: str) -> tuple[str, str, str] | None:
    if START not in text and END not in text:
        return None
    if text.count(START) != 1 or text.count(END) != 1:
        raise SyncError("Malformed or multiple managed sections.")
    before, rest = text.split(START)
    if END in before:
        raise SyncError("Reversed managed markers.")
    middle, after = rest.split(END)
    return before, middle, after


def merge(old: bytes | None, content: str, frontmatter: str = "") -> bytes:
    check_content(content)
    if START in content or END in content or "```" in content:
        raise SyncError("Generated marker injection or code dump rejected.")
    if old is None:
        result = frontmatter + START + "\n" + content.rstrip() + "\n" + END + "\n"
    else:
        original = decode(old)
        parts = managed(original)
        if parts is None:
            raise SyncError("Existing user note has no managed markers; left intact.")
        before, _, after = parts
        result = before + START + "\n" + content.rstrip() + "\n" + END + after
    # Also scan preserved user text and boundaries. Unsafe notes remain untouched.
    check_content(result, MAX_NOTE)
    return result.encode("utf-8")


def atomic_write(path: Path, previous: bytes | None, content: bytes) -> None:
    """Atomic per note, private temp file, optimistic edit detection, no deletion."""
    with directory(path.parent, create=True) as parent:
        temp = ".sam-sync-" + secrets.token_hex(12) + ".tmp"
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if read_bytes(path, MAX_NOTE) != previous:
                raise SyncError("Note changed during sync; refusing to overwrite it.")
            # Rewalk from / to detect replaced/renamed/symlinked parent directories.
            with directory(path.parent) as current:
                a, b = os.fstat(parent), os.fstat(current)
                if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                    raise SyncError("Destination directory changed during sync.")
            os.replace(temp, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            # Remove ONLY this invocation's unpublished temporary file, never a note.
            try:
                os.unlink(temp, dir_fd=parent)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- rendering
# Notes summarize validated PROJECT_SPEC.json metadata and read-only Git facts.
# They never read application source, configuration, credentials, Memory,
# Knowledge, transcripts or audio, and they invent nothing: a section with no
# trusted source (ADRs, Professional, Career, Daily, Activity) is not generated.


def bullets(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- none recorded"


def repo_state(workspace: Workspace) -> tuple[str, str]:
    head = decode(git(workspace.repo, "rev-parse", "HEAD")).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise SyncError("Unexpected Git HEAD.")
    branch = decode(git(workspace.repo, "rev-parse", "--abbrev-ref", "HEAD")).strip()
    if branch == "HEAD":
        branch = "detached"
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,80}", branch):
        raise SyncError("Unsafe branch name.")
    check_content(branch, 80)
    return head, branch


def render_dashboard(
    spec: dict[str, Any], head: str, branch: str, report: dict[str, Any] | None
) -> str:
    project = spec.get("project", {})
    name = scalar(project.get("name", "Sam"), 80)
    version = scalar(project.get("version", "unknown"), 40)
    current = spec["current_state"]
    phase = next(p for p in spec["phases"] if p["id"] == current["phase"])
    approved = sum(1 for p in spec["phases"] if p.get("status") == "approved")
    return "\n".join(
        [
            f"# {name} - Dashboard",
            "",
            f"- Version: {version}",
            f"- Current phase: [[{phase_title(phase)}]] ({current['status']})",
            f"- Phases approved: {approved} of {len(spec['phases'])}",
            f"- Repository: {head[:12]} on {branch}",
            f"- Verification: {verification_text(report)}",
            "",
            "## Navigate",
            "",
            "- [[Roadmap]]",
            "- [[Security Principles]]",
            "- [[Engineering Overview]]",
            "- [[MCP Integrations]]",
            "",
            "Generated by scripts/obsidian_sync.py from PROJECT_SPEC.json and Git.",
            "Write your own notes outside the managed markers.",
        ]
    )


def render_roadmap(spec: dict[str, Any]) -> str:
    rows = [
        f"| {p['id']} | [[{phase_title(p)}]] | {p.get('status', 'planned')} |"
        for p in spec["phases"]
    ]
    return "\n".join(
        ["# Roadmap", "", "| Phase | Name | Status |", "|---|---|---|", *rows]
    )


def render_phase(workspace: Workspace, phase: dict[str, Any]) -> str:
    docs = [
        d for d in PHASE_DOCS.get(phase["id"], ()) if (workspace.repo / d).is_file()
    ]
    out = [f"# {phase_title(phase)}", "", f"Status: {phase.get('status', 'planned')}"]
    for field, heading in (
        ("scope", "Scope"),
        ("must_not_include", "Must not include"),
        ("integrations", "Integrations"),
    ):
        if field in phase:
            out += ["", f"## {heading}", "", bullets(phase[field])]
    for field, heading in (
        ("scope_note", "Notes"),
        ("rule", "Rule"),
        ("security_rule", "Security rule"),
    ):
        if field in phase:
            out += ["", f"## {heading}", "", phase[field]]
    if docs:
        out += ["", "## Repository documents", "", bullets(docs)]
    out += ["", "Back to [[Roadmap]]."]
    return "\n".join(out)


def render_security(spec: dict[str, Any]) -> str | None:
    security = spec.get("security")
    if not isinstance(security, dict):
        return None
    out = ["# Security Principles", ""]
    out += ["## Principles", "", bullets(string_list(security.get("principles", [])))]
    out += [
        "",
        "## Never allowed by default",
        "",
        bullets(string_list(security.get("never_allow_by_default", []))),
    ]
    risks = spec.get("risk_levels")
    if isinstance(risks, dict):
        rows = []
        for level in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
            entry = risks.get(level)
            if isinstance(entry, dict):
                confirm = "yes" if entry.get("default_confirmation") else "no"
                rows.append(
                    f"| {level} | {scalar(entry.get('description'))} | {confirm} |"
                )
        if rows:
            out += [
                "",
                "## Risk levels",
                "",
                "| Level | Description | Confirmation by default |",
                "|---|---|---|",
                *rows,
            ]
    return "\n".join(out)


def render_engineering(spec: dict[str, Any]) -> str | None:
    out = ["# Engineering Overview"]
    architecture = spec.get("architecture")
    if isinstance(architecture, dict):
        out += ["", "## Architecture layers", ""]
        out += [bullets(string_list(architecture.get("layers", [])))]
        out += [
            "",
            "## Request flow",
            "",
            bullets(string_list(architecture.get("flow", []))),
        ]
    rules = spec.get("development_rules")
    if isinstance(rules, dict):
        for key, heading in (
            ("before_each_phase", "Before each phase"),
            ("after_each_phase", "After each phase"),
        ):
            out += ["", f"## {heading}", "", bullets(string_list(rules.get(key, [])))]
        if "important" in rules:
            out += ["", f"Important: {scalar(rules['important'])}"]
    technology = spec.get("technology")
    if isinstance(technology, dict):
        facts = []
        for area, values in sorted(technology.items()):
            if isinstance(values, dict):
                for key, value in sorted(values.items()):
                    if isinstance(value, str):
                        facts.append(
                            f"{scalar(area, 40)} {scalar(key, 40)}: {scalar(value)}"
                        )
        if facts:
            out += ["", "## Technology", "", bullets(facts)]
    return "\n".join(out) if len(out) > 1 else None


def render_integrations(spec: dict[str, Any]) -> str | None:
    integrations = spec.get("mcp_integrations")
    if not isinstance(integrations, dict) or not integrations:
        return None
    out = ["# MCP Integrations", ""]
    for name in sorted(integrations):
        entry = integrations[name]
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", name) or not isinstance(
            entry, dict
        ):
            raise SyncError("Malformed integration metadata.")
        out += [f"## {name}", ""]
        facts = []
        for key in sorted(entry):
            value = entry[key]
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", key):
                raise SyncError("Malformed integration metadata.")
            if isinstance(value, str):
                facts.append(f"{key}: {scalar(value)}")
            elif isinstance(value, list):
                facts.append(f"{key}: {', '.join(string_list(value))}")
        out += [bullets(facts), ""]
    return "\n".join(out).rstrip()


def build_notes(
    workspace: Workspace,
    spec: dict[str, Any],
    head: str,
    branch: str,
    report: dict[str, Any] | None,
) -> dict[str, str]:
    notes: dict[str, str] = {
        "Dashboard.md": render_dashboard(spec, head, branch, report),
        "Roadmap.md": render_roadmap(spec),
    }
    for phase in spec["phases"]:
        notes[f"Phases/{phase_title(phase)}.md"] = render_phase(workspace, phase)
    for rel, body in (
        ("Security/Security Principles.md", render_security(spec)),
        ("Engineering/Engineering Overview.md", render_engineering(spec)),
        ("Integrations/MCP Integrations.md", render_integrations(spec)),
    ):
        if body is not None:
            notes[rel] = body
    for rel in notes:
        relative_note(rel)
    return notes


# --------------------------------------------------------------- plan / apply


@dataclass(frozen=True)
class Change:
    rel: str
    status: str  # created | updated | unchanged
    previous: bytes | None
    data: bytes


def plan(workspace: Workspace, notes: dict[str, str]) -> list[Change]:
    """Render and validate EVERY note before anything is written."""
    if not 1 <= len(notes) <= MAX_NOTES:
        raise SyncError("Unexpected number of notes.")
    changes: list[Change] = []
    total = 0
    for rel, body in notes.items():
        path = workspace.destination / relative_note(rel)
        previous = read_bytes(path, MAX_NOTE)
        try:
            data = merge(previous, body, FRONTMATTER)
        except SyncError as error:
            raise SyncError(f"{error} ({rel})") from None
        total += len(data)
        if total > MAX_TOTAL:
            raise SyncError("Total generated content exceeds its bound.")
        if previous is None:
            status = "created"
        elif previous == data:
            status = "unchanged"
        else:
            status = "updated"
        changes.append(Change(rel, status, previous, data))
    return changes


def sync(
    workspace: Workspace,
    *,
    dry_run: bool = False,
    verification_path: Path | None = None,
) -> list[Change]:
    workspace.validate_vault()
    validate_git(workspace)
    spec = load_spec(workspace)
    head, branch = repo_state(workspace)
    report = verification(verification_path, head)
    notes = build_notes(workspace, spec, head, branch, report)
    validate_git(workspace, sorted(notes))
    changes = plan(workspace, notes)
    if not dry_run:
        for change in changes:
            if change.status != "unchanged":
                atomic_write(
                    workspace.destination / relative_note(change.rel),
                    change.previous,
                    change.data,
                )
    return changes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian_sync",
        description="One-shot, offline, local-only mirror of Sam project state "
        "into an Obsidian vault. Never runs in the background.",
    )
    parser.add_argument(
        "--vault",
        metavar="PATH",
        help="Obsidian vault directory (default: the repository's ignored "
        "Obsidian/). Must contain .obsidian or be an open vault in Obsidian.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render and validate everything, report changes, write nothing.",
    )
    parser.add_argument(
        "--verification",
        metavar="FILE",
        help="Optional operator-written verification report (JSON).",
    )
    args = parser.parse_args(argv)
    try:
        workspace = Workspace(
            REPOSITORY, resolve_vault(args.vault, REPOSITORY), REGISTRY
        )
        report_path = (
            Path(os.path.abspath(os.path.expanduser(args.verification)))
            if args.verification
            else None
        )
        changes = sync(workspace, dry_run=args.dry_run, verification_path=report_path)
    except SyncError as error:
        print(f"obsidian_sync: refused: {error}", file=sys.stderr)
        return 2
    except OSError:
        print(
            "obsidian_sync: refused: filesystem safety check failed.", file=sys.stderr
        )
        return 2
    words = {"created": "create", "updated": "update", "unchanged": "unchanged"}
    for change in changes:
        label = words[change.status]
        if args.dry_run and change.status != "unchanged":
            label = f"would {label}"
        elif change.status != "unchanged":
            label = change.status
        print(f"{label}: Sam/{change.rel}")
    counts = {s: sum(c.status == s for c in changes) for s in words}
    suffix = " (dry run: nothing written)" if args.dry_run else ""
    print(
        f"{len(changes)} notes: {counts['created']} created, "
        f"{counts['updated']} updated, {counts['unchanged']} unchanged{suffix}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
