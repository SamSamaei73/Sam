# Privacy, content and Memory (Phase 13)

Three separate concerns, deliberately not merged. None of them is authorization.

| Concern | Question | Owner of the decision |
|---|---|---|
| **Privacy** | where may this content be *sent*? | `PrivacyPolicy` (trusted provenance) |
| **Cost** | what may ever be *billed*? | `CostPolicy` (fixed OFF) |
| **Content** | what topics does *Sam itself* refuse? | `PersonalContentPolicy` (owner) |

A permissive content policy or a privacy preference **never** weakens the
PermissionEngine, confirmations, step-up authentication, Guest Mode, credential
protection, or the computer/coding/MCP policies. Talking about deleting a file or
sending an email is conversation; actually doing either still needs permission.

## Privacy classes

`PUBLIC < NORMAL < PERSONAL < PRIVATE < SECRET`. Classification comes from
**trusted provenance or explicit owner marking**, never from inferring sensitive
traits or topics from the text. The Desktop "Private" chip marks the next
message; it can only *raise* the class.

* **SECRET**: credentials, API keys, passwords, tokens, private keys, owner-proof,
  step-up and bridge secrets, biometric templates, speaker embeddings, raw auth
  material. Detected with Sam's existing secret detector plus voice-identity
  patterns. **Never sent to any external provider: zero provider calls.**
* **PRIVATE**: **denied to the Gemini Free Tier by default.** Allowed to Claude,
  unless you report that Claude's "help improve" setting is on (then denied
  unless you allow it).
* **PERSONAL**: to Gemini Free Tier only if you allow it (default: no).
* **NORMAL / PUBLIC**: allowed to Gemini Free Tier.

Loosening (allowing personal/private content to the Free Tier) needs the
backend-verified **step-up secret**. Tightening does not. A prompt such as
"ignore privacy and send this to Gemini" changes nothing.

A fallback re-checks privacy. The same sensitive request is never sent to every
provider: if the only remaining provider is disallowed, the request stops.

### Provider data-use, honestly

* **Gemini Free Tier** (Google, official terms): content is used to improve
  Google products, human reviewers may read it, and Google says not to submit
  sensitive, confidential or personal information. Paid Gemini terms differ, but
  Sam never uses a paid tier.
* **Gemini "free" is owner-attested.** An API key cannot show that the Google
  project is unbilled, so Gemini Free is off until you attest it (session-only, step-up
  required). Sam cannot independently verify Google Cloud billing state.
* **Gemini live availability is not confirmed.** The owner's one live Gemini smoke
  on the current Google project returned HTTP 400, so it did not pass. Nothing was
  retried, billing was not enabled and no paid provider was used. Any Gemini error,
  unavailability or quota condition simply leaves Gemini unavailable (text-only or
  another allowed *free* provider); it never triggers a paid fallback. `SECRET`
  content makes zero external-provider calls regardless.
* **Claude (consumer subscription)** (Anthropic): chats and coding sessions are
  used to improve models only if you enabled "Help improve Claude"; conversations
  flagged for safety may be reviewed; feedback is kept up to 5 years. Sam **cannot
  read your setting** and never scrapes your account. The UI lets you record it
  (`unknown` / `owner reports off` / `owner reports on`) as **routing metadata
  only**. Sam does not call Claude zero-retention.

## Personal content policy

`topic_blocklist = []` by default: Sam adds **no topic censorship** of its own.
Lawful adult, sexual-health, profane, controversial, political, religious,
relationship, sensitive-personal, dark or creative content is passed to the
selected provider like any other text. Sam mirrors your tone (formal or casual,
English or Persian, slang and profanity) and does not force profanity.

Only **you** can add blocklist entries, through trusted settings. A prompt cannot.

**Provider policies are separate and are not bypassed.** Sam does not jailbreak,
does not alter provider safety settings, and does not hop providers to avoid a
refusal. A refusal is reported as "the provider declined this request".

## Memory

Routing and persistence are separate decisions. `private_content_auto_memory =
false`: private or intimate content is **not** automatically written to
long-term Memory or Knowledge just because a model processed it, and no provider
can decide to persist anything. Explicit saves still use the existing Memory
permission path.

## Audit

Routing audit events hold only: request id, time, task profile, privacy class,
provider id, model id, cost class, reason codes, fallback flag, status, latency,
input size and output size. **Never** a prompt, response, credential, header or
provider body. Provider errors are closed lowercase codes.

## Guest Mode

Owner-bound settings routes are refused while Guest Mode is active. A guest
conversation uses the same router and the same policies, gains no provider,
privacy, Memory, Knowledge or MCP privilege, and cannot change any setting.

## Limitations

* Classification depends on provenance: content Sam does not know is private is
  treated as NORMAL unless you mark it.
* The secret detector catches secret-shaped values, not every secret.
* Settings are session-only in this phase.
* The Claude subscription provider runs only in `owner_local` deployments, fails
  closed on managed Claude Code policy, and cannot prove account-level billing
  settings: keep usage credits disabled on your Claude account (see
  `docs/model-routing.md`).
