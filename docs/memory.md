# Sam Memory Engine (Phase 4)

## Purpose

The Memory Engine lets Sam store, retrieve, correct, and delete a bounded
set of facts about a principal (a user, Sam itself, a service, or an
integration — see `sam.permissions.models.Principal`, reused here rather
than duplicated) — deterministically, privacy-consciously, and without
ever letting the LLM decide on its own that something should be
permanent.

```
LLM / caller
   │
   ▼
MemoryCandidate
   │
   ▼
MemoryPolicy.decide()   →  STORE / REJECT / REQUIRES_REVIEW
   │  (only STORE continues)
   ▼
MemoryEngine
   │
   ▼
MemoryStore  (Protocol)
   │
   ▼
InMemoryMemoryStore  (Phase 4)      ──future──▶  PostgresMemoryStore + pgvector (cloud)
```

`MemoryEngine` depends only on the `MemoryStore` / `WorkingMemoryStore`
protocols — see "Cloud readiness" below for exactly what that buys.

## Package layout

```
src/sam/memory/
    models.py         domain models (immutable, Pydantic)
    sanitization.py    best-effort secret-pattern detection/redaction
    policy.py           MemoryPolicy.decide()
    store.py            MemoryStore protocol + InMemoryMemoryStore
    working.py           WorkingMemoryStore protocol + InMemoryWorkingMemoryStore
    audit.py             MemoryAuditSink protocol + InMemoryMemoryAuditSink
    retrieval.py         deterministic ranking (rank())
    engine.py             MemoryEngine + PermissionChecker
    errors.py             typed domain errors
```

## The four memory types

| Type | Store | Lifetime | Example |
|---|---|---|---|
| `WORKING` | `WorkingMemoryStore` (never `MemoryStore`) | short-lived, bounded slots, optional TTL | "Current task is reviewing Phase 4." |
| `EPISODIC` | `MemoryStore` | long-term | "Phase 3 Permission Engine was approved." |
| `SEMANTIC` | `MemoryStore` | long-term, user-level | "User prefers Farsi technical explanations." |
| `PROJECT` | `MemoryStore`, requires `project_id` | long-term, project-scoped | "Sam Phase 3 uses a fail-closed authorization model." |

Working memory is a **structurally distinct Python type**
(`WorkingMemoryEntry`, not `Memory`) — there is no code path anywhere that
converts one into the other, so "working memory must not accidentally
become permanent memory" is a static-typing guarantee, not merely a
runtime check. `Memory` itself refuses to be constructed with
`memory_type=WORKING` at all (see its model validator).

## Memory model

`Memory` (long-term, immutable-by-replacement) carries: `memory_id`,
`memory_type`, `principal`, `content` (≤2000 chars), `source`,
`confidence`, `project_id` (required for `PROJECT`), `tags` (≤10, ≤40
chars each), `metadata` (`dict[str, str]`, ≤20 keys, bounded value
length — never an arbitrary nested object, by the type itself), `importance`
(LOW/NORMAL/HIGH), `status` (ACTIVE/ARCHIVED), and timezone-aware
`created_at`/`updated_at`/`last_accessed_at`/`expires_at`. Every bound
exists because it was asked for by a real requirement (bounded record
size, no unbounded tag/metadata growth) — nothing was added for
theoretical completeness.

`MemoryCandidate` is the *proposal* shape a caller (a future AgentCore,
a user command) constructs — not yet a `Memory`. It additionally carries
an optional `reason` (≤500 chars, sanitized, display-only, never used in
any decision).

### Source vs. confidence — the trust model

`MemorySource` (`USER_EXPLICIT`, `USER_CONVERSATION`, `SYSTEM`, `AGENT`,
`TOOL`, `IMPORT`) records *where* a candidate came from. `MemoryConfidence`
(`EXPLICIT` / `INFERRED`) is the one trust distinction Phase 4 makes: did
the user *state* this, or was it *inferred*? That is deliberately the
whole epistemic model — no numeric confidence score, no verification
state machine (the Phase 4 task explicitly asked not to invent one).
Source alone never determines trust; the policy decision (next section)
looks at both together, and an `AGENT`-sourced candidate is never treated
as an authoritative user preference regardless of the confidence it
claims for itself.

