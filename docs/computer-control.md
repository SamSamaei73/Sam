# Sam Computer Control (Phase 5)

## Purpose

Phase 5 gives Sam a secure, modular foundation for inspecting and
bounded-interacting with the computer — screenshots, active-window
queries, mouse movement/clicks/scrolling, and keyboard input. **It is
foundational computer-control infrastructure, not an autonomous computer
agent.** Every real action passes through the existing Phase 3 Permission
Engine before anything is dispatched, and no capability here can execute
a shell command, run a program, or touch the filesystem.

```
LLM / caller
   │
   ▼
ComputerAction  (strongly typed, one of ten capability-specific models)
   │
   ▼
sam.computer.policy.build_request()   (pure mapping — no decision)
   │
   ▼
sam.permissions.engine.PermissionEngine.evaluate()   (the only authority)
   │
   ├─ DENY ──────────────────────────► stop, backend never called
   ├─ CONFIRM_REQUIRED ────────────────► stop, backend never called
   └─ ALLOW
        │
        ▼
   ComputerController._dispatch()
        │
        ▼
   ComputerBackend  (Protocol)
        │
        ▼
   FakeComputerBackend (tests) / UnimplementedComputerBackend (today)
        ──future──▶ MacOSBackend / WindowsBackend / LinuxBackend / RemoteComputerBackend
```

## Package layout

```
src/sam/computer/
    models.py         action/result/audit models (immutable, Pydantic)
    capabilities.py    declarative capability → (PermissionAction, scope) registry
    policy.py          ComputerAction → PermissionRequest mapping + risk lookup
    backend.py         ComputerBackend Protocol + FakeComputerBackend +
                        UnimplementedComputerBackend
    controller.py       ComputerController: the only caller of both the
                        Permission Engine and any backend method
    audit.py            ComputerAuditSink protocol + InMemoryComputerAuditSink
    errors.py            typed domain errors
```

## Capabilities

Phase 5 implements exactly these ten, and no others:

```
SCREENSHOT, GET_ACTIVE_WINDOW, GET_SCREEN_SIZE
MOUSE_MOVE, MOUSE_CLICK, MOUSE_DOUBLE_CLICK, MOUSE_SCROLL
KEY_PRESS, KEY_COMBINATION, TYPE_TEXT
```

There is no `EXECUTE_COMMAND`, `SHELL`, `TERMINAL`, `DELETE_FILE`,
`UPLOAD_FILE`, `DOWNLOAD_FILE`, or `INSTALL_APPLICATION` capability, and
`ComputerBackend`'s Protocol has no `run`/`shell`/`execute`/`eval` escape
hatch — a future backend implementation literally cannot expose one
without changing the Protocol itself, which is exactly the "stop and keep
it behind an explicit future interface" boundary the Phase 5 task
requires for anything Phase-6-shaped.

Each capability is its own strongly typed action model (never a
`dict[str, Any]` parameter bag): `ScreenshotAction`, `GetActiveWindowAction`,
`GetScreenSizeAction`, `MouseMoveAction`, `MouseClickAction`,
`MouseDoubleClickAction`, `MouseScrollAction`, `KeyPressAction`,
`KeyCombinationAction`, `TypeTextAction`. None carry an `action_id` or
`created_at` — those are always assigned by `ComputerController`, the
same discipline `sam.permissions` (grant id) and `sam.memory` (memory id)
use: a caller-supplied identifier or timestamp is never trusted for audit
purposes.

## Permission Engine integration — reused, not duplicated

`sam.permissions` was extended with the smallest addition the task asked
for: one new `PermissionResource.COMPUTER` member, and three new rows in
`sam.permissions.policy` mapping `(COMPUTER, action)` to the requested
risk baseline:

| `PermissionAction` | Risk | Confirmation | Capabilities |
|---|---|---|---|
| `READ` | LOW | no | screenshot, get_screen_size, get_active_window |
| `WRITE` | MEDIUM | no | mouse_move, mouse_click, mouse_double_click, mouse_scroll |
| `EXECUTE` | HIGH | yes | key_press, key_combination, type_text |

