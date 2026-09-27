# Proactive Agent (Phase 15)

Sam's **Proactive Agent** (`src/sam/proactive/`) lets the owner create reminders,
scheduled summaries and condition watches that run over time and surface meaningful
changes as notifications, without a new message each time.

**Proactive does not mean autonomous authority.** A run may evaluate, summarize,
decide whether a notification is warranted and create a safe notification. It never
sends email or messages, applies for jobs, modifies files, runs shell commands, uses
Computer Control or MCP tools, publishes, deletes data, approves transactions or
performs any HIGH or CRITICAL action. The PermissionEngine is still the only
authorization authority, and it is asked again on every run.

**Status:** approved after independent review. Storage is **in-memory only**.
Background scheduling is **off by default**.
This is the proactive framework; the Career and PhD automation is Phase 16.

## Architecture

```
trigger (scheduler tick or "run now")
  → ProactiveService / ProactiveScheduler             [service.py, scheduler.py]
  → task re-validation (ProactivePolicy)              [policy.py, schedule.py]
  → expiry check
  → PermissionEngine: PROACTIVE / EXECUTE, now        [runner.py]
  → action
      REMINDER  deterministic draft
      SUMMARY   one call through the Phase 13 ModelRouter
      WATCH     observer's own permission, then observe  [conditions.py, observers.py]
  → change detection (state machine)                  [conditions.py]
  → deduplication → cooldown → notification level     [dedup.py, cooldown.py]
  → notification (never an action) + history + audit  [repository.py, audit.py]
```

| Module | Role |
|---|---|
| `models.py` | closed enums and immutable records: `ProactiveTask`, `Schedule`, `ConditionSpec`, `ConditionState`, `ProactiveNotification`, `RunRecord` |
| `clock.py` | `Clock`, `SystemClock`, `ManualClock` (tests never sleep) |
| `schedule.py` | deterministic occurrence arithmetic, timezones, DST, schedule validation |
| `policy.py` | `ProactiveLimits`, `ProactivePolicy` (validation), `ProactiveNotificationPolicy` |
| `conditions.py` | the trusted `ConditionRegistry` and the watch state machine |
| `observers.py` | built-in, read-only, deterministic observers |
| `dedup.py`, `cooldown.py` | Sam-owned deduplication and cooldown |
| `runner.py` | runs ONE task once; never schedules, retries or acts |
| `scheduler.py` | bounded scheduler: tick, dispatch, coalescing, missed runs, timeouts |
| `repository.py` | `ProactiveRepository` protocol, `InMemoryProactiveRepository` |
| `service.py` | `ProactiveService`, the one owner-facing entry point |
| `audit.py` | metadata-only audit |

The LLM is not the scheduler, not the clock and not the authorization authority.

## Task types and timing modes

Three explicit, trusted task types (never inferred from model output):

| Task type | Timing mode | Actions | Example |
|---|---|---|---|
| `ONE_TIME` | `EXACT_SCHEDULE` or `FLEXIBLE_SCHEDULE` | reminder, summary | "Remind me tomorrow at 3 PM." |
| `RECURRING` | `EXACT_SCHEDULE` or `FLEXIBLE_SCHEDULE` | reminder, summary | "Every morning summarize X." |
| `CONDITION_WATCH` | `CONDITION_WATCH` | watch | "Tell me when X becomes true." |

* **EXACT_SCHEDULE:** an exact local wall-clock time.
* **FLEXIBLE_SCHEDULE:** a daypart (`morning` 08:00–12:00, `afternoon` 12:00–17:00,
  `evening` 17:00–21:00); any time inside the window is on time.
* **CONDITION_WATCH:** checked on a cadence; notifies only when a trusted condition
  changes in a way the task's trigger semantics say is meaningful.

A task stores data only: a schedule, one closed action, an optional reference to a
condition in Sam's registry, and the owner's instruction text. There is no field for
code, a command, a module path, a provider, a permission, a risk level or a
confirmation, and every model refuses unknown fields.

## Schedule representation

`Schedule` is a parsed, deterministic model, not a cron string and not RRULE text:

