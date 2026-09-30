# Production Integration & Hardening (Phase 17)

Phase 17 turns Sam from a secure multi-phase proof of concept into a durable,
local, owner-only production system. It adds no product features. It adds
durable state, explicit migrations, crash recovery, restart-safe idempotency
for external actions, startup integrity, backups and restore, a credential
boundary, structured health, bounded sanitized logs and packaging checks.

**Status:** implementation in progress; independent-review remediation
applied (durable idempotency including `VERIFIED_SUCCESS`, durable Memory and
Knowledge, a backend owned by the packaged app, exclusive and fully validated
restore, production credential precedence). Pending re-review. Not approved.

The single most important invariant: **a consequential external action
(Career SUBMIT / SEND) is recorded durably as IN_FLIGHT before anything is
dispatched, and nothing that may already have happened is ever retried
automatically, not even across a crash or restart.** Real SUBMIT and SEND
adapters remain unconfigured and gated (see *External-action readiness*).

## State classification

| Class | State | Where |
|---|---|---|
| **A. Must survive restart (safety)** | Career external-action attempts (all states, incl. `OUTCOME_UNKNOWN`, receipts, reconciliation state) | `external_attempts` |
| | Draft / outreach states tied to an attempt (`SUBMITTING`, `SENDING`, `OUTCOME_UNKNOWN`) | `documents` (career) |
| | Owner revocations of bootstrap grants (a restart must not hand access back) | `revoked_bootstrap_grants` |
| | Proactive dedup ledger and cooldown (`last_notified_at`), so a restart never re-notifies | `documents` (proactive) |
| | Schema version and migration history | `PRAGMA user_version`, `schema_migrations` |
| | Security audit metadata (Career operations, HIGH/CRITICAL permission decisions, system events) | `audit_events` |
| **B. Should survive restart (product)** | Memory: long-term memories exactly as `MemoryEngine` accepted them (policy, secret rejection and owner scoping unchanged) | `memory_documents` |
| | Knowledge: collections, resource metadata and provenance (checksum, type, source label and kind, document metadata, timestamps) and accepted chunks; the lexical index is rebuilt deterministically from them | `knowledge_documents` |
| | Professional sources, claims, `EvidenceRef` provenance, reviews, resolutions | `documents` (professional) |
| | Proactive tasks (schedule, explicit timezone, `next_run_at`), condition state, bounded notifications and history | `documents` (proactive) |
| | Career opportunities with provenance, drafts and versions, documents and hashes, questions, contacts, outreach, follow-ups, non-sensitive preferences | `documents` (career) |
| | The owner's explicit "keep scheduling on after restart" choice | `owner_settings` |
| **C. Intentionally ephemeral** | Confirmations (pending, approved, consumed), step-up attempts | memory only; die with the process |
| | Bootstrap grants | recreated from trusted code on every start (revocations re-applied) |
| | Sensitive Career answers (salary, sponsorship, work authorization, disability, demographics, criminal history, legal attestations, …) and sensitive preferences (`salary_preference`, `needs_sponsorship`) | memory only (minimum retention) |
| | Short-term working memory (session state with expiry), conversations, transient provider responses, the activity log, provider / privacy preferences | memory only (safe defaults on restart) |
| | Scheduler worker state (in-flight proactive runs) | memory only |
| Already durable elsewhere | Owner voice template | macOS Keychain (Phase 12) |

Nothing is persisted blindly: only the rows above exist.

## Durable architecture

```
create_app (production)
  BOOTSTRAP            production config checks (fail closed)
  DATA_DIRECTORY_CHECK owner-only 0700 dir, no symlinks, instance lock
  DATABASE_OPEN        0600 file, WAL, synchronous=FULL, foreign_keys, …
  INTEGRITY_CHECK      PRAGMA integrity_check (never repaired or recreated)
  MIGRATION            explicit ordered migrations, one transaction each
  RECOVERY             IN_FLIGHT attempts -> OUTCOME_UNKNOWN
  REPOSITORY_INIT      durable repositories load; Career items follow attempts
  SECURITY_BOUNDARY_INIT bootstrap grants (+ persisted revocations)
  PROVIDER_INIT        credentials: environment, then Keychain (no fallback)
  SCHEDULER_INIT       scheduler OFF unless the owner opted in
  READY
```

