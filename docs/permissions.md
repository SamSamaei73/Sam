# Sam Permission Engine (Phase 3)

## Purpose

The Permission Engine answers exactly one question, deterministically:

> "Is Sam allowed to perform this specific action, on this specific
> resource, in this specific scope, right now?"

It never asks the LLM. The LLM (via `AgentCore`, in a future phase, once a
Tool Router exists) may *request* an action; only `PermissionEngine`
decides whether it is authorized. This module implements no tool, no MCP
server, and no integration — it is a pure, provider-neutral authorization
boundary that later phases must call before executing anything real.

```
LLM: "What should I do?"
      -> Policy / Permission Engine: "Am I authorized to do it?"
          -> (future) Tool Router: "Which tool executes it?"
              -> (future) Tool: "Perform the authorized operation."
```

## Package layout

```
src/sam/permissions/
    models.py        domain models (immutable, Pydantic)
    policy.py         static (resource, action) -> risk/confirmation table
    store.py          PermissionStore protocol + InMemoryPermissionStore
    confirmation.py   ConfirmationProvider protocol + InMemoryConfirmationProvider
    audit.py          AuditSink protocol + InMemoryAuditSink
    engine.py         PermissionEngine: evaluate(request) -> decision
    errors.py         typed domain errors
```

`PROJECT_SPEC.json`'s `project_structure.target` list anticipates a
top-level `src/sam/audit` module for cross-cutting audit beyond
permissions. Phase 3 deliberately keeps `AuditEvent`/`AuditSink` scoped
inside `sam.permissions` instead, per the Phase 3 task's own proposed
layout — a generic, top-level audit subsystem is unnecessary abstraction
until a second consumer (tool execution, the coding agent, ...) actually
needs it. That promotion is a candidate for whichever future phase
introduces the next audit-emitting subsystem, not Phase 3.

## Domain model

| Concept | Type | Notes |
|---|---|---|
| `PermissionAction` | closed `StrEnum` | READ, WRITE, CREATE, UPDATE, DELETE, EXECUTE, SEND, PUBLISH, APPROVE |
| `PermissionResource` | closed `StrEnum` | filesystem, terminal, git, gmail, calendar, browser, social_media, mcp, database, financial_service |
| `PermissionScope` | hierarchical segment tuple | see below |
| `RiskLevel` | closed `StrEnum` | LOW, MEDIUM, HIGH, CRITICAL |
| `Principal` | `{kind, id}` | `kind` is user / sam / service / integration |
| `PermissionRequest` | immutable | what's being asked, by whom, for what, why (untrusted display text) |
| `PermissionGrant` | immutable, replace-on-change | persistent authorization; revoked, never deleted |
| `ConfirmationRecord` | immutable, replace-on-change | one-time, context-bound approval |
| `PermissionDecision` | immutable | ALLOW / DENY / CONFIRM_REQUIRED, never a bare bool |
| `AuditEvent` | immutable | secret-free record of one evaluation |

An unknown action/resource *string* cannot become a request at all —
Pydantic rejects it before the engine exists. A valid but unregistered
`(resource, action)` *pair* (both individually legal enum members, but no
row in the policy table) is denied by the engine as `UNCLASSIFIED_ACTION`.
Both paths fail closed; see `sam/permissions/models.py`'s module docstring.

### Scope semantics

`PermissionScope` is a tuple of normalized segments, never a raw string.
Matching (`PermissionScope.contains`) is always whole-segment: a grant for
`("Projects", "Sam")` matches `("Projects", "Sam")` and
`("Projects", "Sam", "src")`, but never `("Projects", "Sam-Secret")` — the
second segment differs as a whole unit, regardless of shared characters.
There is no substring/prefix-string matching anywhere in the codebase.

- `PermissionScope.from_path(raw)` — for hierarchical, filesystem-like
  resources. Resolves `.`/`..` deterministically and raises `ValueError`
  if the path would traverse above its own root, rather than silently
  clamping to root (which could make an escape attempt look like a benign
  match). A bare `"/"` cannot be constructed — there is no way to build a
  scope that matches literally everything.
- `PermissionScope.identifier(value)` — for a single, non-hierarchical
  resource name (a repository id, a mailbox, a service account).

This is deliberately *not* a glob/regex engine. Nested-descendant support
plus exact-segment comparison covers every required Phase 3 case
(hierarchical filesystem paths, flat identifiers) without introducing
pattern-matching complexity that would be harder to audit.

### Risk classification (`sam/permissions/policy.py`)