No new `PermissionAction` was added — every capability reuses `READ`,
`WRITE`, or `EXECUTE`, matching the risk semantics those already carry
elsewhere in the policy table (`FILESYSTEM:READ` is also LOW,
`FILESYSTEM:WRITE` is also MEDIUM/no-confirm, `FILESYSTEM:EXECUTE` is also
HIGH/confirm). **No Phase 5 capability is CRITICAL**, matching the task's
baseline; the CRITICAL-always-requires-confirmation invariant from Phase 3
still applies as defense in depth even though no computer row uses it.

Scopes are explicit, single-segment identifiers — `computer:screen`,
`computer:window`, `computer:mouse`, `computer:keyboard` — one per
capability group, via `sam.permissions.models.PermissionScope.identifier`.
There is no wildcard syntax anywhere in the Permission Engine (a literal
`"*"` scope is an ordinary opaque string, not a glob — see
`docs/permissions.md`), so a broad-looking grant string never
accidentally authorizes an unrelated scope; this is tested explicitly
(`test_06_universal_scope_string_does_not_grant_unrestricted_control`).

`sam.computer.policy.build_request` performs **no authorization decision
itself** — it only translates a `ComputerAction` into a
`PermissionRequest`. The actual ALLOW / CONFIRM_REQUIRED / DENY decision
is made exclusively by `PermissionEngine.evaluate`, called from exactly
one place: `ComputerController._execute_unsafe`. `_dispatch` — the only
place any `ComputerBackend` method is called — is itself only ever
reached from the `ALLOW` branch of `_execute_unsafe`. There is no
shortcut, no `if allowed: backend.click(...)` pattern anywhere that
doesn't trace back to that one `evaluate()` call.

## Confirmation

Reused directly from Phase 3 (`sam.permissions.confirmation`) — Phase 5
adds no new confirmation system. The important consequence: a persistent
`WRITE:mouse` or `EXECUTE:keyboard` grant does not by itself make typing
or a keyboard combination execute — `EXECUTE`-mapped capabilities are
HIGH risk, which Phase 3's engine always requires confirmation for,
independent of any grant.

**Confirmation context binding for typed text** deserves its own note.
Phase 3's confirmation `consume()` requires an exact `target` match to
prevent context substitution (see `docs/permissions.md`). For
`TypeTextAction`, the `target` cannot be the raw typed text — audit and
confirmation records must never carry it — so `sam.computer.policy`
computes a truncated SHA-256 hex digest of the text and uses that as the
target instead. This is deterministic (the same text always hashes the
same way, so approving-then-consuming the identical text works) and
one-way (the digest cannot be reversed to recover the text), while still
proving a confirmation approved for one string can never authorize a
different one (`test_24_confirmation_context_mismatch_is_rejected`). Every
other capability's target is already non-sensitive (coordinates, a key
name, a modifier combination) and is used as-is.

## Coordinate safety — two independent layers

1. **Static, model-level**: every coordinate field is a plain `int`
   (never `float`), which by itself makes NaN/infinity impossible to
   construct. `Field(ge=0, le=20_000)` additionally rejects negative
   values and absurd magnitudes at construction time, before any backend
   or screen-size knowledge is involved.
2. **Dynamic, real-screen-aware**: before any mouse dispatch,
   `ComputerController._check_bounds` queries the backend's *actual*
   `get_screen_size()` and rejects any coordinate outside
   `0 <= x < width` / `0 <= y < height`. If the screen size itself cannot
   be determined, the action fails closed rather than proceeding
   unchecked — a coordinate is never dispatched without both checks
   passing.

## Keyboard safety

`NamedKey` is a closed enum of non-printing/control keys (Enter, Tab,
Escape, arrows, function keys, ...). A key is accepted only if it is a
member of that enum *or* exactly one printable, non-whitespace-except-
space ASCII character — never an arbitrary string, never a multi-key
"word" like `"ctrl+c"` typed as a single field. `KeyCombinationAction`
separates modifiers (a closed `ModifierKey` enum: CTRL/ALT/SHIFT/CMD,
1–4 of them, no duplicates) from the one non-modifier key, itself
validated the same way. Nothing here listens for, captures, or logs
keystrokes — every keyboard action is Sam *sending* one bounded input,
never *observing* one; there is no hook, listener, or capture method
anywhere on `ComputerBackend`.

