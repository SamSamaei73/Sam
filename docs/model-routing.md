# Multi-model routing (Phase 13)

Sam reaches every language-model provider through **one trusted, deterministic
router**. The router is **not** an authorization boundary: the PermissionEngine
stays the only authority, and provider output is untrusted text.

```
User -> AgentCore -> RoutedLLMProvider -> ModelRouter
          (task, privacy class, capabilities) -> CostPolicy -> PrivacyPolicy ->
          owner preference -> availability -> ONE provider adapter -> text
text -> Sam's agent loop -> PermissionEngine -> Sam-owned tools / MCP / Computer / Coding
```

Never: user -> external model -> tool. A provider returns text only and can
never execute, approve, grant, enable another provider, or change policy.

## Provider registry (`sam/models/registry.py`)

Trusted and immutable. A provider cannot mark itself free: the cost class lives
in Sam's code. A paid provider **cannot be built as enabled**.

| id | enabled | cost class | auth |
|---|---|---|---|
| `claude_subscription` | yes | `SUBSCRIPTION_INCLUDED` | your Claude Code login |
| `gemini_free` | when `GEMINI_API_KEY` is set | `FREE_TIER` | API key (local) |
| `openai_api` | **never** | `PAID_API` | none read |
| `grok_api` | **never** | `PAID_API` | none read |

OpenAI and Grok are present but operationally absent: their adapters contain no
network, subprocess, credential or SDK code, and no setting can enable them.

## Routing algorithm (`sam/models/router.py`)

1. validate the request (no endpoint/credential/model/cost/permission fields)
2. **SECRET** detection (Sam's existing secret detector plus voice-identity and
   biometric patterns) -> blocked, **zero provider calls**
3. owner topic blocklist (empty by default)
4. required capabilities (`PERSIAN` when the text is Persian)
5. registry, drop disabled providers
6. `CostPolicy` (only subscription/free/local may ever be selected)
7. `PrivacyPolicy` (see docs/privacy.md)
8. owner preferred provider (an ordering only, never eligibility)
9. availability (bounded cooldowns; never a permanent disable)
10. choose at most **2** providers; execute; normalize

Every exclusion has a reason code: `preferred_provider`, `owner_selection`,
`privacy_restricted`, `provider_disabled`, `provider_unavailable`,
`free_quota_exhausted`, `subscription_limit`, `capability_mismatch`,
`secret_blocked`, `refusal_no_failover`, `cost_blocked`, `owner_blocklist`,
`fallback_used`, `no_eligible_provider`.

### Fallback

A fallback is a **new decision**: cost, privacy, capability, owner settings and
availability are all re-checked at fallback time. It happens only after
`UNAVAILABLE`, `AUTH_FAILURE`, `RATE_LIMIT` or `TIMEOUT`, only if the owner
allows free fallback, and never to a paid provider. There is no retry of the same
provider and no loop (max 2 providers per request). A **refusal**, a policy
block, or an invalid response never triggers a fallback.

### Failure categories

`SUCCESS, UNAVAILABLE, AUTH_FAILURE, RATE_LIMIT, TIMEOUT, REFUSAL,
INVALID_RESPONSE, POLICY_BLOCKED, COST_BLOCKED`. A refusal is **not** an outage
and not a rate limit; Sam reports it and does not work around it.

## Claude via your subscription (`providers/claude_subscription.py`)

**Verified against official Anthropic docs on 2026-09-20** (code.claude.com:
headless, cli-reference, authentication, permissions, agent-sdk/overview;
installed Claude Code 2.1.278):

* `claude -p` is the supported non-interactive path and reads the prompt from stdin.
* `--bare` **never reads the subscription login**, so it is not used.
  `--safe-mode` disables CLAUDE.md, skills, plugins, hooks, MCP servers, custom
  commands/agents but keeps authentication. `--tools ""` disables all built-in tools.
* **Authentication precedence** puts cloud-provider variables
  (`CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY`), `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_API_KEY` and `apiKeyHelper` **above** the subscription login, and in
  `-p` mode a present `ANTHROPIC_API_KEY` is always used. Any of them would
  silently switch to paid API billing.
* Under `-p`, project hooks, `.mcp.json` servers, the `env` block and
  `apiKeyHelper` in settings files are used without a trust prompt.