A static, hand-reviewed table maps `(resource, action) -> (risk,
requires_confirmation)`. It is the *only* source of risk in the system —
never the LLM, never a grant, never runtime configuration. Every CRITICAL
row is asserted at import time to require confirmation, and the engine
enforces the same invariant a second time at evaluation time (defense in
depth): **no grant can ever waive confirmation for a CRITICAL action**,
regardless of what the grant's own flags say.

### Grants vs. confirmation

A `PermissionGrant` answers "is this within the user's granted authority?"
A `ConfirmationRecord` answers "does the user approve this exact execution
right now?" They are never conflated:

- A grant's `always_require_confirmation` flag can only *add* a
  confirmation requirement on top of policy — there is no field that lets
  a grant waive one policy already mandates.
- Confirmations are one-time and context-bound: `consume()` requires an
  exact match of principal, action, resource, scope, *and* `target`
  (a finer-grained identifier than scope — e.g. a specific message or
  file — used only for display and replay-binding, never for grant
  matching), and transitions the record to `CONSUMED` so it can never
  authorize a second action.

### Evaluation precedence

Documented in full in `sam/permissions/engine.py`'s module docstring.
Summary: unclassified action → deny; no fully-matching active, unexpired
grant → deny (with the most specific available reason: revoked vs.
expired vs. no match at all); among fully-matching grants, the most
specific is cited for audit but the confirmation requirement is the union
of *all* matching grants (a broad lenient grant can never silently relax a
stricter narrow one); CRITICAL or any matching policy/grant confirmation
requirement → confirm-required or, given a validly consumed confirmation,
allow; otherwise allow. Any unexpected failure anywhere in this path
(a raising store, a bug) is caught and converted to a safe deny — the
engine's `evaluate()` never raises and never fails open.

## Fail-closed guarantees

- Unknown action/resource string: rejected at `PermissionRequest`
  construction (Pydantic `ValidationError`), before the engine runs.
- Unknown/unclassified `(resource, action)` pair: denied by the engine.
- No grant, revoked grant, expired grant, scope mismatch: denied.
- Missing or mismatched confirmation: denied, never implicitly trusted.
- Any unexpected exception (raising store, raising confirmation provider,
  a bug): caught in `PermissionEngine.evaluate` and converted to `DENY`
  with `reason=SYSTEM_ERROR`. The exception's own text is never logged,
  audited, or included in the decision — only that it happened.
- A failing audit sink never changes an already-computed decision and
  never propagates out of `evaluate()`.

## Audit

Every call to `evaluate()` records exactly one `AuditEvent`, success or
failure. `AuditEvent` has no field for free-text request content: it
carries only closed enums (`action`, `resource`, `risk`, `outcome`,
`reason`), ids, and `scope_summary` (the scope's own already-validated
segments, not arbitrary text). `PermissionRequest.reason` and `.target`
— which may be LLM-influenced — are never copied into the audit record.
Scope identifiers are structural locators (a path, a mailbox id, a repo
id), not a place for secrets; callers must never embed a credential in a
scope segment, the same way a filename shouldn't be a password.

## Known limitations / deliberate simplifications

- **Case sensitivity**: scope segments are compared by exact string
  equality, with no case-folding. On a case-insensitive filesystem this
  can cause an unnecessary denial (safe: fail-closed direction), never an
  unintended match.
- **Unicode normalization**: segments are compared by code point, with no
  NFC/NFD normalization. Same trade-off: only ever over-restrictive.
- **git actions**: `push` is modeled as `WRITE` and local commits/branches
  as `CREATE` — there is no dedicated `PUSH` action. `WRITE` is HIGH-risk
  and confirmation-gated, matching the intended escalation protection; a
  future coding-agent phase may introduce finer-grained git actions.
- **`terminal:execute`** is classified HIGH even though no terminal tool
  exists yet (Phase 5). This is a conservative default; a future phase may
  introduce a narrower, lower-risk sub-action for an explicitly
  allow-listed command set.
- **No authentication**: `Principal` models identity, but Phase 3
  implements no login/session system. Every example and test uses a
  directly-constructed `Principal`; a future phase must wire real
  authenticated identity in before any external exposure.
- **In-memory only**: `InMemoryPermissionStore`, `InMemoryConfirmationProvider`,
  and `InMemoryAuditSink` do not persist across process restarts. A grant
  created in one process run does not survive a restart. Persistent storage
  (PostgreSQL, per `PROJECT_SPEC.json`) is future-phase scope.
- **No API surface**: Phase 3 exposes no HTTP endpoints for granting,
  revoking, or evaluating permissions. See "Future integration boundary"
  below for why.

