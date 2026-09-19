# Sam Coding Agent (Phase 6)

> **Phase 6 is a controlled Coding Agent foundation, not an unrestricted
> terminal agent or autonomous software engineer.**

## Purpose

The Coding Agent lets Sam inspect a repository, propose and make bounded
file changes with optimistic concurrency, and run a closed set of
allowlisted verification commands (tests, lint, typecheck, `git diff
--check`) — never an unrestricted shell. Every operation passes through
the existing Phase 3 Permission Engine before anything touches the
filesystem or spawns a process.

```
User / AgentCore
   │
   ▼
CodingTask
   │
   ▼
CodingPlanner            (bounds a caller-supplied step list — never generates one)
   │
   ▼
CodingOperationRequest   (one of eleven closed, typed operations)
   │
   ▼
sam.coding.policy.build_request()   (pure mapping — no decision)
   │
   ▼
sam.permissions.engine.PermissionEngine.evaluate()   (the only authority)
   │
   ├─ DENY ─────────────────────────► stop, backend never called
   ├─ CONFIRM_REQUIRED ───────────────► stop, backend never called
   └─ ALLOW
        │
        ▼
   CodingExecutor._dispatch()
        │
        ▼
   RepositoryBackend  (path containment + bounded command execution)
        │
        ▼
   Structured CodingOperationResult
```

## Package layout

```
src/sam/coding/
    models.py         domain models: task, repository context, file
                       change, results, plan, audit event, the closed
                       CodingOperation enum
    operations.py       one typed request model per CodingOperation
    policy.py            CodingOperation → PermissionRequest mapping,
                          risk lookup, and secret-protection helpers
    repository.py         path containment + RepositoryBackend + the
                          only place any subprocess is ever started
    executor.py            CodingExecutor: the sole gateway to both the
                          Permission Engine and RepositoryBackend
    planner.py             CodingPlanner (bounding, never generating)
                          and the CodingProvider abstraction
    verifier.py             verification-status interpretation and the
                          bounded verification pipeline
    audit.py                CodingAuditSink protocol + in-memory impl
    errors.py                typed domain errors
```

## Capabilities

**Implemented** — exactly these eleven operations, and no others:

```
INSPECT_REPOSITORY, READ_FILE, LIST_FILES, GET_GIT_STATUS, GET_GIT_DIFF
CREATE_FILE, MODIFY_FILE
RUN_TESTS, RUN_LINT, RUN_TYPECHECK, VERIFY_DIFF
```

**Intentionally unavailable** — none of these exist anywhere in this
package, not merely "unused": `ARBITRARY_COMMAND`, `SHELL`,
`EXECUTE_SCRIPT`, `DELETE_FILE`, `GIT_PUSH`, `GIT_FORCE_PUSH`,
`GIT_RESET`, `GIT_CLEAN`, `GIT_RESTORE_ALL`, `GIT_COMMIT`, `GIT_REBASE`,
`GIT_MERGE`, `GIT_BRANCH_DELETE`, `INSTALL_PACKAGE`,
`CHANGE_SYSTEM_CONFIG`. `ComputerBackend`-style `run`/`shell`/`execute`/
`eval` methods do not exist on `RepositoryBackend` or `CommandRunner`.

## Claude Code / Codex Integration

**No real external provider integration was implemented.** A narrow
`CodingProvider` Protocol (`propose(task) -> CodingProposal`) and one
deterministic `FakeCodingProvider` exist in `sam.coding.planner` — that
is the entire provider surface in Phase 6. There is no
`ClaudeCodeAdapter` and no `CodexAdapter`.