* `timezone`: an explicit IANA name, stored **apart from** the wall-clock time;
* `start_date` and either `time_of_day` (minute precision) or `daypart`;
* `frequency`: `none`, `hourly`, `daily` or `weekly` (there is no minutely or
  secondly member), `interval`, optional `weekdays`;
* end condition: `until` (a local date) or `max_runs`.

Nothing is evaluated, imported or executed; a malformed schedule fails closed.

### Timezones and DST

Occurrences are computed from (local date, local time, zone) with `zoneinfo`, never by
a model:

* **Nonexistent local time** (spring-forward gap): resolved with `fold=0`, so it runs
  once, shifted forward by the gap (01:30 on the UK change day runs at 02:30 BST).
* **Ambiguous local time** (fall-back): the **first** occurrence is used; daily and
  weekly schedules produce one occurrence per local date, so it never runs twice.
* **Hourly** schedules step in elapsed hours from their anchor, so the interval stays
  exact while the local clock time shifts across DST.
* **Timezone change:** editing the task's timezone keeps its wall-clock time and
  recomputes the next run in the new zone; a change to a zone's rules is honoured
  because nothing is precomputed beyond the next run.

## Minimum frequency

`ProactiveLimits.min_interval` is one hour and its constructor refuses anything lower.
Any recurrence or watch cadence shorter than the minimum is rejected. There is no
minutely or secondly frequency at all.

## Scheduler

A bounded, Sam-owned scheduler (no system cron, no thread per task):

* `tick()` reaps timed-out runs, expires tasks past `expires_at`, and walks due tasks in
  `(next_run_at, task_id)` order. The background loop calls `tick()` and waits on an
  `Event` (`tick_seconds`, at least 5 s); there is no busy loop.
* Each run is one daemon worker thread. At most `max_concurrent_runs` (default 2) runs
  are live at once, and a live run always counts, including one that has timed out but
  whose thread has not exited. At most `max_dispatch_per_tick` (default 10) dispatches
  happen per tick; a due task that cannot start stays due (backpressure).
* A task is never run twice at once: if it comes due while running it is **coalesced**
  (skipped to its next occurrence) and "run now" answers `already_running`.

### Enabling scheduling (off by default)

A fresh runtime runs **nothing**: `tick()` and "run now" do no work and no loop thread
exists until the owner turns scheduling on. There are two trusted, owner-only paths:

* the **Automations switch** (`POST /desktop/v1/proactive/scheduler`), which needs
  PROACTIVE UPDATE, is refused in Guest Mode, and resets to off on every restart (the
  preferred path for this in-memory POC);
* trusted local configuration `PROACTIVE_SCHEDULER_ENABLED=true` (default `false`),
  read at startup.

No prompt, model output, task or proposal can reach either path. Turning scheduling on
grants no permission: every run is still authorized on its own. Turning it off stops
the loop and all new runs at once; runs already in flight finish (or time out) under
the normal policy. Application shutdown also cancels live runs cooperatively.

### Missed runs (SKIP_TO_NEXT)

* **Recurring:** later than its grace (1 h for an exact time; the end of the window for
  a flexible one) → **not replayed**. One `skipped_missed` record is written and the
  next occurrence after *now* is scheduled, however many were missed.
* **One-time:** overdue → marked `MISSED`, with one "reminder was due while Sam
  wasn't running" notification unless the task is silent.

The overdue rule is exact and deterministic (no model): an EXACT-time task is missed
iff `now - scheduled_time > OVERDUE_THRESHOLD` (1 hour, trusted configuration bounded
to 1 minute–24 hours). Exactly at the threshold it still runs; one second beyond, it
is missed. A FLEXIBLE task is missed iff `now` is at or after the end of its daypart
window.
* **Watch:** never "missed"; it simply checks once now.

Because storage is in-memory, a restart starts empty: there is never a backlog.

### Timeouts, cancellation and concurrency

Every run has a deadline (`run_timeout`, default 5 min) and a cancel `Event`.

* **Cancellation is cooperative.** When a run passes its deadline, the next tick
  records it as `TEMPORARY_FAILURE` (`run_timeout`) and sets its cancel event. The
  runner checks that event between every step (before and after authorization, before
  and after an observation, before and after the model call), so once the blocking call
  it was in returns, it authorizes, observes, routes and writes nothing more.