## Typed-text safety

Bounded to 500 characters. Null bytes are always rejected. Control
characters are rejected **except** tab and newline, which are accepted as
ordinary, legitimate typed input (a form field, a multi-line note) — every
other control character is rejected outright rather than silently
stripped, since silently altering typed text could change its meaning
without the caller noticing. Phase 5 does **not** attempt to detect or
censor secret-looking typed content (unlike `sam.memory`'s policy, which
governs what gets *remembered*, not what gets *typed*) — typing is an
explicitly authorized, confirmation-gated, one-shot action, and refusing
to type legitimate-but-secret-looking text (a password field is exactly
where a real password belongs) would be more surprising than helpful. The
guarantee Phase 5 makes instead is that the text itself never appears
anywhere outside the one backend call it was meant for — not in the
permission request's target (hashed), not in the audit record (no field
exists for it), and not in any log (`sam.computer` contains zero
`logging`/`print` calls).

## Screenshot handling

`screenshot()` returns a controlled `ScreenshotResult` (width, height,
PNG bytes, capture timestamp) directly to the caller — nothing in
`sam.computer` writes it to disk, includes it in an audit record, sends
it anywhere external, or caches it. Its lifecycle is exactly one Python
object, returned once, garbage-collected once the caller drops its
reference; there is no hidden screenshot cache anywhere in this package.
Automatically persisting a screenshot (to disk, to Memory, or to an
external service) is out of scope for Phase 5 by design.

## Active window

`GetActiveWindowAction` returns only `application`, `title` (bounded to
300 characters), and `bounds` — never application history, browser
history, clipboard contents, keystroke history, or unrelated-window data.
Clipboard access is not implemented in Phase 5 at all (per the task's
explicit instruction to leave it for a later phase if ever added) — there
is no clipboard capability, model, or backend method anywhere in this
package.

## Action sequences — bounded, never autonomous

`ComputerController.execute_sequence` accepts an already-finite,
caller-supplied list of actions, rejects it outright (before executing
anything) if longer than `max_sequence_length` (default 10), and stops at
the first non-`SUCCESS` result rather than continuing past a failure.
Nothing inside `execute_sequence` or `execute` constructs a new
`ComputerAction`, calls itself recursively, or loops indefinitely — there
is no `while True: generate_action(); execute_action()` anywhere in this
package, and there cannot be one added without a visible, reviewable
change to this specific bounded loop.

## Fail-closed behavior

`ComputerActionResult` enforces a structural invariant via its own model
validator: a `SUCCESS` outcome must never carry an `error_category`, and
a `FAILED` outcome always must. `ComputerController.execute` wraps
policy-mapping, permission evaluation, and dispatch in one `try/except`
that converts *any* unexpected failure (a raising permission engine, a
raising backend, a bug) into a `FAILED` result with
`error_category=INTERNAL_ERROR` — the exception's own text is never
included, since it may carry backend- or provider-specific internals. A
`SUCCESS` result is only ever constructed in the one branch of
`_dispatch` where the backend call itself returned without raising —
there is no code path that reports success without the backend having
actually confirmed it.

## Audit

Reuses the project's audit *pattern* (`record(event) -> None`, a
failing sink never changes the action's result and never propagates) —
its own `ComputerAuditEvent` type, not a literal reuse of
`sam.permissions.models.AuditEvent` or `sam.memory.models.MemoryAuditEvent`,
for the same reason `sam.memory.audit` already documents: neither
existing vocabulary fits a computer action cleanly. Recorded per action:
timestamp, action id, principal, capability, risk, permission outcome,
confirmation outcome, execution outcome, error category. **Never**
recorded: typed text, coordinates, key names, screenshot bytes, or any
raw exception message — confirmed by two dedicated tests
(`test_17_screenshot_bytes_never_appear_in_audit_records`,
`test_18_typed_text_never_appears_in_audit_records`) and by the model
itself simply having no field for any of them.