This is a deliberate choice, not an oversight, explicitly sanctioned by
the Phase 6 task: *"If a safe real provider integration cannot be
implemented without introducing an unrestricted command runner,
implement the provider Protocol and deterministic fake provider
instead."* Building a genuinely safe adapter that shells out to an
external CLI tool — bounded, allowlisted, environment-isolated,
timeout-bounded, output-bounded, structurally unable to commit or push —
is real, separate scope that deserves its own dedicated review, the same
reasoning Phase 5 applied to deferring a real OS backend. A
`CodingProvider` returns a `CodingProposal` (data: a summary and a list
of `PlanStep`s) — never a direct repository mutation, never a raw
command string — so a future real adapter only has to implement that one
method; `CodingPlanner.build_plan_from_proposal` applies the exact same
bounds to its output as to any hand-written step list, and every
resulting operation still passes through the full Permission Engine
path regardless of which provider proposed it.

## Repository Safety

### Path containment

`sam.coding.repository.resolve_within_repository` is the single function
every file operation resolves its target through. It uses **real path
resolution** (`Path.resolve()`, which follows symlinks) plus
`Path.relative_to()` containment checking — never string-prefix
matching. Rejected: a null byte, an absolute path, an over-length path
(>1000 chars), excessive directory depth (>12), a path that resolves
outside the repository root after normalization, and (for file targets)
a path that resolves to the root itself. A symlink whose real target
lands outside the root is rejected the same as a literal `..` escape,
because `.resolve()` follows it before the containment check runs.
Path-bearing operation fields (`ReadFileOperation.path`,
`ListFilesOperation.path`, `FileChange.path`) additionally reject (never
silently strip) any control character at construction time — silently
removing a character from a path could redirect it to a different, real
file, which is worse than failing loudly.

An optional `RepositoryContext.allowed_paths` adds a second, coarser
structural layer (which top-level areas are even conceptually in scope)
on top of per-request Permission Engine scoping — not a replacement for
it.

### User-change protection

Before any write, `RepositoryBackend`:

1. Requires an explicit `FileChange` with `path`, `operation`
   (`CREATE`/`MODIFY`), `expected_original_hash` (required for `MODIFY`,
   forbidden for `CREATE`), and bounded `new_content`.
2. For `CREATE`: refuses if the target already exists
   (`ConcurrentModificationError`) rather than overwriting it.
3. For `MODIFY`: re-reads the current file, computes its SHA-256, and
   compares against `expected_original_hash` *immediately before
   writing*. A mismatch — the file changed since it was read, by another
   process, another user, or a previous step — raises
   `ConcurrentModificationError` and the write never happens; the
   concurrent content is left exactly as it was.

There is no `git reset --hard`, `git checkout -- .`, `git clean`, or
`git restore` operation anywhere in this package — those commands are
not merely unauthorized, they do not exist as `CodingOperation` values
at all, so there is no automatic-repair code path that could invoke them.

### Write boundaries

