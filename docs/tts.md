# Sam Voice Synthesis (Phase 10) — Text-to-Speech with Fish Audio

> **Fish Audio is a synthesis provider, not an authorization or security boundary.**
>
> **Secret-containing text is never sent to Fish Audio.**
>
> **Voice selection, model selection, endpoint selection, credentials, and
> authorization are controlled by Sam, not by the LLM or Fish Audio.**
>
> **Phase 10 does not implement voice cloning.**

Phase 10 adds a provider-independent text-to-speech layer (`src/sam/tts/`) and
Fish Audio as its first real provider. It is **explicit, non-streaming REST
synthesis against a trusted, preconfigured voice**. There is no playback, no
streaming/WebSocket, no cloning, and no automatic synthesis of anything.

```
Text != provider configuration     Text != authorization
Text != model selection            Fish Audio != security boundary
Text != voice selection            TTS output != proof of action
```

## Architecture

```
trusted code (supplies the principal, decides WHEN to speak)
   │
   ▼
TTSAgentBoundary.speak(SpeechProposal{text, voice_profile})
   │
   ▼
TTSGateway.synthesize(SynthesisRequest{principal, text, trusted_voice_profile})
   ├─ 1  resolve the TRUSTED voice profile (unknown/disabled → REJECTED)
   ├─ 2  validate text (size, UTF-8 bytes, control chars, blank)     → REJECTED
   ├─ 3  secret detection: secret-looking text is WITHHELD           → REJECTED
   │        (nothing below runs: no permission record, no key, no network)
   ├─ 4  duplicate request id?                                       → REJECTED
   ├─ 5  policy builds the PermissionRequest (scope from the trusted profile)
   ├─ 6  PermissionEngine.evaluate()        ◄── the only authority
   │        DENY → DENIED   CONFIRM_REQUIRED → CONFIRMATION_REQUIRED
   │        (the provider is never called; an engine failure fails closed)
   ├─ 7  reserve the request id (bounded, at-most-once)
   ├─ 8  AT MOST ONE provider call, Sam-owned timeout, contained errors
   │        └─ FishAudioProvider: resolves its key HERE → pinned HTTPS POST
   ├─ 9  validate the untrusted audio (size, signature); Sam computes the SHA-256
   ├─ 10 emit ONE content-free audit event
   └─ 11 return a normalized SynthesisResult (audio in memory only)
```

`synthesize` never raises and never retries. The layer starts no thread and no
background work, and nothing a provider returns can start another synthesis.

| Module (`src/sam/tts/`) | Responsibility |
|---|---|
| `models.py` | Immutable models, enums, and **every** limit (one place). |
| `errors.py` | Typed errors with fixed, content-free messages. |
| `profiles.py` | `TrustedVoiceProfiles`: the read-only voice catalog. |
| `validation.py` | Pure text validation and secret withholding. |
| `policy.py` | Builds (never decides) the `PermissionRequest`. |
| `credentials.py` | `TTSCredential`, `TTSCredentialProvider`, fake provider, settings bootstrap. |
| `provider.py` | `SpeechSynthesisProvider` protocol and `FakeSpeechSynthesisProvider`. |
| `fish_audio.py` | `FishAudioProvider` — the real adapter (`httpx`, REST only). |
| `audio.py` | Validation of generated audio (MP3 signature, size, digest). |
| `audit.py` | `TTSAuditSink`, in-memory and failing sinks. |
| `gateway.py` | The lifecycle above. |
| `agent_boundary.py` | `TTSAgentBoundary` and `SpeechProposal`. |

## Official Fish Audio API — what was verified