## Future integration boundary

**AgentCore is unchanged in Phase 3.** It has no tool-execution loop yet
(Phase 2's `AgentCore.execute` performs exactly one bounded provider
call), so there is nothing today for the Permission Engine to gate. Wiring
a no-op `permission_engine` parameter into `AgentCore` now, or faking a
tool call just to exercise the engine, would be exactly the kind of
premature/fake integration the Phase 3 task explicitly forbids. Instead,
the intended future insertion point is documented here: whenever a future
Tool Router is introduced (per `PROJECT_SPEC.json`'s architecture list —
`agent_core -> planner -> policy_engine -> permission_engine ->
tool_router -> tools`), it must call `PermissionEngine.evaluate(request)`
before invoking any tool and branch on `decision.outcome`:

- `ALLOW` — proceed with execution.
- `CONFIRM_REQUIRED` — surface `decision.confirmation` to the user (or a
  future confirmation UI/API) and stop; only re-evaluate with the
  resulting `confirmation_id` after an explicit approval.
- `DENY` — stop; surface only `decision.reason` (a closed enum), never
  raw internals.

**No new API endpoints were added in Phase 3.** Phase 2's API has no
authentication or session system, so any endpoint that let a caller create
or evaluate grants would let *any* HTTP caller grant themselves
permissions — the exact "LLM grants itself permission" failure this phase
exists to prevent, just moved one layer out. The engine is exposed only as
an importable, directly-testable Python module. Exposing it over HTTP is
explicitly deferred to whichever future phase introduces real
authentication.

## Threat model

| # | Threat | Mitigated in Phase 3? | How |
|---|---|---|---|
| 1 | Prompt injection convincing Sam it has permission | Yes | Authorization never reads `PermissionRequest.reason`/`.target`; grants come only from the injected `PermissionStore`, never from request content. |
| 2 | LLM-generated action escalation (READ→WRITE, WRITE→DELETE, ...) | Yes | Action is matched by exact enum equality; no action implies another. Tested directly. |
| 3 | Resource/scope manipulation | Yes | Resource matched by exact enum equality at the store-query layer, independent of scope; scope matched by whole-segment containment, never substring. |
| 4 | Path traversal | Yes | `PermissionScope.from_path` resolves `.`/`..` and denies escape above root; a resolved traversal that lands outside the grant is denied by ordinary scope mismatch. |
| 5 | Identifier confusion (`sam` vs `sam-production`) | Yes | Same whole-segment matching as path traversal; tested explicitly. |
| 6 | Revoked permission reuse | Yes | `_match_grants` excludes non-ACTIVE grants; tested at the exact revoke boundary. |
| 7 | Expired permission reuse | Yes | `_match_grants` excludes grants past `expires_at`; boundary is inclusive (expiry instant counts as expired) and tested. |
| 8 | Confirmation replay | Yes | `consume()` transitions to `CONSUMED`; a second `consume()` on the same id fails. Tested. |
| 9 | Broad wildcard grants | Partially — by design there is no wildcard syntax at all; a literal `"*"` segment is an ordinary opaque string with no special meaning, and a scope with zero segments (a bare root) cannot be constructed. Tested. |
| 10 | Audit-log secret leakage | Yes | `AuditEvent` has no field for request free text; only closed enums, ids, and the scope's own segments are recorded. Tested with a secret-bearing `reason`/`target`. |
| 11 | Application restart/state assumptions | Documented, not solved | The Phase 3 in-memory implementations do not survive a restart. A caller must not assume grant persistence across process restarts until a real store (future phase) is wired in. |
| 12 | Malformed requests | Yes | Invalid construction raises before the engine runs; any other unexpected internal failure is caught by `evaluate()`'s fail-closed handler. |
| 13 | Future MCP tools bypassing the permission boundary | Documented, not enforceable in-process by Phase 3 alone | Architectural contract stated above: every future tool invocation must route through `PermissionEngine.evaluate`. No MCP integration exists yet to bypass. |

## Configuration

No new configuration surface was added. Confirmation TTL is a constructor
parameter on `InMemoryConfirmationProvider` (`DEFAULT_CONFIRMATION_TTL =
5 minutes`) rather than a new `Settings` field, per the Phase 3 task's
"do not create a second source of truth" instruction — there was no
concrete need yet for it to be externally configurable.

## Dependencies

None added. Everything is built from the standard library (`datetime`,
`uuid`, `threading.RLock`, `typing.Protocol`) plus the project's existing
`pydantic` dependency.