Bounded on every write: file size (≤500,000 bytes), content size
(≤500,000 bytes, enforced by `FileChange`'s own `Field`), and no
implicit formatter or secondary file modification — `create_file`/
`modify_file` touch exactly the one path named in the `FileChange`, and
the resulting `FileWriteResult` reports exactly that path and the new
hash, never a broader "files changed" claim.

### Git restrictions

Git support is inspection and local verification only:

| Allowed | Forbidden (do not exist as operations) |
|---|---|
| `git status --porcelain` (`GET_GIT_STATUS`) | `git commit`, `git push`, `git push --force` |
| `git diff` (`GET_GIT_DIFF`) | `git reset`, `git clean`, `git checkout -- .`, `git restore` |
| `git diff --check` (`VERIFY_DIFF`) | `git rebase`, `git merge`, `git branch -D` |

`GET_GIT_STATUS`/`GET_GIT_DIFF` reuse the **pre-existing** Phase 3
`PermissionResource.GIT` (already classified `READ` → LOW) rather than
duplicating a row under the new `CODE` resource — see "Permission
integration" below.

## Command Security

This is the most important boundary in Phase 6.

### Closed operation model, not a command string

There is no `run(command: str)` anywhere. `RUN_TESTS`/`RUN_LINT`/
`RUN_TYPECHECK`/`VERIFY_DIFF`/`GET_GIT_STATUS`/`GET_GIT_DIFF` are bare
marker operations with no argument fields at all — the actual
`ResolvedCommand` (executable name, fixed argument tuple, bounded
timeout) is looked up from a static, code-reviewed registry in
`sam.coding.repository._COMMAND_REGISTRY`, keyed by `CodingOperation`.
No operation model field, LLM output, or caller input ever contributes a
single argument to a real command invocation:

```python
CodingOperation.RUN_TESTS: ResolvedCommand(
    executable_name="uv", arguments=("run", "pytest"), timeout_seconds=180.0
),
```

### Executable resolution

`_resolve_executable` searches only a small, fixed, trusted set of
directories (`sys.executable`'s directory, `/usr/local/bin`,
`/opt/homebrew/bin`, `/usr/bin`, `/bin`, `~/.local/bin`, `~/.cargo/bin`)
— **never** the ambient inherited process `PATH` and **never** the
repository directory or the caller's current working directory. The
resolved absolute path is additionally checked against the repository
root and rejected if it falls inside it. A file named `git`, `uv`, or
`python` placed inside the repository (or prepended to `PATH`) is never
the one resolved or invoked — proven with a real subprocess in
`tests/test_coding_security.py` (tests 11–14).

### Environment isolation

The child process receives an explicit, minimal environment
(`PATH` set to the same trusted search list, `HOME`, and `LANG`/`LC_ALL`
if present) — **never `os.environ` wholesale**. A secret set in this
process's own environment is not inherited by a spawned command, proven
directly in `tests/test_coding_security.py` (test 10).

### No shell, ever

`subprocess.run` is always called with `shell=False` and a fixed
argument list (never a string). There is no shell interpolation, no
pipes, no redirects, no command chaining, and no metacharacter
interpretation — `&&`, `||`, `;`, `<`, `>`, `|`, backticks, and `$()` are
all inert literal characters when they appear anywhere in output or in a
value handed to `python3 -c`, proven directly (tests 8–9).

### Timeout and output bounds

Every `ResolvedCommand.timeout_seconds` is a `FiniteFloat` bounded
`(0, 300]` — NaN, infinity, zero, and negative values are all rejected
at construction, the same discipline Phase 2 uses for `CLAUDE_TIMEOUT`.
A command that exceeds its timeout is killed and reported with
`timed_out=True`, `exit_code=None` — never silently treated as success.
`stdout`/`stderr` are each hard-bounded to 50,000 characters
(`CommandExecutionResult`'s own `Field` constraint — there is no way to
construct, let alone return, an over-length result), with
`stdout_truncated`/`stderr_truncated` flags that are never hidden.

## Permission Integration

Phase 3 is extended with the smallest addition the task asked for: one
new `PermissionResource.CODE`, plus three new policy rows following the
exact same risk convention every other resource already uses:

| `PermissionAction` | Risk | Confirmation | Covers |
|---|---|---|---|
| `READ` | LOW | no | inspect/list/read files, `VERIFY_DIFF` |
| `WRITE` | MEDIUM | no | create/modify files (protected by optimistic concurrency) |
| `EXECUTE` | HIGH | **yes** | run tests/lint/typecheck |

`GET_GIT_STATUS`/`GET_GIT_DIFF` deliberately do **not** use the new
resource — they reuse the pre-existing `PermissionResource.GIT`'s
already-classified `READ` → LOW row, avoiding a duplicate policy entry
for a resource that already covers git inspection.

Scopes always begin with the repository id
(`PermissionScope.from_path(f"{repository_id}/{path}")` for file
operations, `PermissionScope.identifier(f"{repository_id}:{segment}")`
for operation-scoped ones — both reused directly from Phase 3, unmodified)
— so a grant issued for one repository can never be reused against a
different one, and a grant for `sam-core:tests` never authorizes
`sam-core:lint`. `sam.coding.policy.build_request` performs **no
authorization decision itself**; `PermissionEngine.evaluate` is called
from exactly one place, `CodingExecutor._execute_unsafe`, and
`RepositoryBackend` is only ever reached from that call's `ALLOW`
branch.

### Confirmation

Reused directly from Phase 3 (`sam.permissions.confirmation`) — no
parallel confirmation system exists. Because `EXECUTE` is always HIGH
risk, running tests/lint/typecheck always requires a fresh, one-time,
context-bound confirmation regardless of any persistent grant — the same
CRITICAL/HIGH-always-requires-confirmation invariant Phase 3 enforces
independently of grant data. **Git commit/push are unavailable
regardless of permission** — not merely denied by policy, they have no
`CodingOperation` value to request in the first place.

## Verification

**Verification is separate from execution.** A `RUN_TESTS` operation
*executing* successfully (`ExecutionOutcome.SUCCESS`) means the command
ran to completion — it says nothing about whether the tests passed.
`sam.coding.verifier.status_from_command_result` is the one place a raw
`CommandExecutionResult` becomes the closed `VerificationStatus`
vocabulary:

- `exit_code == 0` → `PASSED`
- any other exit code → `FAILED`
- `timed_out` → the operation's own execution is reported `FAILED`
  (`ErrorCategory.COMMAND_TIMEOUT`), not silently treated as a
  verification status at all.

`NOT_RUN` and `BLOCKED` (denied or awaiting confirmation) are never
converted to `PASSED` anywhere — `run_verification_pipeline` runs the
fixed, task-specified sequence (`VERIFY_DIFF`, `RUN_TESTS`, `RUN_LINT`,
`RUN_TYPECHECK`, a caller may select any *subset*, never a step outside
this set) through the full authorization path for each, and aggregates
by priority: `TIMEOUT` > `BLOCKED` > `FAILED` > `NOT_RUN` > `PASSED`.

## Autonomy Limit

**No plan-generation loop, no self-triggering, no background execution
exists anywhere in this package.**

- `CodingPlanner.build_plan` is a pure validation/bounding function over
  an already-finite, caller-supplied step list (≤20 steps, ≤20 affected
  paths by default) — it has exactly two public methods
  (`build_plan`, `build_plan_from_proposal`), proven structurally in
  `tests/test_coding_planner.py`. There is no `has_more_work()` /
  `generate_more()` method to loop on.
- `run_verification_pipeline` iterates a fixed, bounded, pre-given
  sequence of at most 4 canonical steps exactly once.
- `CodingExecutor.execute` performs exactly one operation and returns; it
  never calls itself, never schedules a retry, never triggers another
  operation.

A coding task has: a finite plan, finite operations, finite
verification, a final structured report. Then it stops.

## Secret Protection

Best-effort, documented as non-exhaustive (never claimed perfect):

- **Filename patterns** (`sam.coding.policy.is_restricted_filename`):
  `.env`, `.env.*`, `*.pem`, `*.key`, `credentials.*`, `secret(s).*`,
  SSH private key names (`id_rsa`, `id_ed25519`) — matched against the
  final path segment as a whole, never a substring.
- **Content patterns** (`sam.coding.policy.is_restricted_content`):
  reuses `sam.memory.sanitization.looks_like_secret` directly rather
  than duplicating its pattern list.

When either matches, `CodingExecutor._read_file_with_secret_protection`
returns the read as `SUCCESS` (the file access itself was authorized and
happened) with `FileReadResult.restricted=True` and `content=None` —
**never automatically exposed**, matching the task's required
`RESTRICTED` behavior. Secret-looking content is never censored from
being *typed into* a file (unlike computer-control keyboard input, which
this has no relation to) — Phase 6 only ever *reads* files; this
restriction is exclusively a read-time protection.

Never sent anywhere automatically: `.env`/private-key content is not
included in any `CodingAuditEvent` (that model has no field for file
content at all), is never passed to a `CodingProvider` by any code in
this package, and is never persisted to Memory (`sam.coding` does not
import `sam.memory` — the only cross-package dependency is the narrow,
deliberate reuse of `sam.memory.sanitization.looks_like_secret`
mentioned above).

## Audit

Mirrors every prior phase's audit pattern (`record(event) -> None`, a
failing sink never changes the result, never propagates) with its own
`CodingAuditEvent` type — not a reuse of `sam.permissions.models.AuditEvent`,
`sam.memory.models.MemoryAuditEvent`, or `sam.computer.models.ComputerAuditEvent`,
for the same reason each of those is distinct from the others: none of
those vocabularies fit a coding operation. Recorded: timestamp,
operation id, principal, repository id, operation, risk, permission
outcome, confirmation outcome, execution outcome, error category,
affected paths (bounded), verification status. **Never recorded**: file
content, patch/diff text, command stdout/stderr, or any raw exception
message — confirmed by the model having no field for any of them and by
dedicated tests proving a secret-bearing read/command never appears in
any audit record's serialized form.