If any storage or security phase fails, startup is **BLOCKED**: the desktop
bridge answers only `/status` (with a safe reason code) and every other route
returns `503 storage_unavailable`. There is never a silent in-memory fallback
for durable data.

Development and tests keep the proven in-memory repositories: the storage
backend is `sqlite` when `APP_ENV=production` (or `SAM_STORAGE=sqlite`) and
`memory` otherwise. Production configured with `memory` is refused.

### Repository adapters

Domain services are unchanged and never import `sqlite3`. Each durable
repository (`sam.storage.career`, `.professional`, `.proactive`) is the
existing in-memory implementation plus **write-through mirroring**: after every
mutating call its state is diffed against what was last committed and the
difference is written to the `documents` table in ONE transaction. If the
write fails, the in-memory state is restored to its pre-call snapshot and the
error propagates, so memory never runs ahead of the database and no caller sees
an unpersisted success. `atomic()` groups several calls, and same-database
writes such as the attempt ledger and audit, into one transaction. The
Professional repository keeps Phase 14's atomic per-source refresh: an
ingestion, refresh or source deletion is one transaction.

The Career attempt ledger uses a dedicated typed store
(`sam.storage.attempts.SQLiteAttemptStore`) with no Python pre-checks: the
database enforces uniqueness and transitions.

### Durable Memory and Knowledge

- **Memory** (`sam.storage.memory.SQLiteMemoryStore`):
  - It is the Phase 4 store, write-through to its own `memory_documents`
    table, and persists exactly what `MemoryEngine` accepted. The policy is
    unchanged: secret rejection, owner scoping, duplicate handling, and
    deletion that really deletes.
  - Defence in depth: a record whose content, tags or metadata look like a
    secret is refused by the store before any byte is written, and again on
    load.
  - Short-term working memory stays ephemeral.
- **Knowledge** (`sam.storage.knowledge.SQLiteKnowledgeStore`):
  - It is the Phase 7 store, write-through to its own `knowledge_documents`
    table: collections, resource metadata with its provenance, and the chunks
    Knowledge policy accepted.
  - A resource reaches disk only together with its complete chunk set, so an
    interrupted ingest never leaves half a resource.
  - Original files are never re-read on startup and no content is executed.
  - The lexical index is rebuilt deterministically from the persisted chunks,
    so retrieval identity and ranking are identical before and after a
    restart (tested).
  - Secret-like text is refused before it is written and again on load.
  - Corrupt or incomplete persisted records fail closed.
- **Separation:** Memory, Knowledge and Professional stay separate domains in
  separate tables. Nothing copies data between them.
- **Fail closed on load:** if any durable repository cannot load and validate
  its records, startup is BLOCKED (`repository_load_failed`). There is never
  a silent fallback to empty state.

## SQLite / storage design

- **One file:** `sam.sqlite3` in the data directory, and one connection per
  process used under a lock.
- **Pragmas:** `journal_mode=WAL`, `synchronous=FULL` (a commit is on disk
  before it returns), `foreign_keys=ON`, `secure_delete=ON` (deleted content is
  overwritten), `trusted_schema=OFF`, `busy_timeout=5000`.
- **Transactions:** always `BEGIN IMMEDIATE`, which takes the write lock up
  front so two processes cannot interleave a check and a write. Transactions
  nest (an inner one joins the outer), and any exception rolls back the whole
  outer transaction. Driver errors become `StorageError(<reason code>)`: no SQL
  text or data ever leaves the storage layer.
- **SQL:** every statement is a fixed string with bound parameters. There is
  no arbitrary-SQL interface and no SQL is built from user or model text.
- **Schema v1:**
  - `documents` (domain, kind, key, body): validated model JSON, with bounds
    and CHECK constraints.
  - `external_attempts`: typed columns and CHECK constraints (see below).
  - `audit_events`: metadata-only columns.
  - `owner_settings`: an allowlisted key only.
  - `revoked_bootstrap_grants`.
  - v2: `memory_documents` and `knowledge_documents`, plus the per-manifest
    and per-item idempotency indexes (see *Constraints the database
    enforces*).
  - No table or column can hold a credential (a test enforces the absence of
    any secret/credential/password/token/api-key column).

## App data paths and permissions