Verified against the official documentation on **2026-09-20**
(`docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech`, plus
Fish's developer guide and product pages). Nothing below is assumed from older
examples.

| Item | Verified |
|---|---|
| Endpoint | `POST https://api.fish.audio/v1/tts` |
| Authentication | `Authorization: Bearer <api key>` |
| Model selection | a **request header** named `model`. Documented values: `s1`, `s2-pro`, `s2.1-pro` (default), `s2.1-pro-free`, `drama-3-preview`. |
| Request body | JSON (or msgpack). Fields include `text` (required), `reference_id` (a voice model id), `format` (`wav`, `pcm`, `mp3` default, `opus`), `temperature`, `top_p`, `chunk_length`, `normalize`, `latency`, `prosody`, `references` (zero-shot samples), and others. |
| Success | HTTP 200, chunked audio stream |
| Documented errors | 401 (no permission), 402 (no payment), 503 (high load), JSON with `status`, `message`, optional `reason` |

**Not documented (so not assumed):** the response `Content-Type`, HTTP 429
behavior, the error-body shape beyond the above, and a maximum text length. Sam
therefore treats the response `Content-Type` as advisory (validating the actual
bytes), handles 429 generically, and enforces its own text limits.

**Notable provider behavior.** Fish's pages state that an omitted or
unrecognized `model` value falls back to its default (`s2.1-pro`). A typo could
therefore silently change which model bills a call, so Sam refuses any model
outside a fixed allowlist (`s1`, `s2-pro`, `s2.1-pro`, `s2.1-pro-free`; the
preview model is deliberately excluded) *before* any request is made.

**What Phase 10 sends** — exactly:

```
POST https://api.fish.audio/v1/tts
Authorization: Bearer <key>          (only place the key exists)
Content-Type:  application/json
model:         <trusted profile's model>
Accept:        audio/mpeg
Accept-Encoding: identity
User-Agent:    sam-tts/0.1

{"text": "<validated text>", "reference_id": "<trusted profile voice>", "format": "mp3"}
```

Nothing else: no `references`, no sampling parameters, no prosody, no
`pronunciation_dictionary`. There is **no** WebSocket/streaming, cloning,
voice-creation, model-listing, upload, or ASR call anywhere in the package.

## Trust boundaries

| Party | Trust | May influence |
|---|---|---|
| Sam's policy, profile catalog, PermissionEngine | Trusted | Voice, model, endpoint, scope, risk, allow/deny |
| Trusted caller (supplies principal, decides when) | Trusted | Who and when |
| The text | **Untrusted data** | Only the words rendered (validated, secret-checked) |
| The LLM / transcript / MCP result / document | Untrusted | The text and a *profile id* from the fixed catalog |
| Fish Audio (response, errors, metadata) | **Untrusted** | Only the audio bytes it returns, after validation |

## PermissionEngine integration

`sam.permissions` gained `PermissionResource.SPEECH_SYNTHESIS` and one policy
row: **`SPEECH_SYNTHESIS / SEND` — MEDIUM, no mandatory confirmation.** Every
other action on that resource is unclassified and therefore denied.

Why a new resource rather than Phase 9's `VOICE`: `VOICE` grants cover *local*
session/utterance handling. Synthesis **transmits text to an external service**.
Reusing `VOICE` would let a grant meant for local voice input silently authorize
data egress, so the two are separate resources and a `VOICE` grant of any
action or scope cannot authorize synthesis (tested, including a `VOICE/SEND`
grant on the exact synthesis scope). `SEND` fits the semantics (data leaves the
process); `MEDIUM` without mandatory confirmation fits routine voice output,
while a grant may still add `always_require_confirmation`.

Scope is `(provider_id, profile_id)` from the **trusted profile** — never from
text, the LLM, or Fish. So a grant for one profile or provider does not cover
another, and principals are isolated by the engine. The confirmation `target`
embeds a digest of the exact text, so a confirmation approved for one text
cannot be consumed for another, for another profile, or twice. DENY → zero
provider calls and the API key is never even resolved; an engine failure fails
closed.

## External-data transmission

Text sent to Fish leaves the Sam process. That is why (1) it needs a
`SPEECH_SYNTHESIS` grant, (2) secret-looking text is withheld, (3) synthesis is
explicit and bounded, and (4) the audit records only metadata. A Fish outage,
key problem, or rate limit yields a generic typed failure and never changes any
authorization.

## Credential handling

> The Fish API key exists only at the provider execution boundary.

- It is loaded **once, at the bootstrap boundary**: `Settings.fish_audio_api_key`
  (a `SecretStr`, from `FISH_AUDIO_API_KEY`), wrapped by
  `credentials_from_settings`. `FishAudioProvider` never reads `os.environ`.
- A trusted `TTSCredentialReference` (not the secret) is what the provider
  holds; it resolves the key **inside `synthesize`**, after every gate passed.
- `TTSCredential` is opaque: no `__dict__`, redacted `repr`/`str`/`format`, not
  picklable/copyable, immutable, identity equality; only `reveal()` yields it.
- The key appears only in the `Authorization` header. It is absent from every
  request model (there is no such field), the gateway, AgentCore, audit, Memory,
  Knowledge, reprs, serializations, and error messages (all tested with a
  synthetic key).
- No real credential is committed; tests use an obviously fake key.

## Endpoint pinning and network hardening

The endpoint is a **module constant** (`FISH_TTS_URL`). `FishAudioProvider` has
no `base_url`/`endpoint` parameter, `Settings` has **no Fish base-URL setting**,
and nothing in a request, an LLM output, a provider response, a document, or an
MCP result can influence it. This closes the class of issue where environment
configuration silently redirected an API key to another host. Additionally:

- **HTTPS, exact host.** `_assert_pinned` requires scheme `https`, hostname
  exactly `api.fish.audio`, port 443/default, and no userinfo.
- **No redirects.** `follow_redirects=False`; any 3xx is refused and never
  followed, so the key can never reach a redirect target.
- **No ambient configuration.** `trust_env=False`: `HTTP(S)_PROXY`,
  `ALL_PROXY`, `SSL_CERT_*`, `REQUESTS_CA_BUNDLE`, `.netrc` etc. are ignored,
  and no proxy mounts exist.
- **Bounded body.** The response is streamed and aborted the moment it exceeds
  `MAX_TTS_AUDIO_BYTES`; a declared `Content-Length` over the cap is refused
  without reading. `Accept-Encoding: identity` is sent and any
  `Content-Encoding` is refused (no decompression amplification).
- **No error-body leakage.** A non-200 body is never read into Sam.
- **No retries**, no streaming, no other endpoint.

## Voice and profile selection

The voice comes from a `TrustedVoiceProfile` (`profile_id`, `provider_id`,
`provider_voice_reference`, `provider_model`, `enabled`), held in a
`TrustedVoiceProfiles` catalog built once from Sam's configuration. After
construction the catalog exposes only `get`/`list_profiles`, holds its data
behind a read-only `MappingProxyType`, and blocks attribute writes. A request
names only a **profile id**; unknown and disabled profiles are rejected before
any permission check or network call. The LLM cannot supply a `reference_id`, a
model, an endpoint, a key, a timeout, or any sampling parameter: the
LLM-facing models (`SpeechProposal`, `SynthesisRequest`) have **no such fields
and forbid extras**. A profile bound to a different provider can never reach
this one.

## No voice cloning

Phase 10 **does not implement voice cloning.** There is no code path to upload
reference audio, create or enroll a voice, call a cloning endpoint, browse a
voice library, or replicate a person's voice. The provider's request body has
exactly `text`, `reference_id`, `format` (never `references`), is always JSON
(never multipart), targets exactly one URL, and the profile model has no field
that could carry audio. Only an already-approved, preconfigured voice id may be
used. A future cloning feature would need its own consent, provenance, and
permission design.

## Text validation and secret withholding

Before any provider call, `validate_text` checks: a `str`; at most
`MAX_TTS_TEXT_CHARS` (5,000) characters and `MAX_TTS_TEXT_BYTES` (20,000) UTF-8
bytes (size is checked first so huge input is rejected quickly); valid Unicode
(no lone surrogates); no NUL or other control characters (tab/newline/CR
allowed); not blank. The text is never altered or truncated.

Secret-looking text (the existing `looks_like_secret` detector, unchanged) is
**withheld**: `REJECTED / secret_detected`, with **zero** provider calls, zero
network, **no credential lookup**, and not even a permission record. It is
never redacted-and-sent, and the value appears in no result, audit event,
error, or repr.

**Provider-specific inline tags.** Fish supports expressive/direction syntax in
text. Phase 10 exposes no structured direction controls and treats the text
purely as data: it is never parsed for `[tags]`, `reference_id=`, `model=`, or
any control syntax, and is never stripped. If ordinary tags are present they can
only affect how Fish *renders* the words; they cannot select a voice, model,
endpoint, credential, or permission, because those are set by Sam outside the
text (the voice/model are header/body fields Sam writes from the trusted profile).

## HTTP timeout behavior

Sam owns the timeout (`0 < t ≤ 60`, default 30). It is passed to the provider
as `timeout_seconds`, is not a field of any request, and cannot be changed by
the LLM, the request, or Fish. There is no abandoned daemon thread. The Fish
adapter enforces it **at the network boundary**: `httpx.Timeout(t)` sets the
connect, read, write, and pool timeouts, **plus** an overall deadline checked as
bytes stream in (a read timeout is per chunk, so a slow drip could otherwise
stretch a request indefinitely). A timeout becomes a generic `timeout` result
with no audio. As a backstop, the gateway also discards any provider result
that arrives after its deadline. Sam still cannot interrupt an arbitrary
synchronous provider that ignores `timeout_seconds` (a limitation for fakes and
future providers), which is why a real adapter must enforce it at its own
transport boundary — as Fish's does.

## No retry policy

One `synthesize` → at most one Fish HTTP request. There is no retry loop for any
failure (timeout, 429, 5xx, connection, invalid audio). Retries could
double-bill. A failed attempt keeps its request id, so re-sending the same id is
rejected (`duplicate_request`); a caller must issue a new request explicitly.
There is no global exactly-once guarantee across restarts: the request-id table
is in memory and bounded (`MAX_TRACKED_TTS_REQUEST_IDS`).

## Provider error handling

| Fish outcome | Sam result (no provider text, ever) |
|---|---|
| 401 / 403 | `authentication_error` |
| 429 | `rate_limited` |
| 402, 5xx, other non-200, 3xx | `provider_error` |
| timeout (any phase, or overall deadline) | `timeout` |
| connection / protocol / unexpected error | `provider_error` |
| 200 with JSON/text content type, HTML/JSON body, non-MP3 bytes | `invalid_audio` |
| empty body | `invalid_audio` |
| body over the cap (declared or streamed) | `output_too_large` |
| compressed body | `invalid_audio` |

## Audio output validation

Fish output is untrusted. `validate_audio_bytes` checks: bytes; non-empty;
within `MAX_TTS_AUDIO_BYTES` (10 MiB); not an HTML/JSON/text error body
(leading `<`, `{`, `[`, or `Error`); and a structural MP3 signature — a
well-formed ID3v2 header (valid version, syncsafe size, payload present) or an
MPEG **Layer III** frame-sync header (reserved version/layer rejected).
The SHA-256 digest is computed by Sam over the exact returned bytes, never taken
from the provider, and `provider_id`/`trusted_profile_id` on the result are
Sam's own values, never provider-supplied. Nothing is decoded, converted, or
played: no ffmpeg, subprocess, shell, or system player.

## Audit and privacy

`TTSAuditEvent` is content-free by construction (no field for text, audio, key,
header, or provider body). It records request/synthesis ids, principal,
provider/profile ids, permission action/resource/risk/scope/outcome, status,
error category, **text length** (not text), **output size**, whether a provider
call was attempted, and duration. One event per call on every path; a failing
sink never changes a result. `SynthesizedAudio.audio_bytes` and the request
`text` are excluded from `repr` **and from every serialization**
(`model_dump`/`model_dump_json`), so neither can reach a log or JSON body by
accident. Generated audio lives in memory only, for the immediate caller: no
file is written, and no voice-layer object retains a copy.

## Memory and Knowledge isolation

Synthesis input and generated audio are never written to Personal Memory and
never ingested into Knowledge. `sam.tts` imports neither `sam.knowledge` nor any
Memory module other than the pure `sam.memory.sanitization.looks_like_secret`,
and does not import `sam.agent`, `sam.mcp`, or `sam.voice`; a subprocess test
confirms importing the gateway and Fish adapter loads none of them.

## AgentCore boundary

AgentCore is **not modified** and is **not** wired to synthesize automatically —
that would create hidden network calls and hidden cost. `TTSAgentBoundary.speak`
is an explicit call from trusted code (a Phase 11 interface will decide when to
request voice). The only thing an agent/LLM may propose is a `SpeechProposal`
(`text` + a profile id); the principal and any confirmation id come from trusted
code. A synthesis begins only from one explicit request and passes the whole
boundary; an MCP result, a document, a transcript, or a Fish response cannot
start one. AgentCore itself holds no TTS object.

## Real provider vs fake provider

`FishAudioProvider` is the real adapter, but the automated suite never contacts
Fish: it is exercised only through `httpx.MockTransport`, with a synthetic key.
`FakeSpeechSynthesisProvider` and `FakeTTSCredentialProvider` are deterministic
in-process fakes (the fake audio is synthetic MP3-shaped bytes derived from a
digest, never the text). `pytest` and `make check` need no key and make no paid
call. There is no live smoke test in the repository.

## Cost considerations

Fish calls may be billable (pricing is not verified here). Controls: explicit
synthesis only; no automatic synthesis of agent output; bounded text
(5,000 chars); no retries; at most one request per synthesis; a bounded
per-instance duplicate-request guard; a `SPEECH_SYNTHESIS` grant (optionally
confirmation-gated) per profile. The documented free-tier model
(`s2.1-pro-free`) exists; which model a profile uses is Sam's configuration.

## Phase 11 playback boundary

Phase 10 produces validated audio bytes and stops. Playback, buffering, audio
devices, interruption, and deciding *when* to synthesize belong to the Phase 11
interface. Playing untrusted MP3 in a UI must use a vetted player and must not
trust the audio beyond what this layer validated.

## Known limitations

- **One format.** MP3 only; the validator checks structure, not decodability. A
  malformed-but-well-headed stream would pass this layer and be a playback-layer
  concern.
- **Undocumented behaviors are not assumed:** the response content type, 429
  semantics, and maximum text length are not documented by Fish; Sam validates
  the bytes and enforces its own limits, so a future API change could produce a
  new generic failure but not a bypass.
- **Model fallback.** Sam pins models to an allowlist that reflects the docs on
  2026-09-20; new Fish models require a reviewed allowlist update.
- **Timeouts.** Enforced at Fish's network boundary and by an overall deadline;
  Sam cannot interrupt an arbitrary synchronous provider that ignores its
  timeout.
- **Duplicate-call protection is per gateway instance,** in memory, and bounded;
  it does not survive a restart.
- **No content moderation or rights check:** Sam does not judge what text is
  spoken beyond secret withholding and validation.
- **Secret detection is pattern-based** and can miss novel formats or withhold
  ordinary text.
- **A single profile per call;** no per-request voice adjustment, prosody, or
  emotion controls.
- **In-process capability boundary:** the read-only profile catalog and opaque
  credential stop ordinary access, not arbitrary hostile code in the same process.

## Explicit non-scope

Not implemented: voice cloning, custom voice creation, voice enrollment,
reference-audio upload, impersonation or public-figure replication, arbitrary
voice-library browsing, dynamic model or `reference_id` selection by the LLM,
arbitrary Fish API operations, Fish ASR, WebSocket/streaming synthesis, real-time
interruption, audio playback, desktop/mobile UI, automatic synthesis, MCP or
other tool execution from speech, automatic Memory or Knowledge writes, and any
live/paid test. **No dependency was added** (`httpx`, already a direct
dependency, is used; the Fish SDK is not).