* **Policy caveat:** Anthropic does not allow third-party developers to *offer*
  claude.ai login or rate limits in their products (Agent SDK overview). This
  adapter is for the **owner's own local tool using the owner's own login**. It is
  not a way to give anyone else access. If Sam were ever distributed, replace it.

How Sam enforces "subscription only, model only". Every step below is required;
if any cannot be met, `claude_subscription` is **UNAVAILABLE** and no request is
made. Sam never degrades to weaker isolation.

1. **Deployment gate (`DeploymentMode`).** Trusted configuration
   (`DEPLOYMENT_MODE`, default `owner_local`), never prompt- or model-controlled.
   `claude_subscription` runs **only** in `owner_local`. `multi_user`, `hosted`
   and `distributed` fail closed (registry `enabled=False`, provider `DISABLED`,
   reason `deployment_not_owner_local`, zero processes) because Anthropic does not
   allow third-party products to offer claude.ai login.
2. **Env allowlist.** The child gets only `HOME, USER, LOGNAME, LANG, PATH,
   TMPDIR, TERM, NO_COLOR`. It is never a copy of Sam's environment, so no
   `ANTHROPIC_*`, `CLAUDE_CODE_OAUTH_TOKEN`, cloud, gateway or proxy variable can
   reach it. The known billing/redirect variables are also listed and asserted absent.
3. **Trusted executable only:** an absolute path named `claude`, resolving to a
   regular executable owned by you or root, not world-writable, inside a Claude
   install directory (a symlink named `claude` pointing at `/bin/sh` is refused).
   No path ever comes from a request or a model.
4. **CLI capability check.** Sam runs `claude --version` (must be at least
   2.1.259) and `claude --help` and requires every mandatory isolation flag to be
   advertised, including `--restricted` (2.1.248+) and `--permission-prompts`
   (2.1.259+). (`--max-turns` is print-mode-only and not listed by `--help` in
   2.1.278; it is enforced in argv, and a CLI that rejects it fails the run.) A CLI that lacks any of them is `cli_unsupported`/UNAVAILABLE.
5. **Managed policy fails closed.** `--safe-mode` still applies *managed* policy
   (hooks, MCP, helpers) that Sam cannot disable or observe. Sam only checks that
   the documented managed-policy locations do **not exist** (it never reads them):
   `/Library/Application Support/ClaudeCode/managed-settings.json`, `managed-settings.d`,
   `managed-mcp.json`, `/Library/Managed Preferences/com.anthropic.claudecode.plist`
   (and the per-user variant), `/etc/claude-code/managed-settings.json`. If any is present
   or cannot be checked, the provider is `managed_policy_present`/UNAVAILABLE.
   This deliberately errs toward unavailable: an organization-managed machine
   cannot use this provider until the policy is removed or a future review proves
   it inert.
6. **Exact authentication source (`claude auth status --json`).** An
   `apiKeySource` of `none` is not proof of a subscription (it is also what an
   `apiKeyHelper` or a profile can look like). Before use, Sam runs the official
   CLI's `auth status --json` with the **same sanitized environment and trusted
   executable**, parses **only** the safe fields it understands, and drops the
   rest (email, organization, directories are never stored or shown). It enables
   the provider only on a *positive* match: `loggedIn: true`,
   `authMethod: "claude.ai"`, `apiProvider: "firstParty"`, `subscriptionType` in
   {`pro`, `max`}, and **no unknown fields**. Everything else is rejected:

   | `auth status` shows | classification |
   |---|---|
   | `authMethod: api_key` | `api_key_auth` (also covers `apiKeyHelper` output) |
   | `authMethod: oauth_token` (auth token or `CLAUDE_CODE_OAUTH_TOKEN`) | `token_auth` |
   | `third_party`, or `apiProvider` other than `firstParty` (Bedrock, Vertex, Foundry, gateway) | `third_party_auth` |
   | `loggedIn` false / no method | `not_logged_in` |
   | Console/PAYG, profile or federation, unknown method or unknown extra fields | `ambiguous_auth` |
   | any other plan (free, enterprise, team, missing) | `unsupported_plan` |
   | unreadable, non-JSON, non-zero exit, timeout | `auth_status_unreadable` |

   A request that merely *succeeds* is never treated as proof of a subscription.
   Sam does not read credential files, the Keychain, OAuth tokens, `~/.claude`
   credentials or cookies; it only runs the CLI. The result is cached briefly
   (120 s positive, 30 s negative) and re-verified after any authentication failure.