## Memory Integration Boundary

**No automatic `Computer action → Memory` path exists** (the Phase 6
task's own phrasing, adapted: no automatic `Coding action → Memory`
path exists either). `sam.coding` does not import `sam.memory.engine`,
`sam.memory.models`, or any Memory storage type. A future phase that
wants Sam to selectively remember a meaningful coding event must route
it through `sam.memory`'s own `MemoryCandidate` → `MemoryPolicy` flow
explicitly, the same as any other candidate — never automatically, and
never bypassing that policy.

## Computer Control Boundary

Phase 5's `sam.computer` is not imported anywhere in `sam.coding`, and
vice versa. The Coding Agent does not, and structurally cannot, use
computer-control mouse/keyboard actions to "simulate a terminal" — its
only path to process execution is `RepositoryBackend`'s own bounded
`CommandRunner`.

## AgentCore Integration Boundary

**`AgentCore` is unchanged in Phase 6.** The intended future integration
point, matching `PROJECT_SPEC.json`'s architecture list, is the same
shape every prior phase has documented and left unwired:

```
AgentCore / future planner
   │
   ▼
CodingTask / CodingOperationRequest proposal
   │
   ▼
CodingExecutor.execute() / CodingPlanner.build_plan()
   │
   ▼
Permission Engine  →  RepositoryBackend
```