* **The supervisor owns the timeout record.** `ProactiveScheduler._reap` (the
  scheduler, under its lock, not the worker) marks the run abandoned, sets its cancel
  event and records the timeout **exactly once**: one `timed_out` history record and
  one metadata-only audit event (`TEMPORARY_FAILURE`, reason `run_timeout`; no task
  text, observation, provider output or secret). The `abandoned` flag guarantees it is
  never recorded twice.
* **No detached work.** Python cannot kill a running thread, so until the worker thread
  actually exits: the task stays **in flight** (it is never started again, and a due
  occurrence is coalesced), and the run keeps its **concurrency slot**; the bound on
  live threads holds even when every worker hangs. The slot and the in-flight mark are
  released in exactly one place, when the worker thread finishes.
* **Late results are discarded completely, by the supervisor.** `_finalize` discards
  the outcome of any abandoned run, even one whose worker ignored cancellation and
  reports success. A timed-out run creates no notification, adds no second history or
  audit entry, never overwrites the timeout with a success, and updates no dedup,
  cooldown or condition state and no task field.
* **Every external boundary is bounded.** Each observer declares `max_seconds` (built-ins:
  deadline 1 s, Professional count 5 s, provider status 45 s, which covers the Claude
  adapter's two 20 s readiness probes), and each Phase 13 provider call has its own
  timeout (Claude 120 s, Gemini 60 s). The service refuses to start if any observer, or
  the router's worst case (the two slowest provider timeouts plus a 60 s probe
  allowance, 240 s today), exceeds the run budget.
* Worker threads are **daemon** threads, so a hung one never blocks process exit.

## Condition watches

A watch names ONE condition in the trusted `ConditionRegistry` by exact id. The
registry is built once by Sam's composition code (`desktop/runtime.py`); there is no
dynamic import, module path or callable in a task, and an unknown id is invalid. Each
entry declares the permission it needs; the runner asks the PermissionEngine for it
on every run.

Built-in observers (read-only, deterministic, no model, no network):

| Condition | Signal | Permission |
|---|---|---|
| `deadline_approaching` | `deadline - lead_hours <= now < deadline` (pure clock arithmetic) | PROACTIVE READ |
| `model_provider_status` | Sam's own provider availability (as on the Models page); never calls a model | PROACTIVE READ |
| `professional_open_conflicts` | count of open Professional conflicts, via `ProfessionalService` | PROFESSIONAL READ |

State (`met`, `value_key`, `episode`, `observed_at`, `last_notified_at`) lives in the
repository for the process lifetime. With `BECOMES_TRUE` (the initial state is
"not met"):

| Previous → current | Change | Notification |
|---|---|---|
| false → false | `NO_CHANGE` | none |
| false → true | `CONDITION_MET` | candidate |
| true → true | `NO_CHANGE` / `CHANGED` | none (unless `REPEAT_WHILE_TRUE`) |
| true → false | `CONDITION_CLEARED` | none; state updated |
| false → true again | `CONDITION_MET` | candidate, subject to cooldown |
| error | `ERROR` | none; state unchanged |
| unknown | `UNKNOWN` | none; state unchanged |

`ON_CHANGE` records its first observation as a baseline and reports `CHANGED` when the
observed value changes. A task running is never itself a reason to notify.

### Deduplication

`dedup_key = sha256(task_id | event identity | normalized state/version)`. For a watch
the event identity includes the condition and its episode; for a reminder the
occurrence; for a summary a hash of the summary. Keys are kept in a bounded, windowed
ledger (7 days, 5,000 keys). A duplicate key creates no notification.

### Cooldown

`cooldown_hours` (default 24, 1–720) is the minimum time between two notifications
from one watch. It is part of the owner's task, changed only through trusted task
management; a model proposal cannot set it.

## Notifications

`ProactiveNotificationPolicy` levels: `SILENT` (history only), `NOTIFY_OWNER`,
`REQUIRES_ATTENTION`. A run's result is `NO_NOTIFICATION`, `NOTIFICATION_CREATED`,
`ACTION_PROPOSED` or `ERROR_RECORDED`. **A notification is not an approved action**:
`ACTION_PROPOSED` means the notification carries a *label* such as "Review the
deadline"; nothing executes it.

