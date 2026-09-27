# Career & PhD Agent (Phase 16)

Sam's **Career & PhD Agent** (`src/sam/career/`) helps the owner find, track and
prepare job and PhD applications from **evidence the owner has already confirmed**
in Professional Intelligence (Phase 14). It drafts; the owner decides.

**Review first, always.** Career may read listings, check evidence, draft CVs,
cover letters, research statements and outreach, and remind the owner about
deadlines and follow-ups. It never submits an application, sends a message,
uploads a document, creates an account or answers a legally binding question on
its own. Every consequential step is a HIGH-risk action that needs a fresh
owner confirmation bound to the exact content being sent.

**Status:** approved after independent review (including the remediation for
at-most-once SUBMIT/SEND, unknown outcomes, immutable manifests and
destination trust). Storage is
**in-memory only** (no SQLite, no files, no cloud). The desktop runtime
configures **no** submission adapter, **no** e-mail tool and **no** listing
source adapter, so submission, sending and discovery fail closed.

## Architecture

```
owner-pasted listing / read-only source adapter            [sources.py]
  → normalize_listing (explicit facts only, unknown stays unknown) [opportunities.py]
  → deterministic dedup + official-source canonicalization  [dedup.py]
  → InMemoryCareerRepository                                [repository.py]
  → fit / research alignment via EvidencePort, PaperPort    [matching.py, evidence.py]
  → drafts: CV, letter, research statement, proposal        [documents.py]
      every factual line → ClaimChecker                     [claims.py]
  → questions: classify, safe prefill, sensitive → owner    [questions.py]
  → ApplicationDraft state machine                          [applications.py]
  → owner approval → SubmissionManifest hash                [manifests.py]
  → under one lock: freshness, destination, questions, manifest,
    CAREER SUBMIT confirmation (consumed once), attempt IN_FLIGHT [attempts.py]
  → SubmissionAdapter → ExternalActionResult: verified success / verified
    failure / OUTCOME_UNKNOWN (never retried)
  → metadata-only audit                                     [audit.py]
```

| Module | Role |
|---|---|
| `models.py` | closed enums and immutable records (`CareerOpportunity`, `ApplicationDraft`, `ApplicationDocument`, `DocumentLine`, `Contact`, `OutreachDraft`, `FollowUp`, `CareerPreferences`) |
| `sources.py` | source kinds, priorities, URL safety (`safe_host`), `OpportunitySource` protocol |
| `opportunities.py` | listing normalization; labelled, explicit-only extraction |
| `dedup.py` | deterministic duplicate detection and merge |
| `evidence.py` | narrow read-only ports: `EvidencePort`, `PaperPort` |
| `claims.py` | `ClaimChecker`: is a sentence a factual claim, and is it evidenced |
| `matching.py` | requirement fit and PhD research alignment (statuses, no scores) |
| `documents.py` | CV / letter / statement / proposal builders, `revalidate` |
| `questions.py` | application question classification and safe prefill |
| `contacts.py`, `outreach.py` | evidence-backed contacts; outreach drafts |
| `tracking.py` | follow-up interval, cap and cooldown |
| `applications.py` | state machine, freshness, destination check, manifest and package, adapter contract |
| `manifests.py` | immutable, canonical `SubmissionManifest` / `SendManifest` and their hashes |
| `attempts.py` | `ExternalActionAttempt`, the at-most-once ledger, `ExternalActionResult` and its validation |
| `service.py` | `CareerService`: the only entry point; every call is authorized |
| `repository.py`, `audit.py` | in-memory storage (bounded) and metadata-only audit |

Desktop composition lives outside the domain: `src/sam/desktop/career_ports.py`
adapts Professional Intelligence and the Knowledge layer to the read-only ports;
`career_api.py` / `career_models.py` expose nine owner-only routes.

## Permissions

Career has its own resource, `PermissionResource.CAREER`. It does **not** reuse
`PROFESSIONAL`, `PROACTIVE` or `MCP` permissions.

| Action | Risk | Confirmation | Used for |
|---|---|---|---|
| READ | low | no | overview, fit, alignment, review queue |
| CREATE | medium | no | import listing, create draft, add contact, draft outreach |
| UPDATE | medium | no | edit, answer, approve a document, preferences, track |
| SUBMIT | high | **yes** | submitting an application (new `PermissionAction.SUBMIT`) |
| SEND | high | **yes** | sending an outreach e-mail |
| DELETE | high | **yes** | withdrawing / deleting |