7. **Isolation argv** (checked by `verify_isolation` immediately before the process
   starts; removing or weakening any item refuses the run):
   `claude -p --restricted --safe-mode --tools "" --disallowedTools "*"
   --strict-mcp-config --permission-prompts none --permission-mode dontAsk
   --disable-slash-commands --max-turns 1 --no-session-persistence --no-chrome
   --output-format stream-json --verbose --system-prompt <fixed text>`.
   `--restricted` loads only managed settings and Sam's own flags (no user,
   project or local settings, hooks, `apiKeyHelper` or `env` block);
   `--permission-prompts none` means no prompt is ever routed to anyone.
   `--dangerously-skip-permissions`, `--allowedTools`, `--mcp-config`, `--bare`,
   `--settings`, `--resume`, `--continue` and `--chrome` are forbidden. Also:
   argv list, `shell=False`, empty private temp working directory, prompt on
   **stdin** (never argv).
8. **Run attestation:** the run's `system/init` must show a subscription
   credential source, **no tools**, and **no MCP servers**, otherwise the output
   is discarded (defence in depth on top of steps 1-7).
9. **Bounds:** stdin ≤ 400 KB, stdout ≤ 1 MiB, stderr ≤ 32 KiB, finite timeout;
   on any failure the whole process group is killed and the temp directory removed.
10. Sam never asks you to paste a credential and never calls a private claude.ai API.

If the subscription hits its limit the provider reports `USAGE_LIMIT`; the router
may use another allowed **free** provider if privacy allows, and **never** the
paid Anthropic API (there is no such provider).

### Billing: what Sam can and cannot guarantee

Sam uses `claude -p` on your logged-in subscription and never uses an API key. It
does **not** rely on the announced separate Agent SDK monthly credit: that credit
is paused, and this document does not describe `claude -p` as consuming one. If
your Claude account has **usage credits / pay-as-you-go extra usage** enabled,
subscription overage may be billed. **For a strict no-extra-charge posture, keep
usage credits disabled on your Claude account.** Sam never enables them. Account
billing is an *external* state that Sam cannot read, verify or cryptographically
guarantee; the controls above prove only that Sam does not use an API key,
token, helper, profile or cloud provider.

**Live evidence (Claude subscription): PASSED.** Claude Code 2.1.278.
`claude auth status` showed the claude.ai `firstParty` login on a Pro plan and the
subscription authentication preflight passed (`available`). The owner-run live
smoke through Sam's constrained adapter with the final argv above (including
`--restricted` and `--permission-prompts none`) returned `RESULT ok: 'pong'` with
the run attestation passing: subscription credential source, **no tools** and
**no MCP servers**. The paid Anthropic API is not used, `PAID_FALLBACK` is OFF, and
this provider operates in `OWNER_LOCAL` mode only.

## Gemini Free Tier (`providers/gemini.py`)

**Verified against official Google docs on 2026-09-20** (pricing, models,
text-generation, rate-limits, terms, API reference):

* Models used: `gemini-3.8-flash` (general; newest stable) and
  `gemini-3.5-flash-lite` (efficient), both listed **"Free of charge"**. The
  earlier suggestion (`gemini-3.7-flash`) is a previous-generation stable model
  and was not the newest.
* Endpoint: `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`
  with `x-goog-api-key`. The Interactions API is recommended for new projects
  (GA June 2026) and `generateContent` is documented as legacy but **fully
  supported**. Sam uses `generateContent` because its request/response schema is
  fully documented; the Interactions raw response schema is not, and Sam's
  Interactions TTS live smoke earlier returned an unexplained HTTP 400.
* **Free-tier data use:** "Used to improve our products: Yes" (free) vs "No"
  (paid); unpaid services may be read by human reviewers, and Google says not to
  submit sensitive, confidential or personal information to them.
* Rate limits are per project; exceeding one returns `429 RESOURCE_EXHAUSTED`.
  Google states a project is **automatically upgraded to a paid tier** once
  billing is set up. Sam never sets up billing and treats 429 as
  "free quota exhausted", with no retry.