## Memory Policy — the LLM cannot write permanent memory by asking

`MemoryPolicy.decide(candidate)` is a pure, deterministic function — no
I/O, no randomness — returning `STORE`, `REJECT`, or `REQUIRES_REVIEW`
with a closed-enum reason on the latter two. Rules, in order:

1. `WORKING`-typed candidates are rejected outright (wrong path — use
   `MemoryEngine.remember_working`).
2. Content matching a known secret-like pattern
   (`sam.memory.sanitization.looks_like_secret`) is rejected — even for
   an explicit, high-trust statement.
3. Trivially short/empty content is rejected.
4. Only `USER_EXPLICIT` and `SYSTEM` sources are eligible for automatic
   storage. Everything else — ordinary conversation, agent inference, a
   tool, an import — is `REQUIRES_REVIEW`: never silently promoted.
5. Otherwise: `STORE`.

`MemoryEngine.remember()` is the only method that ever calls
`MemoryStore.save()`/`.replace()` for a new/duplicate long-term memory,
and it always calls the policy first. There is no second write path.

### Explicit user memory vs. ordinary conversation

"Remember that I prefer Farsi explanations" → `source=USER_EXPLICIT`,
`confidence=EXPLICIT` → eligible for `STORE`. Ordinary chat content, even
if it sounds like a preference, should be submitted as
`source=USER_CONVERSATION` (or left unsubmitted) and will come back
`REQUIRES_REVIEW` — Phase 4 implements no automatic "save every
conversation" behavior, and no autonomous promotion path from
`REQUIRES_REVIEW` to stored exists. A future phase might let a user
confirm a `REQUIRES_REVIEW` suggestion, at which point *that* confirmation
becomes a fresh `USER_EXPLICIT` candidate — not an automatic retry of the
original one.

## Secret handling

`sam.memory.sanitization.looks_like_secret` matches a documented,
best-effort set of patterns: Anthropic/OpenAI-style API key prefixes, AWS
access key ids, GitHub/Slack tokens, bearer-auth phrasing, PEM private
key blocks, JWT-shaped strings, and explicit "password/api key is ..."
phrasing. **This is not comprehensive secret detection** — it will miss
creative obfuscation and may occasionally flag ordinary text. Any
detection results in an unconditional `REJECT`; Phase 4 never stores a
redacted-but-still-persisted version. `redact()` exists only so a caller
can safely *display* why something was rejected without repeating the
secret text — it is not used to produce something that gets stored.

## Ownership, isolation, and deletion

Every read/update/delete requires an explicit `principal`, and
**the store itself enforces ownership** — `MemoryEngine` never assumes "if
a caller reached me, it is allowed." `MemoryStore.get` returns `None` both
for a missing id and for a memory owned by someone else; the two are
indistinguishable to a caller, so a lookup can never be used to probe
whether another principal's memory id exists. `replace`/`delete` re-check
ownership independently as a second, defensive layer even though the
engine already checked via `get` first.

Deletion is a **real removal**, not a status change — unlike a Phase 3
permission grant (kept for audit history via revocation), a memory the
user asked Sam to forget must actually be forgotten. `ARCHIVED` is a
separate, optional status for memories that should stop appearing in
normal retrieval without being deleted (`update(..., status=ARCHIVED)`).

Project isolation is enforced the same way: `MemoryStore.list_candidates`
filters by exact `project_id` string equality (never truncated, never
prefix-matched — `"sam"` never matches `"sam-production"`, confirmed by a
dedicated adversarial test), and `RetrievalQuery` refuses to be
constructed asking *exclusively* for `PROJECT` memories without saying
which project. A query that asks for a *mix* of types including `PROJECT`
without a `project_id` simply drops `PROJECT` from consideration rather
than erroring — it should not fail just because project-scoped facts
exist, but it must never guess which project.

## Retrieval — deterministic, not semantic