A notification holds bounded, safe metadata only: id, task, owner id, title, a short
summary (≤ 500 characters), time, reason code, importance, source label, proposed
action label and the task's privacy class. Failed runs never create a success
notification.

### The final notification gate

Provider and observer output is untrusted, whoever produced it. Every notification
passes one gate (`notifications.safe_notification`) at the single write path, just
before it is stored and so before it can be displayed.

**Privacy comes from provenance, not from text.** Every notification draft carries a
trusted privacy class (`PUBLIC` < `NORMAL` < `PERSONAL` < `PRIVATE` < `SECRET`) set by
Sam's code, independently of the text:

| Draft | Trusted class |
|---|---|
| reminder | the task's owner-set class |
| summary | the task's owner-set class (PUBLIC or PERSONAL only; see below) |
| watch | the strictest of the task's class, the observer registration's class (`ConditionEntry.privacy_class`) and the observation's class (`Observation.privacy`) |
| missed-reminder notice | the task's class |

Built-in registrations: deadline PERSONAL, provider status NORMAL, Professional
conflicts **PRIVATE**. **Monotonicity:** the gate joins the trusted class with what
the text detectors find and keeps the strictest. Text (from a model, a provider, an
observer or the task instruction) can only raise the class, never lower it: a PRIVATE
observation with harmless-looking text stays PRIVATE; SECRET stays SECRET.

Then, deterministically:

* **SECRET:** nothing from the draft is stored. The notification is a fixed notice
  ("Sam found an update for this automation but withheld all of its details because
  they looked like a secret.") with a generic title.
* **PRIVATE:** no raw observation, provider or task text is copied anywhere. The
  notification is a fixed template naming only the trusted source label: title
  "Private automation update", summary "An update requiring your attention was
  detected in <source>. Open Automations in Sam for details." The owner opens the
  authorized view for the details.
* **PERSONAL / NORMAL / PUBLIC:** the cleaned, bounded text is shown.
* The reason and the suggested next action are closed enums; an untrusted source
  label is replaced by "automation".

Pattern detection (secrets; e-mail headers or addresses, phone numbers, long
account/card/ID digit runs) stays as defence in depth **after** provenance: it can
raise a class (for example PERSONAL text containing an e-mail body becomes PRIVATE),
never replace the trusted one. The run's history reason becomes `withheld_secret` or
`withheld_private`. Nothing rejected is ever written to the audit, the history or the
activity log.

**Private summaries are not supported in Phase 15.** Because a PRIVATE notification
never shows derived text, a PRIVATE summary could never be displayed. So a summary task
cannot be created or edited to PRIVATE (`private_summary_not_supported`), and nothing
private is sent to a provider for it.

## Authorization

`PermissionResource.PROACTIVE` (never another domain's resource):

| Action | Operations | Risk |
|---|---|---|
| READ | list tasks, history, notifications | LOW |
| CREATE | create a task | MEDIUM |
| UPDATE | edit, enable/disable, mark notifications read/dismissed | MEDIUM |
| EXECUTE | every scheduled run and "run now" | MEDIUM |
| DELETE | delete a task | HIGH, **always confirmed** |

* **Run task ≠ execute side effect.** A run needs PROACTIVE EXECUTE *now*; revoking
  that grant stops all runs. An observer that reads another domain needs that
  domain's own permission on every run (e.g. PROFESSIONAL READ, MCP READ).
* A proactive run never performs a HIGH or CRITICAL action: if a required permission
  is HIGH/CRITICAL or requires confirmation by policy, the run is `POLICY_BLOCKED`.
* **Task text is data, not authority.** "Check my email and delete spam" authorizes
  nothing; the run only reads what its own permissions allow.
* **Confirmations are never reused.** No task stores a confirmation (the models have
  no such field) and a run always evaluates with `confirmation_id=None`. A confirmation
  from Monday cannot authorize Friday's run; if a run would need one, it is recorded as
  `PERMISSION_DENIED` (`confirmation_required`) and the approved confirmation stays
  unconsumed.
* A principal only sees and changes its own tasks and notifications.
* The desktop bootstrap grants the local owner READ, CREATE, UPDATE, EXECUTE and
  DELETE on scope `proactive` (DELETE still confirms by policy). Every grant is
  revocable in the Permissions view.

### What PROACTIVE EXECUTE means, and why the bootstrap grant is non-consequential

In Phase 15, PROACTIVE EXECUTE authorizes **only**: evaluating one task, the READ
observation its trusted observer declares, one text-only summary through the Phase 13
ModelRouter, and one local, bounded, gated notification. It is the only bootstrap
`execute` grant, and it is safe by construction:

1. The PermissionEngine matches grants by resource and action, so this grant can
   authorize nothing but PROACTIVE/EXECUTE (tested against every row of the policy
   table).
2. A run can only ever ask for its own PROACTIVE/EXECUTE and a READ:
   `TaskRunner.authorize` refuses every other (resource, action) **before** asking the
   PermissionEngine. There is no SEND, DELETE, PUBLISH, APPROVE, WRITE, CREATE or
   UPDATE path, and nothing HIGH, CRITICAL or confirmation-requiring.
3. The condition registry refuses any observer that requires anything but READ, and
   the runner refuses one even if it were registered by mistake.
4. An observer's READ still needs that resource's own current grant (PROFESSIONAL,
   MCP, ...); PROACTIVE EXECUTE never substitutes for it.
5. Task text and model output cannot name a permission (no such fields), and "run
   now" goes through the same run-time authorization as a scheduled run.

Task-scoped EXECUTE grants were considered. They would require the proactive service
to create grants, a new grant-creation path that the desktop design deliberately does
not have, so the single broad grant plus the construction-level restrictions above was
chosen instead.

### Task creation from chat (model proposals)

`ProactiveService.propose_from_model` accepts untrusted model output only in the
strict `ModelTaskProposal` shape (title, type, timing, action, schedule, condition,
instruction). Any other key (enabled, cooldown, provider, confirmation, command, code
...) rejects the whole proposal. A valid proposal becomes a **disabled draft**; nothing
is stored or scheduled. The owner must create it (CREATE) and enable it (UPDATE). The
model cannot mutate the repository. `AgentCore` has no tool loop yet, so chat is not
wired to this boundary in Phase 15.

## Privacy and provider routing

* Privacy is **re-evaluated on every run**: the task's current privacy class
  (owner-set: public, personal or private; never secret) goes to the Phase 13
  `ModelRouter`, which re-reads owner preferences, cost policy and availability on
  every call. A task created months ago does not override today's policy; a stricter
  policy fails closed.
* **SECRET:** secret-looking task text is never stored; a run whose text or context
  looks secret is blocked before the router is reached (zero provider calls).
* **PRIVATE:** denied to the Gemini free tier by default (Phase 13); if Claude is
  unavailable the run fails rather than falling back.
* Summaries always go through the router, never a pinned provider. Paid fallback stays
  **OFF**; a task cannot enable OpenAI or Grok, turn on paid fallback or change privacy
  settings (it has no such fields and the runner never touches settings).
* One router call per run, no retry. A provider **refusal** is final: it is recorded as
  `POLICY_BLOCKED` (`provider_refusal`) and is never re-routed to another provider.

## Failure behaviour

Failures are classified: `TEMPORARY_FAILURE`, `PERMISSION_DENIED`,
`PROVIDER_UNAVAILABLE`, `RATE_LIMIT`, `SOURCE_UNAVAILABLE`, `INVALID_TASK`, `EXPIRED`,
`POLICY_BLOCKED`. There is **no immediate automatic retry** and no recursive retry; the
next scheduled run tries normally. A task that fails validation at run time (for
example a corrupted record) is marked `INVALID` and disabled.

## Resource limits (fail closed)

| Limit | Default |
|---|---|
| tasks / enabled tasks | 100 / 50 |
| title / instruction | 120 / 1,000 characters |
| notification summary | 500 characters |
| notifications / history retained | 500 / 1,000 (newest kept) |
| condition parameters | 8, each ≤ 200 characters; value key ≤ 128 |
| concurrent runs / dispatch per tick | 2 / 10 |
| run timeout | 5 minutes (≥ the router's 240 s worst case) |
| overdue threshold (one-time, exact) | 1 hour |
| minimum interval | 1 hour |
| dedup window | 7 days, 5,000 keys |

## Guest isolation

Every `/desktop/v1/proactive/*` route uses `owner_bridge_runtime`: while Guest Mode is
active it is refused **before** the body is used and before the service is reached. A
guest can neither read tasks or notifications nor create, update, delete or run
anything, and does not inherit the owner's workflows.

## Audit

Metadata only: task id, task type, timing mode, trigger, start/end time, result,
failure, change kind, reason code, whether a notification was created, permission
outcome and a safe provider id. Never the title, instruction, prompt, summary, email or
document content, a secret or a token. The desktop activity log is equally
content-free.

## Desktop

An **Automations** section (Active, Scheduled, Watching, Notifications, History) in the
existing design. Each task shows its title, type, schedule, timezone, enabled state,
next and last run, last result and, for watches, the cooldown; actions are enable /
disable, edit, delete (confirmed) and run now. A **Turn scheduling on/off** switch
sits in the header; while scheduling is off a notice says that nothing runs, not even
"Run now". The notification inbox shows the task,
time, safe summary (plain text), why Sam surfaced it and any suggested next step,
which is never executed; the only inbox actions are mark read and dismiss.
Native OS notifications are deliberately **not** included in Phase 15 (in-app inbox
only).

## Separation

`sam.proactive` imports nothing from Memory, Knowledge, Professional, MCP, Computer
Control or Coding (a test enforces it). It never writes to Memory or Professional
Intelligence; the Professional observer only reads a count through the Professional
service's own permission check.

## Known limitations

1. **In memory only.** Tasks, notifications, watch state and history do not survive a
   restart. Restart persistence needs a separate, reviewed storage-security design.
2. **Scheduler off by default.** Nothing runs until the owner explicitly enables
   scheduling (the owner-only Automations switch, or `PROACTIVE_SCHEDULER_ENABLED=true`
   in trusted local configuration). The switch resets to off on every restart.
3. **PRIVATE summaries are unsupported in Phase 15.** A summary task cannot be created
   or edited to PRIVATE, and nothing private is sent to a provider for one.
4. **In-app notifications only.** There is no native OS notification integration; the
   inbox is visible only inside Sam.
5. **Python cannot forcibly terminate a running worker thread.** Hard cancellation
   does not exist here; a timed-out run is cancelled cooperatively.
6. **A dependency that violates its own finite timeout and hangs forever:**
   * keeps its task **in flight**, so the task cannot run again;
   * keeps its **concurrency slot occupied** (with the default 2 slots, two such hangs
     stop all proactive runs);
   * needs a **process restart** to recover.

   This is **fail-closed availability degradation, not authorization escalation**: the
   hung worker can never write a result, and its daemon thread does not block shutdown.
7. **PROACTIVE EXECUTE is Phase-15-safe only.** The bootstrap grant authorizes only
   evaluation, read-only observation (which still needs the observed domain's current
   permission), safe ModelRouter summarization and a local notification. It does
   **not** authorize SEND, DELETE, PUBLISH, APPROVE, email sending, calendar mutation,
   MCP side effects, Computer Control, Coding execution, job applications or recruiter
   contact.
8. **Phase 16 MUST NOT reuse PROACTIVE EXECUTE** as authorization for any career, job
   or PhD side effect. Phase 16 needs its own resources, actions and confirmed
   workflows.

Other limitations:

* The private-content pattern check is defence in depth only (provenance decides
  privacy); it cannot recognise every kind of private prose.
* Condition watches are limited to the built-in trusted observers; external sources
  (email, calendar, web) are not wired in this phase.
* Chat is not wired to `propose_from_model` yet (no agent tool loop).
* Relies on the operating system's timezone database (`zoneinfo`); `tzdata` is not a
  locked dependency.
* No live provider was used to build or test this phase; tests use fakes and a
  manual clock.
