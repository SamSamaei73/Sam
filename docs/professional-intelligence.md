# Professional Intelligence (Phase 14)

Sam's **Professional Intelligence** layer (`src/sam/professional/`) is an
evidence-backed model of the owner's professional profile: skills, employment,
projects, education, publications, research, technologies, achievements and career
preferences. Every fact about the owner is traceable to a trusted source, and Sam
reports how strongly the evidence supports it, with no invented claims.

It is **not** the career or application agent. Nothing here searches for jobs,
applies, sends email, rewrites a CV or automates LinkedIn; that is a later phase
(see "Phase 16 integration").

**Status:** approved after independent review. Storage is **in-memory only**.

## Architecture

```
owner-selected file (bytes)
  → PermissionEngine (PROFESSIONAL / WRITE)
  → Phase 7 Knowledge pipeline (parse, chunk, secret detection)   [reader.py]
  → deterministic extraction                                       [extract.py]
  → optional model candidates (UNTRUSTED)                          [candidates.py]
  → schema + provenance validation
  → ProfessionalRepository (atomic commit)                         [repository.py]
```

| Module | Role |
|---|---|
| `models.py` | `ProfessionalClaim`, `EvidenceRef`, `SourceRecord`, `Conflict`, enums, bounds |
| `vocabulary.py` | the trusted skill vocabulary; the only thing that mints canonical skill ids |
| `timeline.py` | deterministic date parsing and experience arithmetic |
| `evidence.py` | evidence strength, conflict detection, the skill evidence graph |
| `profile.py` | read-only snapshot; every derived value is computed here from evidence |
| `claims.py` | `ProfessionalClaimPolicy` |
| `matching.py` | requirement-to-evidence matching |
| `gaps.py` | factual profile-gap analysis |
| `repository.py` | `ProfessionalRepository` protocol and `InMemoryProfessionalRepository` |
| `service.py` | `ProfessionalService`, the one Sam-owned entry point |
| `tool.py` | the model-facing, read-only boundary |
| `audit.py` | metadata-only audit events |
| `safety.py` | identifier scrubbing, sensitivity cues, secret detection (Knowledge's) |

The desktop bridge (`sam/desktop/professional_api.py`) exposes five owner-only routes
under `/desktop/v1/professional/`: `profile`, `ingest`, `review`, `remove`, `query`.

## Trust boundaries

* **PermissionEngine is the authorization authority.** Professional Intelligence has
  its own `PermissionResource.PROFESSIONAL` (never `KNOWLEDGE`):

  | Action | Operations | Risk |
  |---|---|---|
  | READ | search, evidence lookup, lists, timeline, gaps, sources | LOW |
  | WRITE | ingest or refresh an owner-selected source | MEDIUM |
  | UPDATE | confirm or reject a candidate, resolve a conflict, set a source's privacy class | MEDIUM |
  | DELETE | remove a source and its derived evidence | HIGH, **always confirmed** |

  Any other action on the resource is unclassified and denied. A `KNOWLEDGE` grant
  never authorizes any of them. The model cannot grant itself any of them.
* **The model can read, and only read.** `ProfessionalReadTool` exposes eleven read
  operations. It has no method that ingests, verifies, confirms, rejects, resolves,
  removes or changes privacy, rejects any operation not on its list, and accepts no
  path, SQL, expression or principal. It cannot rewrite a CV, apply for a job or
  send email, because none of those exist in this phase.
* **Owner only.** Every desktop route uses `owner_bridge_runtime`: while Guest Mode
  is active it is refused **before** the request body is processed, before any file
  is decoded and before the service is reached. Guest Mode can therefore neither
  read the private profile nor ingest, update, verify, resolve, delete or change
  privacy.
* **Untrusted inputs:** documents, model output, GitHub/LinkedIn content and file
  names are all untrusted data.

## Source model

Supported, owner-approved source types: Master CV, publication, academic transcript,
project documentation, GitHub material the owner exported, LinkedIn export (CSV or
text) and other owner-approved professional documents. The owner picks each file and
its privacy class (public / personal / private); `SECRET` is never a choice.

Sources arrive as **bytes the owner selected** (the UI file picker). Sam takes no path
from the UI, a model or a request. `ProfessionalService.ingest_local_file` reads a
file from inside an explicit root for *trusted* callers only; it rejects absolute
paths, `..`, symlinks anywhere on the path, non-regular files and oversized files.

A `SourceRecord` separates **logical identity** from **content version**:

* `source_id` is the logical source. It is derived from `type + label` on first
  ingest and **never changes** afterwards, even when a refresh arrives under a new
  file name (`ProfessionalService.refresh_source(source_id, …)`).
* `checksum` (SHA-256 of the ingested bytes) and `version` (1, 2, 3 …) describe the
  content currently ingested. A refresh with changed content bumps both.
* `independence_key`, `content_fingerprint` (SHA-256 of the normalized text) and
  `line_fingerprints` (short hashes of normalized content lines, never text) decide
  its source **family** (see *Source independence*).
* the owner-selected `privacy_class`, freshness (fresh ≤ 180 days, aging ≤ 365,
  stale beyond) and ingestion metadata. Only a base name is kept; a name containing
  a path separator, `..` or a control character is refused.

Re-ingesting identical content is **idempotent**: nothing changes, not even the
version. Identical bytes under another name are refused as a duplicate.

### Refresh is one atomic transaction

A refresh (changed content for an existing logical source) is all-or-nothing:

1. the complete replacement is validated, read, secret-checked and extracted into
   **temporary** state; any failure returns here and nothing stored is touched;
2. the whole batch is validated again by the repository before anything changes;
3. **every** old `EvidenceRef` of that logical source is removed (no ghost evidence
   survives, whatever its checksum), and the new version's evidence is inserted;
4. claims left without evidence are dropped; owner confirmations whose attestation
   was anchored in the old version are re-anchored on the new version's evidence for
   the same claim, or lapse if the new version no longer says it;
5. any exception during the commit restores the exact previous state.

Other sources' evidence is never changed. Strength, conflicts, skills, publications,
education, timeline and gaps are not cached: they are recomputed from the current
evidence on every read, so nothing from a superseded version can resurface.

## Claim and evidence model

A claim never owns a single source:

* `ProfessionalClaim`: `claim_id` (deterministic from category and key), `category`,
  `canonical_statement`, the category's sensitivity floor, timestamps. Only the
  claim's **identity** (employer + title, degree + subject, the skill id) is stored on
  it. Nothing derived (strength, review, acceptance) is stored on a claim.
* `EvidenceRef`: `evidence_id`, `claim_id`, `source_id`, `source_type`,
  `source_location`, `evidence_reference` (a short scrubbed excerpt, at most 240
  characters, shown to the owner only), `nature`, the sensitivity of what it says,
  and what it *asserts* (dates, classification …). An owner attestation also names
  its `basis_evidence_id`. A claim may have many.
* `ClaimReview`: the owner's decision about a claim, stored beside it.
* `SourceRecord`: as above.

Categories: education, employment, project, skill, technology, certification,
publication, research, achievement, responsibility, domain experience, language,
portfolio, career preference. Each category has an **attribute allowlist**; a student
number, email or account number is not an attribute of anything and is rejected.

Nothing is prepopulated. The profile starts empty and is built only from ingested
sources; a fact mentioned in conversation is not a fact.

### Evidence, strength and review are independent

Three separate vocabularies, never folded into one:

| `EvidenceNature` | Meaning |
|---|---|
| `EXPLICIT_SOURCE` | an owner-selected document states it (**documentary**) |
| `INFERRED_RELATIONSHIP` | a rule-derived relationship (a dependency manifest, a publication topic), never an explicit fact |
| `OWNER_ATTESTATION` | the owner vouched for it through the review workflow; names the evidence reviewed; **never documentary** |
| `MODEL_CANDIDATE` | a model suggestion; always untrusted |

| `EvidenceStrength` | Rule (documentary evidence only) |
|---|---|
| `CORROBORATED` | `EXPLICIT_SOURCE` evidence from **two or more independent source families** |
| `SINGLE_SOURCE` | `EXPLICIT_SOURCE` evidence from exactly one family |
| `NONE` | no documentary evidence |

| `ClaimReviewState` | Meaning |
|---|---|
| `UNREVIEWED` | the owner has not decided |
| `OWNER_CONFIRMED` | the owner confirmed it (an `OWNER_ATTESTATION` exists) |
| `OWNER_REJECTED` | the owner rejected it; it is removed and never re-created |

A claim is **accepted** (presentable as a fact) iff it is not rejected and it has
trusted evidence: documentary strength of at least `SINGLE_SOURCE`, **or** the
owner's confirmation backed by an `OWNER_ATTESTATION` record. Invariants:

* documentary evidence exists regardless of review; confirming a documented claim
  adds an attestation and changes nothing else (strength, provenance);
* a model candidate stays untrusted until the owner reviews it;
* confirmation always creates an `OWNER_ATTESTATION` whose `basis_evidence_id` and
  location are the evidence the owner reviewed, atomically with the review;
* **no accepted claim ever rests on zero trusted evidence**: a review without an
  attestation accepts nothing, and a confirmation lapses (back to `UNREVIEWED`) when
  the source its attestation is anchored in is removed or no longer states it;
* an owner-attested claim with no document behind it is accepted with strength
  `NONE`, never `SINGLE_SOURCE` or `CORROBORATED`.

### Source independence

`CORROBORATED` needs genuinely independent **source families**, decided by
deterministic code only (`sam.professional.lineage`: no model, no embedding, no
similarity service). Two sources are one family when any of these holds:

* they share an `independence_key`: every `MASTER_CV` is one logical document (so CV
  v1 and CV v2 are one family), likewise every `LINKEDIN_EXPORT`; any other source's
  key is its own logical source id;
* their normalized text is identical (`content_fingerprint`): the same content under
  another file name, source type, case or line endings;
* they share at least 60 % of the smaller one's content lines (and at least three):
  a lightly edited copy.

Families are the union of these relations over the **current** sources, recomputed on
every read, so they never depend on ingestion order, and removing a source
recomputes every family it touched. A source type alone never makes two sources
independent.

**Deleting a source removes only that source's `EvidenceRef` records** and recomputes
strength: a claim corroborated by two families drops to `SINGLE_SOURCE`; a claim only
the deleted source supported disappears; another source's evidence is never touched.

### Skill normalization and the evidence graph

`vocabulary.py` maps aliases to one canonical id (`JS`→JavaScript, `TS`→TypeScript,
`ReactJS`→React, `Fast API`→FastAPI, `Gen AI`→Generative AI, `LLM`→Large Language
Models, `torch`→PyTorch …). All-caps acronyms match case-sensitively so ordinary
words never match. **Only the vocabulary mints canonical skills.** A model may
propose an unknown skill; it gets no canonical id, is never accepted and can be
rejected but never confirmed.

The relationship model is `Skill → EvidenceRef → (project | employment | publication |
education) → Source`, in memory (`SkillEvidenceGraph`), built from
`EvidenceRef.context_claim_id`. It needs no graph database and is shaped for a future
Graph RAG to index.

### What does not verify a skill

* A dependency manifest, an infrastructure file (`requirements.txt`, `package.json`,
  Kubernetes YAML …) yields `INFERRED_RELATIONSHIP` only.
* A GitHub README states the *technology* of a project explicitly but yields
  `INFERRED_RELATIONSHIP` for the *skill*: a repository mention does not prove authorship.
* Skills and topics inside a publication are `INFERRED_RELATIONSHIP`: being an author does not
  mean the owner personally did that work.

## Conflicts

Conflicts are first-class and **preserved, never merged**. Only structured facts can
conflict (dates, classification, institution, DOI, year, the owner's author
position). Descriptive text (authors, venue, abstract, modules) is presentation, not
a contradiction; the value shown is chosen by a fixed source authority order and is
never a "more favourable" pick.

Different dates for the same job are two evidence paths under one claim. Two sources
that give different titles for the same employer over overlapping periods are a
role-overlap conflict. The owner resolves one explicitly (`UPDATE`); a model can
identify a conflict but cannot resolve it. While unresolved, no value is chosen and
the affected job is excluded from experience totals.

## Timeline and experience arithmetic

`timeline.py` is pure code with no model. Dates canonicalize to `YYYY-MM`, `YYYY` or
`present` (using the injected clock). Duration is whole months; overlapping intervals
are **merged, so no month is counted twice**; a conflicted or incomplete interval is
excluded and reported. Years are `months // 12` and are **never rounded up**. A
year-only date uses a conservative bound wherever a lower bound is needed. Experience
for one skill is the union of the resolved intervals of the roles and projects its
evidence sits in.

## Claim safety

`ProfessionalClaimPolicy` checks a first-person statement against the evidence before
Sam states it. Protected: years of experience, employer, job title, degree, grade,
certification, publication, authorship position, contribution, leadership, quantified
achievement, expertise, salary, visa/work authorization and clearance. A protected
claim with no trusted evidence is `UNSUPPORTED`. Specifically:

* "N years" is supported only if the resolved timeline covers N whole years
  (conservative bound); it is never estimated by a model.
* "Expert" needs `CORROBORATED` evidence (or the owner's own confirmation).
* Authorship position needs explicit evidence; **a specific contribution needs a
  contribution statement**. Authorship alone never implies one.
* `NOT_CHECKABLE` means no protected pattern matched. It does **not** mean verified.

## Requirement matching

`evidence_for(requirement)` returns `MATCHED`, `PARTIALLY_SUPPORTED`, `NOT_SUPPORTED`
or `UNKNOWN` with the matched claims, skills, projects, employment, research and
education evidence, provenance and the **unsupported aspects**. Recognised aspects:
named skills, "production" (needs employment evidence, not only a project), years,
degree level, publications and leadership. An inferred or unverified skill is
reported as such and does not match. Text it cannot recognise is `UNKNOWN`, never
guessed.

## Privacy

Sources carry an owner-selected class; a claim's effective sensitivity is the highest
of its intrinsic floor (salary, visa and clearance text is `PRIVATE`) and its
sources' classes. The Phase 13 router applies `PrivacyClass` to any provider call:

* `PUBLIC` (a published paper) follows normal routing.
* `PERSONAL` (a CV) goes to Gemini Free only if the owner allowed it.
* `PRIVATE` is denied to Gemini Free by default.
* `SECRET` is never ingested and makes **zero** provider calls.

For anything that may reach a model (`ProfessionalReadTool`), `PRIVATE` claims are
withheld and only counted, and aggregates (timeline, experience, gaps) exclude
`PRIVATE` evidence; whether the owner stated a salary or visa is itself withheld.
The tool never returns raw document text: not an evidence excerpt, not a paper's
abstract, methods, findings or limitations, not a project description, and not the
contribution sentence naming the owner (only whether one exists). It returns
statements, structured attributes and where each fact came from.

Secret detection reuses Knowledge's detector (API keys, passwords, private keys,
tokens, owner-proof, step-up secrets, biometric and speaker material). A source that
contains one **fails closed before extraction**; nothing from it is stored or sent.
Transcript identifiers (student and account numbers), emails and phone numbers are
scrubbed from every reference, dropped from provider prompts and never stored, and
extraction copies only allowlisted attributes.

## Model candidates (optional, untrusted)

The owner may ask a model to suggest extra items for a source (off by default). It
runs through the Phase 13 `ModelRouter` with the source's class. The reply is
untrusted JSON: unknown fields (a date, a verification state) are ignored and
counted; each candidate must **quote the source verbatim** (provenance validation);
accepted candidates carry `MODEL_CANDIDATE` evidence only, so they are not
accepted whatever the model says, and cannot change a date or a classification. A
provider refusal or failure changes nothing already stored. The owner can confirm a
canonical candidate (an `OWNER_ATTESTATION` naming it; strength stays `NONE`) or
reject it (it is not suggested again).

## Owner professional identity

Who "the owner" is in an author list comes only from trusted local configuration
(`OWNER_NAME`, `OWNER_NAME_ALIASES`), frozen at startup as
`OwnerProfessionalIdentity(canonical_name, approved_aliases)`:

* normalization is deterministic only (Unicode NFKD, accents removed, case folded,
  punctuation to spaces);
* an author matches only if the **whole** normalized author equals the canonical
  name or an explicitly approved alias: no surname-only match, no guessed initials,
  no fuzzy, embedding or model matching;
* every name and alias needs at least two words; an invalid configuration disables
  matching entirely (fails closed) rather than loosening it;
* no match, or more than one matching author in one list, is **UNKNOWN**: no
  position is recorded;
* a contribution sentence is attributed only when it names the owner exactly.

Model output cannot add an alias (unknown candidate fields are ignored), no
operation, tool or route can change the identity, and Guest Mode is refused on every
professional route.

## Relationship to Knowledge and Memory

Three separate domains:

* **Knowledge** (Phase 7) parses and retrieves reference documents. Professional
  Intelligence reuses only its **pure pipeline** (`run_ingestion_pipeline`: format
  check, parsing, secret detection, chunking) through a narrow `DocumentReader`. It
  does not use Knowledge's store, index or permissions, and never copies a raw
  document into a Knowledge collection. Chunks are re-assembled into their original
  segments so a line keeps its page, section and paragraph plus its own character
  range, and that provenance reaches `EvidenceRef.source_location`.
* **Memory** (Phase 4) is never written. `sam.professional` imports nothing from
  `sam.memory` (a test enforces it) and no professional fact is copied into Memory.
* **Professional Intelligence** stores structured evidence, not documents: the only
  text kept is the bounded, scrubbed per-line excerpt.

## Audit

Events are metadata only: operation, principal id, permission outcome, status,
source and claim ids, and counts from a closed set. There is no free-text field, so a
CV line, transcript text, a private note, a file name, evidence text or a secret
cannot be recorded. The desktop activity log is equally content-free.

## Desktop

A **Professional** section (Overview, Skills, Experience, Projects, Research,
Publications, Education, Evidence, Profile gaps, Sources) in the existing design.
Every claim shows its strength, sensitivity and provenance; conflicts offer both
values and let only the owner choose; sources show freshness, privacy, accepted
items, pending suggestions and conflicts; removing a source uses the existing
confirmation dialog. There is no "verify" button, and no identifier is displayed.

## Limitations

* **In memory only.** Nothing survives a restart; there is no persistence backend.
* Deterministic extraction is heuristic and English-oriented. Anything ambiguous
  (for example a job line whose title and employer cannot be told apart) is skipped
  rather than guessed, so some real facts will not be extracted without model help
  or a cleaner source. Free-form CV layouts may yield fewer claims than a structured
  export.
* The vocabulary is a curated list, not the whole world of skills; a skill outside
  it can only ever be an unverified, unconfirmable suggestion until it is added to
  the vocabulary in code.
* GitHub and LinkedIn are supported only as material the owner exports and selects.
  Sam does not log in, scrape or call either service.
* Two roles at one employer whose titles differ across sources appear as a conflict
  for the owner to resolve; a promotion inside one source is kept as two roles.
* Claim-safety detection is pattern-based and conservative: it flags, it does not
  certify free-form prose.
* The agent tool is a Sam-owned boundary; `AgentCore` is still a single-turn call
  with no tool loop, so the model is not yet handed the tool.
* No live provider was used to build or test this phase, and no real CV, transcript
  or private professional document is used anywhere in the tests.

## Phase 16 integration

Phase 16 (career and PhD agent) is expected to consume this layer through
`ProfessionalService` and the read boundary: `evidence_for` for job and PhD
requirement matching; verified claims and their provenance for CV tailoring, cover
letters and application evidence packs; publications and research topics for
supervisor and proposal work; the timeline for deterministic experience statements;
and `assess_claim` as a gate so no generated document states an unsupported protected
claim. It will need its own permissioned, confirmed workflows for anything that
writes or sends; none exists in this phase.