**Phase 4 implements deterministic retrieval only.** There is no
embedding model and no vector similarity. `sam.memory.retrieval.rank`
scores each eligible memory (already filtered to the right principal,
type(s), project, and tags by the store) with a plain, documented,
hand-picked formula combining exact/normalized text match, token overlap,
recency (linear decay over a 30-day window), and importance — then sorts
by `(-score, -created_at, memory_id)` for full determinism, and truncates
to `RetrievalQuery.limit` (bounded 1–200, default 20). A future phase may
add real embedding/vector retrieval as an additional signal or an
alternative strategy behind the same `RetrievalQuery` → `RetrievalResult`
contract — it should not need to change this guarantee: same memories,
same query, same `now` → same order, every time.

## Duplicate handling

An exact, *normalized* content duplicate (same principal, type, project,
and case/whitespace-insensitive content) is treated as an access of the
existing memory — `last_accessed_at` is touched and no new record is
created — rather than accumulating identical records. A near-duplicate
(a paraphrase) is left alone; there is no fuzzy matching, per the Phase 4
task's explicit instruction to avoid expensive approximate matching. That
is future semantic-deduplication scope.

## Working memory

`WorkingMemoryStore` is a completely separate protocol/store from
`MemoryStore`, keyed by `(principal, key)`, with a per-principal slot cap
(`InMemoryWorkingMemoryStore(max_slots_per_principal=50)` by default,
raising `WorkingMemoryFullError` on a *new* key beyond that — updating an
existing key is always allowed) and an optional per-entry TTL. It never
passes through `MemoryPolicy` and never reaches `MemoryStore` — there is
no conversion path.

## Audit

`MemoryEngine` records exactly one `MemoryAuditEvent` per operation
(`remember`/`get`/`update`/`delete`/`retrieve`), success or failure, via
an injected `MemoryAuditSink`. `MemoryAuditEvent` mirrors
`sam.permissions.models.AuditEvent`'s discipline — closed enums, ids, and
bounded fields only — but is its own type rather than a literal reuse of
that one: memory operations do not fit the permission-decision vocabulary
(there is no "risk level" or "grant id" for a plain retrieval), and
forcing them through that model would mean inventing fake values for
fields that do not apply. **No memory content, `reason`, or `target` text
is ever recorded** — a dedicated test constructs a secret-bearing
candidate and asserts the resulting audit event's JSON never contains it.
A failing audit sink never changes the operation's result and never
propagates out of the engine (the same trade-off, and the same
`FailingAuditSink`-style test double, as `sam.permissions.engine`).

This does not modify `sam.permissions.audit` in any way. A future phase
that needs a third audit-emitting subsystem is the natural point to
promote both into a shared, general `sam.audit` package (the same
deferred-promotion note Phase 3 left for itself).

## Security summary

- **Principal isolation**: enforced by the store (`get`/`replace`/`delete`
  all require and check `principal`), not by caller discipline.
- **Project isolation**: enforced by exact `project_id` equality at the
  store layer plus a fail-closed `RetrievalQuery` validator; no
  substring/prefix matching anywhere.
- **Secret protection**: best-effort pattern detection → unconditional
  reject, documented as non-exhaustive.
- **Deletion/update boundaries**: ownership checked before any mutation,
  independently by both the engine and the store (defense in depth);
  `update()` only exposes content/tags/metadata/importance/status —
  never principal, type, or project — so it can never be used to move a
  memory to a different owner or scope.
- **Expiration**: `Memory.is_usable`/`WorkingMemoryEntry.is_usable` are
  checked by every read path and by retrieval ranking; the expiry instant
  itself counts as already expired (inclusive boundary), tested exactly.
- **Bounded retrieval**: `RetrievalQuery.limit` is a hard `Field(gt=0,
  le=200)` bound; there is no way to construct an unbounded query.
- **Fail-closed behavior**: every `MemoryEngine` method wraps its
  storage/collaborator calls and converts an unexpected exception into a
  safe result (`REJECT`/`None`/empty result/`MemoryPermissionDeniedError`)
  — never into success, and never leaking the exception's own text.