The desktop bootstrap grants CAREER READ, CREATE, UPDATE and DELETE on `career`.
**SUBMIT and SEND are not bootstrapped**: the owner must grant them explicitly,
and each use still needs its own confirmation.

`PermissionAction.SUBMIT` is new in this phase. It is deliberately distinct from
`EXECUTE`, so a grant for "run things" can never be read as "submit things".

## Opportunity model

A `CareerOpportunity` holds only what the listing states. Salary, funding,
deadline, work mode, sponsorship, hiring manager and supervisor are extracted only
from explicitly labelled text; if not stated they stay `None` / `UNKNOWN` and
the UI shows "not stated" or "Unknown". Funding exists only on PhD opportunities.
Closed-listing phrases set status `CLOSED`. A listing containing a secret-looking
value keeps an empty description.

The **application URL** is kept only when the listing comes from an official
source kind (official career page, official ATS, university page, funding page). An "Apply here" link
in an aggregator or a pasted description can never set where an application goes.

## Sources

Source priority is fixed. Jobs: official career page, official ATS, LinkedIn,
Indeed, Glassdoor, other. PhDs: university page, funding page, supervisor page,
academic source, other. Source adapters are
a read-only protocol; Career never scrapes authenticated sessions, never logs in
and never bypasses platform controls. URLs must be `https`, with no userinfo, no
IDN host and no IP host (`safe_host`).

## Deduplication

Deterministic, in order: same external id on the same registrable domain; same
canonical URL; same normalized organization + title + location; description
shingle Jaccard ≥ 0.85 within the same organization. When duplicates merge, the
**official source is canonical**, and the other sources are kept as provenance.

## Professional evidence

Career reads Professional Intelligence only through `EvidencePort`
(`ProfessionalEvidence`), which exposes **owner-accepted claims only** and maps
Phase 14 verdicts (`NOT_CHECKABLE` → unknown). Career never confirms, edits or
creates Professional claims. Knowledge is read through `PaperPort`
(`KnowledgePapers`), an exact normalized title/name match.

## Fit analysis

Each requirement is `SUPPORTED`, `PARTIALLY_SUPPORTED`, `NOT_SUPPORTED` or
`UNKNOWN`, with the claims that support it and the gaps. **There is no overall
score or percentage.** PhD research alignment per topic is `SUPPORTED_ALIGNMENT`,
`PARTIAL_ALIGNMENT`, `NO_EVIDENCE` (evidence was read, nothing matched) or
`UNKNOWN` (the evidence source could not answer).

## Claim safety

`ClaimChecker` decides whether every factual sentence in a draft is evidenced:

- metrics and numbers must appear verbatim in the evidence;
- a claim that a paper was read needs a Knowledge record for that paper;
- a publication contribution needs the claim's explicit `owner_contribution`
  attribute; a first-author claim needs `owner_author_position == "1"` (this is
  stricter than Phase 14's generic contribution evidence and is Career-only);
- third-party interest ("Professor X is interested in my work", "the hiring
  manager wants…") is **always** flagged: Career never states supervisor or
  employer interest;
- otherwise Phase 14 `assess_claim`, then `evidence_for`.

Unsupported lines are kept but **flagged**; a document with flagged or
unresolved lines cannot be approved.

## CV tailoring and cover letters

The CV is built from canonical renderings of accepted claims: titles, employers
and dates are copied exactly (`render`). Tailoring only reorders and selects; it
never rewrites a dated line. `revalidate` rejects owner edits that change a dated
line, introduce a metric not in evidence, or add an unverified CV line.

Cover letters separate **FACT** lines (checked, evidence-linked) from
**MOTIVATION** lines (owner-written or, optionally, drafted by a
`MotivationWriter`, then still passed through the checker). The writer receives
minimal disclosure (role, organization, skill names), never the CV. The desktop
runtime configures no writer, so no CV, transcript or private professional text
reaches an external model.

## Sensitive questions

Questions are classified. Salary, notice period, relocation, work
authorization, sponsorship, disability, demographics, criminal history, legal
attestations, conflicts / non-competes, references, privacy consent and
availability are `OWNER_REVIEW_REQUIRED`: never inferred, never prefilled
from preferences, never reused across applications. Only contact details
(`SAFE`) and questions answered by supported claims (`EVIDENCE_BACKED`) are
prefilled. Answers are never written to the audit.

## Application state machine

`DISCOVERED → REVIEWED → DRAFTING → READY_FOR_OWNER_REVIEW →
APPROVED_FOR_SUBMISSION → SUBMITTING → SUBMITTED`, with the side states
`NEEDS_OWNER_INPUT`, `BLOCKED`, `SUBMISSION_FAILED`, `EXPIRED` and `WITHDRAWN`.
`OUTCOME_UNKNOWN` follows `SUBMITTING` when the result cannot be proven.
`SUBMITTED` is reachable only from `SUBMITTING`, or from `OUTCOME_UNKNOWN` by
trusted reconciliation. Transitions
come from one table (`TRANSITIONS`). Any edit after approval returns the draft
to review. **No model output can set `SUBMITTED`**: only a verified receipt from
the adapter whose binding equals the approved binding can.

## Submission manifest and confirmation binding

Approval builds an immutable **`SubmissionManifest`** (`manifests.py`) of the
exact external payload and stores its hash:

- action, opportunity id, draft id, draft version;
- the destination: canonical host and canonical application URL (plus a form
  version slot, empty until an adapter can report one);
- every document: id, kind, version and sha256 of its content;
- every answer: question id, sha256 of the question text, sha256 of the answer.

Canonicalization is deterministic: strings are Unicode-NFC normalized,
documents and answers are sorted by id, JSON is written with sorted keys and no
spacing, and floats are refused. Timestamps, review flags and storage order are
not part of it. The raw manifest is never logged; the audit records only
`manifest_hash`.

The SUBMIT confirmation's scope is
`career/opportunities/<opp>/drafts/<draft>/v<version>` and its target is
`manifest:<hash>`. The PermissionEngine matches both exactly, consumes a
confirmation once, and expires it. Any meaningful change (an answer such as
salary, the CV, the cover letter, a removed document, the destination) makes a
new manifest hash. Every edit also creates a new draft version and clears the
approval: no code path modifies a reviewed draft in place while keeping its
approved manifest. Submit recomputes the manifest from current state and
refuses (`package_changed`) if it differs from the approved one, and checks
that the package it hands the adapter matches the manifest byte for byte.

## Order of checks before any side effect

Under one lock, in this order: (1) freshness (verified within 72 h, open, not
past the deadline, unchanged checksum); (2) destination; (3) unresolved
questions and flagged lines; (4) the manifest; (5) permissions; (6) the
confirmation binding; (7) one-time consumption of the confirmation; (8) the
attempt is recorded `IN_FLIGHT` and the draft becomes `SUBMITTING`. Only then,
outside the lock, (9) the adapter is called. A validation failure never
consumes the confirmation. For SEND both CAREER SEND and GMAIL SEND are
consumed before dispatch; if either is refused nothing is dispatched.

## At-most-once external actions

Every SUBMIT or SEND is one `ExternalActionAttempt` (`attempts.py`) with a
Sam-generated `attempt_id`, the action, opportunity, item, item version,
manifest hash, timestamps, state and (when verified) the receipt id. The ledger
allows at most one *blocking* attempt per item: while an attempt is
`IN_FLIGHT`, `OUTCOME_UNKNOWN` or `VERIFIED_SUCCESS`, any further request for
that item (a double click, a concurrent call, a client or network retry, a
duplicate API request, a scheduler racing the owner, a replayed confirmation)
is answered from the ledger (`already_in_flight`, `outcome_unknown`,
`already_done`) without touching the adapter, and without consuming a
confirmation. `submit(..., attempt_id=...)` / `send_outreach(..., attempt_id=...)`
only report on an existing attempt; they never dispatch. Every other state
change on a draft or message is serialized with the same lock, so nothing can
interleave with a reservation or a completion.

## Outcomes and reconciliation

Adapters return an `ExternalActionResult`: `VERIFIED_SUCCESS` (a trusted
receipt proves it happened), `VERIFIED_FAILURE` (it provably did not happen,
for example refused before dispatch) or `OUTCOME_UNKNOWN`. Sam believes a
result only if it is an `ExternalActionResult` for exactly this attempt, item,
opportunity and manifest; a success also needs a well-formed receipt id and the
trusted final destination. Anything else (an exception, a timeout or
connection reset after dispatch, a lost response, a malformed or foreign
receipt, "success" text) is **`OUTCOME_UNKNOWN`**.

`OUTCOME_UNKNOWN` is never retried automatically, never becomes `SUBMITTED`
and is never treated as a safe failure. Edits, answers, approvals and expiry
are refused, the review queue shows "Outcome unknown … Sam will not retry",
and the item leaves that state only through:

- **trusted reconciliation** (`reconcile_submission` / `reconcile_outreach`,
  CAREER UPDATE): a read-only lookup through the adapter; a matching verified
  success gives `SUBMITTED` / `SENT`, a matching verified failure gives
  `SUBMISSION_FAILED` / `SEND_FAILED`, anything else leaves it unresolved; or
- **an explicit owner release** (`release_unknown_submission` /
  `release_unknown_outreach`): HIGH risk, CAREER SUBMIT / SEND with a
  confirmation bound to `release:<attempt_id>`. The owner accepts that the
  first attempt may have gone through; the item gets a new version and returns
  to review, so a new attempt needs a new approval and a new confirmation.

A verified failure also clears the approval: a retry is always a freshly
reviewed and confirmed action.

## Adapter contract

`SubmissionAdapter.submit(package, guard)` and `EmailTool.send(message)`
return an `ExternalActionResult`, never a bool; `reconcile(attempt)` is a
read-only lookup. The package holds the attempt reference, the destination,
the approved documents' bytes with ids, versions and hashes, the answers and
the manifest hash; no file paths. The model cannot produce a result: results
come only from the configured adapter object, and a string, dict or bool is
rejected as malformed.

## Destination trust and redirects

The destination is the opportunity's `application_url`, which only an official
source (official career page or ATS, university or funding page) can set. An
official page that points to an ATS on another domain is trusted because of
that provenance; nothing a page or model says at submission time can replace
it, and `submit` takes no URL. `canonical_destination` accepts only HTTPS with
an ASCII host (no IDN or punycode), no userinfo, no IP literal, the default
port and no backslash, whitespace or control characters; `javascript:`,
`file:`, `data:` and other schemes are refused. `DestinationGuard.allows`
accepts exactly the trusted host. A future HTTP or browser adapter must
disable redirects or ask the guard before following every hop; a success whose
reported final destination is not the trusted host is `OUTCOME_UNKNOWN`
(`untrusted_final_destination`), never `SUBMITTED`.

`CAREER_LINK_DATA` in the Tauri shell is data only: the shell has no opener,
shell, browser or HTTP plugin; the only HTTP client talks to the constant
loopback backend; Career commands only forward validated fields to their fixed
backend route; the Career view renders links as text (no anchors, no
`window.open`). Tests enforce each of these.

## Sensitive answers

Salary, sponsorship, work authorization, disability, demographic,
criminal-history and legal attestation answers are owner-supplied structured
data. They are used only in the approved submission manifest and package. They
never enter a model prompt (a `MotivationWriter` receives only the opportunity
and skill names), the audit, logs, or Professional evidence.

## Contacts and outreach

A contact needs a full name, and the name, organization and (if any) e-mail
must appear in the quoted evidence. **Career never guesses an e-mail address.**
Outreach is drafts only. E-mail is the only sendable channel. Sending needs
CAREER SEND *and* the e-mail tool's own permission (GMAIL SEND on
`mail/outbox`), both bound to the `SendManifest` hash: channel, outreach id and
version, opportunity, contact id, the canonical recipient address, the subject
and body hashes and any attachment hashes. A changed recipient, subject or body
invalidates both confirmations. Both are requested before either is consumed,
and both are consumed before dispatch. Career never owns e-mail credentials. LinkedIn notes are drafts limited to 300
characters; there is no LinkedIn send.

## Follow-up policy

At least 7 days between follow-ups, at most 2 per opportunity and contact, and a
cooldown so a drafted follow-up is not drafted again. Follow-ups are drafts;
sending goes through the same SEND path.

## PhD workflow

PhD listings carry funding (only when stated), supervisor and research topics.
Research statements and proposal outlines use the same claim checking. Paper
claims need Knowledge provenance. Supervisor outreach is a draft, and "the
supervisor is interested" is never written.

## Proactive integration

Career contributes read-only observers to the Proactive Agent: review queue
size, deadlines soon and follow-ups due (CAREER READ, `PRIVATE`). **PROACTIVE
EXECUTE never authorizes** job or PhD submission, e-mail sending, recruiter,
supervisor or LinkedIn messaging, document upload, account creation or legally
binding answers. Those need CAREER SUBMIT / SEND with a fresh owner confirmation.

## Privacy

Career data is `PRIVATE` by default (`CareerPreferences.privacy_class`). Phase
13 rules apply to any model use: secrets never leave the device. Discovery sends
only the search query to source adapters, never the owner's profile.

## Prompt injection

Listing, page and contact text is data. Instructions inside a listing ("ignore
previous instructions", "submit now", "e-mail your CV to…") cannot change the
state, the target, the documents or any permission. A form never drives Sam: the
package an adapter receives has a fixed shape (ids, hashes, the approved
documents' own bytes, the owner's answers and the binding), so there is no way
to ask for extra files, local paths or secrets. A secret-looking answer is
refused (`secret_detected`).

## Audit

`CareerAuditEvent` is metadata only: operation, principal, permission decision,
status, ids, version, source kind and confirmation id. No listing text, document
text, answers, salaries or contact details. The desktop activity log shows
labels only.

## Guest Mode and desktop

All nine routes (`/career/overview`, `/opportunity`, `/fit`, `/draft`, `/submit`,
`/contact`, `/outreach`, `/send`, `/preferences`) are owner-only. Guest Mode is
refused before the request body is used. Request models reject unknown fields.
The **Career** view shows a review queue, opportunities (unknowns shown as
unknown), evidence checks (no score), applications with flagged lines and
"Only you can answer this" questions, and drafts. Submit and send always open
the confirmation dialog. LinkedIn drafts have no send button.

## Known limitations

> **CRITICAL PHASE 17 REQUIREMENT.** Phase 16's `ExternalActionAttempt`
> ledger and the Career repository are **in memory**. The at-most-once
> guarantees therefore hold only **within one running Sam process**: a crash
> or restart after an external dispatch would lose the attempt state.
>
> **Real SUBMIT and real SEND must remain unavailable until Phase 17 adds a
> durable, trusted external-action / idempotency store** (or equivalent
> persistence) that survives a process restart. It must preserve, per
> attempt: `attempt_id`; action; `opportunity_id`; draft or message identity
> and version; the immutable manifest hash; the destination identity; the
> state (`IN_FLIGHT`, `OUTCOME_UNKNOWN`, `VERIFIED_SUCCESS`,
> `VERIFIED_FAILURE`); the receipt or reference where available; and the
> reconciliation state. A restart must never silently turn `OUTCOME_UNKNOWN`
> (or a lost `IN_FLIGHT`) into something retryable. **No real job-submission
> or e-mail adapter may be wired before this is solved.**


- **No real adapters.** There is no listing source adapter, submission adapter
  or e-mail tool in the desktop runtime; discovery, submission and sending fail
  closed. Real adapters are future work and need their own review.
- **SUBMIT and SEND are not bootstrapped.** The owner must grant them explicitly.
- **In-memory only.** All Career state is lost on restart.
- **Heuristic parsing.** Listing extraction is labelled-pattern based. Anything
  it cannot read stays unknown rather than guessed, so some stated facts may be
  missed.
- **LinkedIn is drafts only.**
- **`CAREER_LINK_DATA` Rust exception.** The Tauri test forbidding
  destination-like arguments has a reviewed, explicit allowlist for Career link
  *data* fields (`url`, `application_url`, `portfolio_url`, `linkedin_url`), plus
  a test confining them to Career commands. These are stored data, not
  destinations the shell acts on.
- **Reconciliation and release are service-level only.** With no adapter
  configured there is nothing to reconcile, so the desktop view only surfaces
  an unknown outcome; wiring the actions into the UI belongs with a real
  adapter.
- **In-process single flight.** The lock and the attempt ledger are in memory:
  they protect one runtime. A multi-process deployment would need a shared,
  durable ledger. All Career state is lost on restart.
- **A hung adapter blocks its item.** Python cannot forcibly stop a thread; an
  adapter that never returns keeps its attempt `IN_FLIGHT`, which blocks any
  retry for that item (fail closed) until restart.
- **Two-layer SEND consumption.** If the GMAIL confirmation is refused after
  the CAREER confirmation was consumed, nothing is sent and the owner must
  confirm again.
- **Serialized drafting.** Draft-changing calls share the single-flight lock,
  so a slow `MotivationWriter` (none is configured) would delay other Career
  changes.
- **Private summaries.** Career observers produce counts only; a Proactive
  SUMMARY task over Career content is unsupported (Phase 15 PRIVATE-summary
  rule).
- **No motivation writer configured.** Motivation lines are owner-written.