- **macOS:** `~/Library/Application Support/app.sam.desktop/`, computed from
  the running user's home directory. The name is the Tauri bundle identifier
  (a Rust test ties the two together).
- **Other POSIX systems:** `$XDG_DATA_HOME/app.sam.desktop`.
- **Override:** `SAM_DATA_DIR`, an absolute path set by trusted local
  configuration only.
- **Layout:** `sam.sqlite3` (plus SQLite's `-wal` / `-shm`), `sam.lock`,
  `backups/`, `logs/`.
- **Permissions:**
  - Directories are created `0700` and files `0600`. Both are re-applied and
    verified after creation; the umask is not trusted.
  - SQLite gives its `-wal` / `-shm` files the database's mode, and Sam
    re-checks them.
  - Anything not owned by the current user, anything too open, and any
    symlink is refused.
  - The rotating log handler opens every file (including rotated ones)
    `0600` with `O_NOFOLLOW`.
- **Outside Git:** production data never lives in the repository. The data
  directory is outside it, `.gitignore` also ignores `*.sqlite3*` and backup
  folders as a safety net, and the test suite points `SAM_DATA_DIR` at a
  temporary directory so no test can touch the owner's data.

## Encryption at rest (honest limitation)

The Python standard library's SQLite has no trustworthy built-in encryption,
and Sam does not add a questionable crypto dependency or invent its own. So:

- sensitive values are minimized (sensitive answers are never persisted);
- data files are owner-only (`0600` / `0700`), and the owner's macOS FileVault
  protects the disk;
- credentials live in the macOS Keychain, never in the database.

Anyone who can read the owner's files as the owner (or as root) can read the
database. Encrypting it (e.g. SQLCipher with a Keychain-held key) is future
work that needs its own review.

## Schema and migrations

- The schema version is an integer, and migrations are an explicit ordered
  tuple. Each runs in ONE transaction (SQLite DDL is transactional) and is
  recorded in `schema_migrations`.
- A **database from the future** is refused (`schema_from_future`): there is
  no silent downgrade.
- A **failing migration** is rolled back completely and startup is BLOCKED
  (`migration_failed`). The previous data and version are untouched (tested).
- A **destructive migration** (flag `destructive=True`) runs only after a
  verified backup, and fails with `backup_required_for_destructive_migration`
  if no backup function is available. v1 is not destructive, but the framework
  is in place and tested.
- Nothing is ever inferred from the models.

## Crash recovery and durable idempotency

### The attempt record

| Field | Content |
|---|---|
| `attempt_id` | Sam-generated identifier |
| `action` | `submit` or `send` |
| `owner_id` | the owner |
| `opportunity_id` | the opportunity |
| `item_id`, `item_version` | draft or message identity and version |
| `manifest_hash` | the immutable manifest's hash |
| `destination` | canonical host, or `sha256:` of the recipient |
| `state` | see below |
| `reconciliation` | `none` / `required` / `reconciled` / `released` |
| `receipt_id` | when verified |
| `reason_code` | safe code only |
| `created_at`, `updated_at`, `finished_at` | timestamps |

### Constraints the database enforces

- **The same manifest is never attempted twice** (schema v2): a partial
  UNIQUE index on `(action, item_id, manifest_hash)` covers `in_flight`,
  `outcome_unknown` **and `verified_success`**. So an action that succeeded,
  or that may have happened, can never be dispatched again: not after a
  restart, not from a second Sam process, not from a stale client retry, and
  not through a direct SQL insert.
- **One unresolved attempt per item:** a partial UNIQUE index on
  `(action, item_id)` covers `in_flight` and `outcome_unknown`, whatever the
  manifest.
- **What is allowed:** a proven `VERIFIED_FAILURE` blocks nothing, so a
  freshly reviewed draft with a new confirmation may try again (the state
  machine requires re-approval). A **changed** manifest after a success is a
  new, separately authorized action at the storage level; the Career state
  machine still keeps a `SUBMITTED` application closed.
- These rules are enforced by SQLite. They were tested with two independent
  connections, raw SQL and no Python pre-check. The in-memory store used in
  development mirrors the same rules.
- **Monotonic transitions only:** the `attempts_are_monotonic` trigger allows
  only these, and refuses anything that would make an unknown or in-flight
  attempt retryable:
  - `in_flight` -> `verified_success` / `verified_failure` / `outcome_unknown`;
  - `outcome_unknown` -> `verified_success` / `verified_failure` /
    `released_by_owner`.
- **Retention:** attempts are never deleted (a trigger refuses it), so unknown
  outcomes and verified receipts are never lost to cleanup.

### Durable outbox order

```
validate (freshness, destination, questions, manifest, permissions, binding)
consume the one-time confirmation
ONE transaction: attempt IN_FLIGHT + draft SUBMITTING + audit row   -> commit
ONLY after the commit: adapter dispatch
ONE transaction: settle attempt + draft (+ follow-up, audit)         -> commit
```

- **The durable reservation fails** (disk full, I/O error, constraint):
  nothing is dispatched (`attempt_not_recorded`). The confirmation is already
  spent, so the owner confirms again.
- **The result cannot be written after dispatch:** the durable record stays
  `IN_FLIGHT` (`result_not_recorded`). In-process retries see it as in flight,
  and the next start turns it into `OUTCOME_UNKNOWN`. A lost receipt never
  becomes a retry.

### Startup recovery

- Every `IN_FLIGHT` attempt becomes `OUTCOME_UNKNOWN` (reason
  `interrupted_by_restart`, reconciliation required). It never becomes failed
  or retryable, because the external side effect may already have happened.
- Each draft or message follows its attempt:
  - in flight or unknown -> `OUTCOME_UNKNOWN`;
  - verified success -> `SUBMITTED` / `SENT`;
  - verified failure -> `SUBMISSION_FAILED` / `SEND_FAILED`.
- A draft that was ready or approved but relied on sensitive answers (which
  were never persisted) goes back to the owner (`NEEDS_OWNER_INPUT`) with its
  approval cleared.

`OUTCOME_UNKNOWN` then leaves only through trusted read-only reconciliation
or a confirmed owner release back to review (Phase 16), which is unchanged
and now durable.

**Process-crash tests** kill a child process with `os._exit` at each point:

| Crash point | Result after restart |
|---|---|
| Before the durable commit | no attempt, no dispatch |
| After the commit, before dispatch | `OUTCOME_UNKNOWN`, no retry |
| After dispatch, before the response | `OUTCOME_UNKNOWN`, no retry |
| After a verified response, before the result was persisted | `OUTCOME_UNKNOWN`, then reconciliation marks it `SUBMITTED` |
| After the persisted result | preserved |

## Confirmations and permissions across restart

Grants and authorization artifacts are classified as follows:

- **ONE_TIME_CONFIRMATION**: never persisted. Pending and approved
  confirmations die with the process, so a confirmation can never survive a
  crash and later authorize a side effect (tested).
- **BOOTSTRAP_POLICY**: recreated from trusted code on every start.
- **PERSISTENT_OWNER_SETTING**: only *revocations* of bootstrap grants. The
  grant is recreated and immediately revoked, so the Permissions view shows it
  revoked. Only narrowing is ever persisted; no grant is ever created from the
  database.
- **SESSION**: does not exist beyond the above (the UI cannot create grants).

## Backups and restore

- **Create:** `python -m sam.system.cli backup [--label L]`. This works while
  Sam runs, because it uses SQLite's online backup API (WAL content included).
  Backups are also made automatically before any destructive migration.
- **Format:** `backups/sam-<UTC time>-<label>.sqlite3` plus a `.json`
  manifest. The manifest holds the format `sam.backup/1`, the app identity, the
  schema version, the size, the sha256, the creation time and the label.
- **Atomic completion:** both files are written under temporary names,
  fsynced and verified (integrity check, schema version, hash), then renamed
  into place with the manifest last, so a backup without a manifest never
  counts.
- **Retention:** the newest 10 backups are kept.
- **Contents:** backups are local only and contain no credentials (there are
  none in the database; tested). Nothing is ever uploaded.
- **Restore:** `python -m sam.system.cli restore <ID> --confirm <ID>`.
  - **Exclusive, enforced:** `restore_backup` itself claims the SAME instance
    lock a running Sam holds for its whole life, whether that is the desktop
    runtime or the bundled backend, including during migration and every
    durable external-action reservation. While any of them is active the
    restore is refused (`another_instance_running`). It is not merely a
    warning; this is tested with Sam running, with a separate process holding
    the lock, and during a migration.
  - Only a Sam-generated id is accepted, resolved inside `backups/`. There is
    no path input and no archive extraction.
  - **Full validation before activation**, on a throwaway copy (the backup
    itself is never modified):
    - manifest, hash and SQLite integrity;
    - foreign keys;
    - the schema migrated to this build (a newer one is refused);
    - external-action attempt invariants: every row parses and the durable
      uniqueness indexes accept the data;
    - every domain repository (Memory, Knowledge, Professional, Career,
      Proactive) loading and validating its own records.
  - Then a `pre-restore` safety backup of the current state is taken and
    itself verified, the live database is checkpointed, stale WAL files are
    removed, and a fsynced copy is renamed into place atomically. A failure at
    any point leaves the live database unchanged.
  - **Integrity, not authenticity:** the sha256 manifest protects against
    accidental corruption, **not** against a malicious local owner, who can
    rewrite both a backup and its manifest. There is no cryptographic
    signature.
- **Supporting commands:** `verify-backup`, `list-backups` and `check`.

## Keychain and credentials

- **One boundary:** `sam.system.secrets` resolves the only credential names
  Sam knows (`anthropic_api_key`, `fish_audio_api_key`, `gemini_api_key`) at
  startup.
- **Production** (`APP_ENV=production`):
  - The macOS login Keychain item `app.sam.desktop.credentials/<name>`,
    read through the native macOS `keyring` backend only, is the **only**
    source. Any other backend (file, plaintext) is refused.
  - Ambient environment values (shell, launch environment, `.env`) are
    **ignored**: they never override or supplement the Keychain.
  - There is no production override switch, and nothing a prompt, model,
    task, web page or the frontend says can select a credential source.
  - The packaged app also starts its backend with a cleared environment, so
    no provider variable even reaches it.
- **Development / test:** an explicitly configured environment value wins,
  then the Keychain if enabled, otherwise the credential is not configured.
- The Claude subscription provider keeps its own sanitized-authentication
  boundary (Phase 13), unchanged.
- **Keychain failure:** that credential is **unavailable**. There is no
  plaintext fallback and no model is ever asked to help, and health shows
  `keychain: unavailable` / `credential_unavailable`.
- **Where credentials never go:** SQLite, Obsidian, backups, the frontend and
  logs (not even a prefix).
- **No automatic migration:** environment secrets are never copied into the
  Keychain. The owner stores one explicitly with
  `python -m sam.system.cli secret-set <name>`.
  - The value is read with `getpass` (hidden input), never from argv, so it
    never appears in the process list or shell history.
  - A value mistakenly typed as an extra argument is refused, and the usage
    error does not echo it (tested).
  - `secret-status` shows sources only.

## Health

`/desktop/v1/status` now carries a content-free `health` object, also
available while BLOCKED:

- **Overall:** `ready` / `degraded` / `blocked`, the startup phase and a
  reason code.
- **Storage:** mode (`sqlite` / `memory`), schema version, and last backup
  time and count.
- **Scheduler:** `off`, `on` or `on_persistent`.
- **Recovery:** the number of attempts awaiting reconciliation.
- **Per subsystem:** one of `ok`, `in_memory`, `not_configured`, `disabled`,
  `degraded`, `unavailable` or `blocked`, with an optional reason code. The
  subsystems are database, migrations, keychain, model_router, knowledge,
  professional, proactive, career, voice, tts, mcp, computer and coding.

Health never contains database content, paths, secrets or exception text. The
desktop's System panel shows it with reviewed static explanations for known
reason codes, and a generic message for anything else.

## Logging, audit and crash reporting

- **Logs:**
  - Every handler uses `RedactingFormatter`. The fully formatted record,
    tracebacks included, is scrubbed of API-key shapes, bearer/basic
    authorization, known secret header or field names and private-key blocks.
  - Production also writes a bounded rotating owner-only file,
    `logs/sam.log`: at most 5 × 1 MB.
  - Sam's own code logs metadata (reason codes, ids), never prompts, CV text,
    e-mail bodies, answers or biometric data.
- **Audit:**
  - `audit_events` holds time, domain, operation, status and optionally a
    reason code, an item id, an attempt id and a manifest hash. There are no
    columns for anything else.
  - It is written for every Career operation (inside the reservation
    transaction for SUBMIT/SEND), every HIGH/CRITICAL permission decision and
    system events (recovery).
  - Retention keeps the newest 50,000 rows.
  - This is durable structured metadata, **not** a tamper-proof log: the
    owner of the machine can edit local files, and Sam does not claim
    otherwise.
- **Crash reporting:** there is no external crash upload. Diagnostics are the
  local, redacted, bounded log only.

## Retention

| Data | Policy |
|---|---|
| Notifications, proactive history | Phase 15 limits, now enforced on disk too |
| Audit events | newest 50,000 |
| Backups | newest 10 |
| Career records | bounded (1,000 per collection) |
| External attempts | never deleted: `OUTCOME_UNKNOWN` evidence and verified receipts are kept until a reviewed retention policy exists |
| Professional provenance | kept while its claim exists (Phase 14 deletion rules unchanged) |

## Data deletion and export

Owner deletions (for example removing a Professional source, deleting a
Proactive task or withdrawing a Career application) go through the same
permission and confirmation rules as before. They now remove the durable rows
in the same transaction as the in-memory state, preserve unrelated records,
and `secure_delete` overwrites the freed pages. Data export is **not**
implemented in Phase 17 and is future work (it would need explicit scope,
privacy warnings and no credential export).

## Scheduler restart policy

- A **fresh install** starts with scheduling **OFF**, and so does every
  **migrated** database: the preference row is absent, which means off.
- Scheduling resumes after a restart **only** if the owner turned it on
  **and** ticked *Keep scheduling on after Sam restarts* (`remember=true`).
  Turning scheduling off always clears that.
- `PROACTIVE_SCHEDULER_ENABLED` in trusted local configuration still works as
  before.
- **Missed runs:** recurring tasks keep Phase 15's SKIP_TO_NEXT semantics from
  their persisted `next_run_at`. A backlog is never replayed.
- **Duplicate notices:** the persisted dedup ledger and cooldown prevent
  repeated notifications after a restart.
- A notification, its dedup key and the cooldown mark are written in one
  transaction.
- A storage failure while recording a run result is logged (reason only) and
  the result dropped. It never kills the scheduler thread or half-writes.

## External-action readiness

`ExternalActionReadiness` must be fully true before a real SUBMIT or SEND
adapter is even considered. It requires:

- `durable_store_ready`
- `migrations_ready`
- `idempotency_ready`
- `reconciliation_ready`
- `destination_validation_ready`
- `adapter_configured`
- `permission_ready` (an owner grant for the action exists)

With no probe the service is never ready. The desktop runtime computes
readiness from facts only it knows. In this build `reconciliation_ready` and
`adapter_configured` are **false**: no trusted adapter exists. So Career
SUBMIT and SEND stay **unavailable**, and the overview reports
`submission_available: false` / `sending_available: false`. Even when fully
ready, every action still needs its permission and a fresh manifest-bound
confirmation. Durable persistence alone never enables an adapter.

## Network policy (inventory)

| Module | Host | Timeout / redirects / retries | Credential | Notes |
|---|---|---|---|---|
| `models/providers/gemini.py` | `generativelanguage.googleapis.com` | finite; `follow_redirects=False`; no retry; `trust_env=False` | Gemini key (env / Keychain) | privacy-routed (Phase 13) |
| `tts/gemini_tts.py` | `generativelanguage.googleapis.com` | finite; no redirects | Gemini key | Persian speech, optional |
| `tts/fish_audio.py` | `api.fish.audio` | finite connect/read/write/pool plus overall deadline; no redirects; `trust_env=False` | Fish key | speech, optional |
| `models/providers/claude_subscription.py` | local `claude` CLI (subprocess) | bounded | owner's own subscription login | no API key |
| `agent/claude.py` | `api.anthropic.com` | SDK timeout | Anthropic key | paid API client, **not wired** (PAID_FALLBACK off) |
| `voice_local/setup.py` | `huggingface.co` | explicit model setup only | none | never at request time |
| `voice_local/install.py` | `huggingface.co`, redirects only to `*.hf.co` | connect 15 s / read 60 s, 1 h overall deadline; manual HTTPS-only redirects (max 5); no retry; `trust_env=False` | none | owner-started from the app only; pinned commit + size + SHA-256; staged, atomic |
| desktop shell (Rust, `ureq`) | `127.0.0.1` only | fixed; no redirects, no proxy, no TLS stack | bridge token | loopback bridge |

A test enforces that only the reviewed provider modules import a network
library, and that `storage`, `system`, `career`, `proactive` and
`professional` import none. There is no arbitrary URL egress. MCP is composed
with an empty registry, so nothing is configured.

## Dependency audit

Phase 17 adds **no dependency**: `sqlite3`, `fcntl`, `json` and `logging` are
standard library, and the Keychain reuses the existing optional `keyring`
(macOS backend only). `uv.lock` is unchanged. There are no git or path
dependencies; the Python HTTP stack is `httpx` only, plus `httpx2`, which the
test client needs in the dev group only. The Rust shell has only
`tauri`, `serde`, `serde_json` and `ureq` (no TLS, no default features).

## Production configuration

`APP_ENV=production` means:

- durable SQLite storage is mandatory;
- `DEBUG` logging is refused;
- the Keychain is on;
- the scheduler is off unless the owner opted in;
- no development mocks exist in the composition (a test checks that the
  runtime source references no fake);
- real side-effect adapters are off (not configured, gated by readiness).

Development and tests use in-memory storage.

## Packaging

`Sam.app` owns its backend. The production app does not need the
repository, a project `.venv`, a manually started uvicorn, system Python,
`PYTHONPATH` or a developer terminal.

### The bundled backend

- `desktop/scripts/build-backend.sh` builds it **outside the repository**
  (`~/Library/Caches/app.sam.desktop.build/backend-dist`):
  - a relocatable **python-build-standalone CPython 3.12.13** (via
    `uv python`), which links only system libraries;
  - exactly the locked production dependencies from `uv.lock`, installed with
    `--require-hashes`;
  - the whole locked `voice-local` group (hash-pinned: torch, torchaudio,
    speechbrain, faster-whisper, keyring and their closure), because voice is
    Sam's primary interaction. **No models are bundled or downloaded**: the
    local voice stack stays inactive until the owner has set up the verified
    models (`sam.voice_local.setup`). This makes the app about 790 MB and the
    backend's cold start about 18 s;
  - the `sam` package as a wheel built from a minimal clean copy (package
    source only).
- The Python test suite, idlelib, tkinter and `pip` are removed.
- The script refuses a non-standalone interpreter and any test code in the
  bundle, and verifies all imports in isolated mode from `/`.
- `desktop/scripts/build-release.sh` builds that backend and then the Tauri
  app bundle, with the backend as `Contents/Resources/backend/python`. The
  bundle is **not signed with any owner identity** (ad-hoc only); signing and
  notarization need a separate decision.

### Launch (`desktop/src-tauri/src/sidecar.rs`)

A release build starts **its own** backend:
- **Executable:** exactly `Contents/Resources/backend/python/bin/python3.12`,
  resolved from the running executable, canonicalized and required to be
  inside the bundle.
- **Invocation:** argv only (no shell) with `-I -B -m sam.system.backend`.
  Isolated mode means no user site, no `PYTHON*` variables and no working
  directory on `sys.path`.
- **Environment:** cleared, then exactly: production mode, a **per-launch
  random bridge token** (32 bytes from `/dev/urandom`, never given to the
  webview), a free loopback port, `HOME`/`TMPDIR`, a minimal `PATH`, a
  locale, `VOICE_IDENTITY_ENABLED=true` (it only *allows* the local voice
  stack, see above) and the login account name as `USER`/`LOGNAME`, passed on
  only if it is a plain account name (letters, digits, `.`, `_`, `-`). The
  Claude CLI finds the owner's subscription login in the Keychain by account
  name: without it the Claude provider reports `not_logged_in` and Sam has no
  language model.
- **Server:** the backend binds **127.0.0.1 only**, with no reload, one
  worker, no proxy headers and no server header, and it refuses to run
  outside production mode.
- **Startup:** the window opens immediately and the backend starts on a
  background thread. Until it is ready every bridge call answers `starting`,
  and the webview shows only Sam's presence with "Starting", with no
  navigation, buttons or error text. The frontend polls quickly, bounded to 60 s.
  Readiness is a finite wait (45 s) on the public `/health`. If it fails,
  the app keeps running **without** a backend: every bridge call is
  `unavailable` and the UI shows Sam as offline, with details under Action
  required and in the system panel.
- **No fallback:** a release build never uses an environment-configured
  (developer) backend. Debug builds keep using the developer's backend.
- **Build identity:** Settings › About shows the version, commit, build time,
  whether the build included uncommitted changes, and whether the shell is a
  release (owns its backend) or a development build (needs a separately
  started backend), so a stale or debug `Sam.app` is recognisable at once.
- **Shutdown:** a quit during startup stops the backend as soon as it is up
  (it is never left behind). On quit the backend's stdin is closed; it exits on EOF, and
  it would even if the app crashed, because the kernel closes the pipe. The
  wait is bounded (10 s), and a kill is the last resort.
- **Duplicate instances:** the backend's data-directory instance lock refuses
  a second Sam, which then serves only a BLOCKED status.

### Isolated artifact check

`desktop/scripts/verify-release.sh` copies `Sam.app` to a fresh temporary
directory and runs its headless self-test (`sam-desktop --self-test
<temporary data dir>`):
- **Conditions:** an empty environment (plus `HOME`/`TMPDIR`), an unrelated
  working directory, and a macOS sandbox that **denies reading the
  repository, the verification venv, the uv Python the bundle came from and
  the system Python framework**, and denies all network except loopback.
- **The self-test:**
  - starts the bundled backend and authenticates over the bridge;
  - reads status (storage `sqlite`) and the owner's grants;
  - checks that a wrong token is refused;
  - stops the backend and checks that it exited.

### Other packaging properties

- `npm run build` builds the bundled frontend, so a release uses `../dist`,
  never the dev server (tested).
- The capability surface is fixed commands only. There is no shell, opener,
  filesystem, HTTP or window-creation permission, the CSP is restrictive, and
  the only network is the loopback bridge. Process spawning exists only in
  `sidecar.rs`, and Rust tests pin its executable, arguments and environment.
- Test-only fakes (`FakeSubmitter`, fake providers, the crash child) live
  under `tests/`, are never packaged, and are never referenced by the runtime
  composition.

## Disaster recovery

| Situation | What Sam does | What the owner does |
|---|---|---|
| Database fails the integrity check | BLOCKED (`database_corrupt`), file untouched | `python -m sam.system.cli list-backups`, then `restore <ID> --confirm <ID>` with Sam stopped |
| Migration failed | BLOCKED (`migration_failed`), data rolled back and untouched | install the previous Sam, or report; restore if needed |
| Database from a newer Sam | BLOCKED (`schema_from_future`) | use that version, or restore an older backup |
| Disk full | writes fail with no false success; SUBMIT/SEND not dispatched | free space and restart |
| Sam crashed during a submission | the attempt becomes `OUTCOME_UNKNOWN` | check with the employer; reconcile or release |
| Keychain unavailable | the affected providers are off | unlock or repair the Keychain |
| Second Sam running | BLOCKED (`another_instance_running`) | close the other copy |

## Known limitations

- **In memory by design:** short-term working memory and conversations.
- **No database encryption:** see *Encryption at rest*. Owner-only files,
  FileVault and minimized sensitive data are the protection.
- **The audit is not tamper-proof.**
- **Reconciliation and release are not in the UI:** they exist at the service
  level, and the UI only surfaces the unknown state. The CLI does not expose
  them either, because no adapter exists.
- **An owner-accepted two-layer SEND loss:** if the reservation cannot be
  committed after the CAREER confirmation was consumed, the owner confirms
  again.
- **A hung adapter** still blocks its item until restart (Phase 16). After the
  restart it is `OUTCOME_UNKNOWN`.
- **Single-machine only:** the instance lock plus the database constraints
  protect one data directory on one machine. A network filesystem is not
  supported.
- **Unsigned bundle:** the release bundle is ad-hoc signed only (no owner
  identity, no notarization). Building it needs network access once, to fetch
  the standalone CPython and any locked wheels missing from the uv cache (all
  hash-verified against `uv.lock`).
- **Blocking startup:** a release launch waits for its backend before showing
  the window (up to 45 s).
- **Backup-before-migration** exists as a framework only: v1 has no
  destructive migration.
- **No data export** (future work).