- **Logging safety**: `sam.memory` contains no `logging` calls at all
  (the same choice Phase 3's engine made) — the audit sink, proven
  content-free by tests, is the sole record of what happened.

## Permission Engine integration boundary

`MemoryEngine` accepts an optional `permission_checker:
sam.memory.engine.PermissionChecker | None`, a minimal Protocol
(`check(*, principal, operation, memory) -> bool`) deliberately **not**
tied to `sam.permissions`' concrete enums — Phase 4 makes no change to
that package. It is consulted only before `delete()` (the operation with
the clearest, most consequential risk — irreversible data loss), and a
denial or a raising checker both fail closed (deletion never proceeds).
Leaving it unset (the default) means deletion relies on store-enforced
ownership alone — the Phase-4-only baseline. A future phase can implement
`PermissionChecker` with a small adapter that internally calls the real
`sam.permissions.engine.PermissionEngine`, translating `operation`/
`memory` into a `PermissionRequest`.

## AgentCore integration boundary

**`AgentCore` is unchanged in Phase 4.** It has no memory-aware
planning step yet, so there is nothing today to wire a real call into —
adding a no-op parameter or a fake memory lookup would be exactly the
"fake tool call" the Phase 4 task forbids. The intended future integration,
per `PROJECT_SPEC.json`'s architecture list, is:

```
AgentCore / future planner
   │
   ▼
Memory retrieval  (MemoryEngine.retrieve, scoped to the acting principal)
   │
   ▼
Planner
   │
   ▼
Permission Engine
   │
   ▼
Tool Router
```

## API boundary

**No new HTTP endpoints were added.** Phase 2's API has no authentication
or session system; an endpoint that let a caller read, write, or delete
memories would let *any* HTTP caller do so for an arbitrary principal —
exactly the kind of unrestricted surface Phase 4 exists to prevent.
`MemoryEngine` is exposed only as an importable, directly-testable Python
module. HTTP exposure is deferred to whichever future phase introduces
real authentication (same reasoning, same precedent, as Phase 3's
Permission Engine).

## Cloud readiness

`MemoryEngine.__init__` depends only on the `MemoryStore` /
`WorkingMemoryStore` **protocols** (structural typing — a class satisfies
them by implementing the right methods, no inheritance required). Nothing
in `sam.memory.engine` imports a database driver, and nothing about its
logic assumes an in-memory `dict`. A future `PostgresMemoryStore`
implementing `save` / `get` / `replace` / `delete` / `list_candidates`
against a table shaped like:

```
memory_id, principal, memory_type, content, project_id,
created_at, updated_at, importance, metadata, ...
```

— and eventually adding a `pgvector` embedding column for a future
similarity-ranking strategy layered *alongside* (not replacing)
deterministic ranking — is a drop-in replacement: `MemoryEngine(store=
PostgresMemoryStore(...), working_store=..., ...)`, no change to
`MemoryEngine` itself. **No PostgreSQL, pgvector, Supabase, Redis, vector
database, or embeddings dependency was added in Phase 4.**

## Known limitations

- **`InMemoryMemoryStore` / `InMemoryWorkingMemoryStore` /
  `InMemoryMemoryAuditSink` are development/test implementations**, not
  production-scale storage: state does not survive a process restart, and
  `list_candidates` loads every matching record for a principal into
  Python before ranking/limiting — fine for the bounded datasets Phase 4
  targets, but explicitly not designed around huge per-principal memory
  counts. A cloud store should push filtering/ranking into the database.
- **No authentication**: `Principal` models identity, but nothing issues
  or verifies one yet — the same limitation Phase 3 already documents.
- **No semantic/vector retrieval** — see "Retrieval" above.
- **Case sensitivity and Unicode normalization**: content/project-id/tag
  comparisons are code-point/case-sensitive with no folding or NFC/NFD
  normalization — always the safe (over-restrictive), never the unsafe
  (accidental-match) direction. Confirmed with an explicit NFC-vs-NFD
  adversarial test.
- **Retrieval ranking weights are hand-picked, not tuned** — they exist
  to make ordering predictable and testable, not to model relevance well.