## Memory integration boundary

**No automatic `Computer action → Memory` path exists.** `sam.computer`
does not import `sam.memory` anywhere (confirmed by grep). A future phase
that wants Sam to selectively remember a meaningful computer event must
route it through `sam.memory`'s own `MemoryCandidate` → `MemoryPolicy`
flow explicitly, the same as any other candidate — never automatically,
and never bypassing that policy.

## AgentCore integration boundary

**`AgentCore` is unchanged in Phase 5.** The intended future integration
point, matching `PROJECT_SPEC.json`'s architecture list, is:

```
AgentCore / future planner
   │
   ▼
ComputerAction proposal
   │
   ▼
ComputerController.execute() / execute_sequence()
   │
   ▼
Permission Engine  →  Backend
```

Wiring a live call into `AgentCore` today would either be a no-op
(nothing yet proposes computer actions) or would have to fake one — both
are exactly what the task forbids. This boundary is documented, not
implemented, the same choice Phase 3 and Phase 4 made for their own
`AgentCore` integration points.

## Why no real OS backend ships in Phase 5

Two concrete backends exist: `FakeComputerBackend` (a deterministic test
double that records every call as a bounded, content-safe structured
record) and `UnimplementedComputerBackend` (structurally complete,
raises `BackendUnavailableError` for every method). **No real
`MacOSBackend`/`WindowsBackend`/`LinuxBackend` was implemented.** Reasons:

1. **This session runs headless** — there is no interactive display to
   drive or to verify against. A "real" backend built here could not be
   meaningfully tested against actual screen/mouse/keyboard behavior in
   this environment; shipping an untested real backend is arguably more
   dangerous than an honestly-absent one.
2. **A real OS automation dependency is a meaningful new supply-chain and
   native-code surface** (large transitive dependency trees, OS-level
   Accessibility/Screen-Recording permissions on macOS) that deserves its
   own deliberate review — not one bundled into an already-large phase
   under time pressure. This project has already encountered one
   dependency-safety incident (a typosquat package caught during Phase 2
   review); new automation dependencies get scrutiny, not convenience.
3. **The Phase 5 task explicitly sanctions this fallback**: "If a safe
   real backend cannot be implemented reliably without introducing
   excessive risk, implement a clearly isolated backend interface plus a
   deterministic test/fake backend and document the limitation."

The entire policy → Permission Engine → controller → backend pipeline is
real and fully tested end-to-end against `FakeComputerBackend` and
`UnimplementedComputerBackend`; only the literal OS calls are pending. A
future `MacOSBackend` only has to implement the ten-method
`ComputerBackend` Protocol — nothing in `models.py`, `capabilities.py`,
`policy.py`, `controller.py`, or `audit.py` needs to change.

## Known limitations

- No real OS backend (see above) — Phase 5 cannot yet actually move a
  mouse or type a key on a real machine.
- No clipboard access (deliberately deferred, per the task).
- No browser automation, terminal, shell, or filesystem manipulation
  (explicitly out of scope; see "Phase Boundary" in the final report).
- `execute_sequence`'s bound (`max_actions`, default 10) is a fixed
  constructor parameter, not dynamically negotiated — a reasonable,
  conservative default rather than a tuned value.
- Coordinate bounds checking requires a successful `get_screen_size()`
  call on every coordinate-bearing dispatch; a backend that cannot report
  its screen size can never authorize a mouse action, by design (fail
  closed over silently skipping the check).

## Future extension points

- A real per-OS `ComputerBackend` implementation (Phase 6+).
- UI-element identification from screenshots or accessibility APIs
  (mentioned in the Phase 5 objective as an eventual goal; not
  implemented — Phase 5 returns raw screenshot bytes and structured
  window metadata only, no element detection).
- A `PermissionChecker`-style pluggable hook, if computer actions ever
  need a second, independent authorization layer beyond the Permission
  Engine (none exists today — the Permission Engine is already the sole
  authority, so no additional hook was introduced).
- A real `AgentCore` integration once a planner/tool-router exists to
  propose actions.