## Known Limitations

- No real `CodingProvider` adapter for Claude Code or Codex (see
  "Claude Code / Codex Integration" above).
- `InMemoryPermissionStore`/`InMemoryCodingAuditSink` do not persist
  across process restarts — the same limitation every prior phase's
  in-memory reference implementations carry.
- No authentication — `Principal` models identity, but nothing issues or
  verifies one yet (unchanged from Phase 3).
- Secret detection is best-effort and will miss creatively obfuscated
  secrets (documented directly on `sam.memory.sanitization`, reused
  as-is here).
- Executable resolution trusts a fixed set of common installation
  directories; it cannot detect a system-wide compromise of one of those
  directories (a threat class outside what a request-level authorization
  system can address).
- No file deletion (`DELETE_FILE` is explicitly not implemented, per the
  task) — left behind a future interface if a later phase needs it.
- `list_files` returns a flat listing bounded to 1,000 entries; very
  large repositories will see a `truncated=True` result rather than a
  complete tree.

## Future Extension Points

- A real, safely-sandboxed `ClaudeCodeAdapter`/`CodexAdapter`
  implementing `CodingProvider`, once that scope receives its own
  dedicated security review.
- `DELETE_FILE` as an explicit, separately risk-classified operation.
- A real `AgentCore` integration once a planner/tool-router exists to
  propose coding tasks.
- Promoting `sam.coding.audit`/`sam.memory.audit`/`sam.computer.audit`/
  `sam.permissions.audit` into a single shared `sam.audit` package, the
  same deferred-promotion note every prior phase has left for itself.