* **An API key does not prove an unbilled project.** Because a project is
  upgraded automatically once billing exists, Sam models this as an explicit
  attestation `GeminiFreeAttestation`: `UNKNOWN` (default) or
  `OWNER_ATTESTED_UNBILLED`. While `UNKNOWN`, `gemini_free` is **unavailable**
  (state `unattested`, reason `free_tier_unattested`) and makes **zero network
  calls**; the adapter refuses before any request even if called directly. Only
  you can attest, from Settings, and doing so is a *loosening* that needs the
  step-up secret (Guest Mode cannot; a chat message or model output cannot; the
  request/response models have no such field). It is session-only and resets to
  `UNKNOWN` on restart; withdrawing it needs no step-up. The UI says "Free Tier
  intended / Owner-attested unbilled project" and that **Sam cannot independently
  verify Google Cloud billing state**. Sam never calls Google Billing APIs, never
  requests extra OAuth, and never enables billing.
* **Watch:** the pricing page notes pricing changes for Gemini 3.x models
  effective 2027-01-01. Free-tier availability after that date is unverified.

Adapter properties: pinned HTTPS host, allowlisted model ids only (the URL is
built from the allowlist), `trust_env=False`, `follow_redirects=False`, finite
timeout, bounded request/response, **no retry**, no Google safety-setting
changes (a provider block is a refusal), key only in the request header, never in
a URL, error, `repr`, audit, UI, Memory, Knowledge or Obsidian.

**Live evidence (Gemini Free): NOT CONFIRMED.** The owner ran exactly one Gemini
text smoke with the attestation set to `OWNER_ATTESTED_UNBILLED`. It returned
**HTTP 400** (`category=unavailable`, `code=http_400`, one request). There was **no
retry, no billing was enabled and no paid fallback occurred**. Live Gemini
availability on the current Google project is therefore **not confirmed**, and the
Gemini live smoke did **not** pass. This is accepted as a provider/project runtime
limitation, not a reason to enable billing or paid fallback.

**Runtime behavior on any Gemini error, unavailability or quota condition:** no
retry, no paid fallback, no billing activation; Gemini is simply unavailable and
Sam stays text-only or uses another allowed *free* provider if privacy allows.

The 400 response body was not captured (the adapter deliberately did not read a
non-2xx body). Since then the adapter reads at most 16 KiB of a non-2xx body and
reduces it to **allowlisted** fields only (`http_status`, `error.code`,
`error.status`, an allowlisted `ErrorInfo.reason` such as `API_KEY_INVALID`, and a
sanitized, truncated `error.message`). The key, prompt text, request headers, raw
body, project ids and `error.details` are never exposed or recorded, and the
category and code used for routing are unchanged. The owner-run smoke prints these
fields. A `400 INVALID_ARGUMENT` would indicate a request problem to fix locally,
`FAILED_PRECONDITION` a likely Free Tier/account/region prerequisite (do **not**
enable billing), and `API_KEY_INVALID` a key/project configuration issue.

## Cost policy: `PAID_FALLBACK = OFF`

`CostPolicy` fields (`allow_paid_api`, `allow_paid_fallback`,
`allow_auto_upgrade`, `allow_credit_purchase`) are all fixed `False` and the
constructor refuses `True`. No prompt, model output, provider failure or quota
error can change this. Only `SUBSCRIPTION_INCLUDED`, `FREE_TIER` and `LOCAL` can
be selected. Sam shows safe indicators (Free Tier / Subscription / Paid API
disabled), never a computed price.

**Official facts recorded:** the OpenAI API is billed separately from ChatGPT
subscriptions (a ChatGPT plan includes no API access); the xAI API is a separate
offering with prepaid credits, unaffected by a Grok/X subscription. Sam
automates neither consumer website and reads no credential for either.

## Credentials and endpoints

Credentials are provider-bound and opaque (redacted `repr`/`str`, not
picklable/copyable). There is no generic credential bag. Every cloud adapter
pins its host over HTTPS; no request, model or document can set an endpoint.

## Persian

The router honors `fa`/`en`/`auto`: Persian text requires the `PERSIAN`
capability, so a provider without it is never used just because it is first.
Both current providers are treated as Persian-capable per provider
documentation; Sam has not measured their Persian quality.

## Limitations

* Free-tier and consumer data-use terms are the providers'; Sam cannot read your
  Claude account settings and does not describe any external provider as
  zero-retention.
* No per-request confirmation to send PRIVATE content to Gemini: the owner
  setting (step-up protected) is the only override.
* The Claude adapter depends on Claude Code's CLI flags and `system/init`
  fields; a CLI change fails closed (no text is returned).
* Settings (preferences, blocklist) are in-memory for the session and reset to the
  safe defaults on restart.
