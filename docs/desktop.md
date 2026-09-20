# Desktop UI & Local Interaction Layer (Phase 11)

Sam's desktop app is a Tauri 2 shell (Rust) hosting a React + TypeScript + Vite
interface, talking to the existing Python engines through a **narrow, typed,
local bridge**.

> **The Desktop UI is not an authorization boundary. `PermissionEngine` remains
> the sole authorization authority.** A button, UI state, a frontend "role", a
> hidden field or a confirmation click is never permission.

## UI direction

- **Structure / UX** follows [OpenJarvis](https://github.com/open-jarvis/OpenJarvis)
  (Apache-2.0): full-bleed shell, a 268 px glass sidebar (new-chat and collapse
  controls, status badge, chat search, session conversation list, bottom page
  navigation with a glowing active bar), a thin top pulse bar and backend-health
  banner, a chat toolbar with a right-hand *System* panel, greeting hero with
  quick-action chips, and a bottom composer. Only layout and interaction
  patterns were used; no OpenJarvis code, branding, assets or dependencies
  (Tailwind, base-ui, telemetry) are included.
- **Visual language** follows the project's reference images: near-black navy
  ground, electric-blue luminous rims and bloom, translucent indigo glass,
  squircle/pill geometry, dark glossy control buttons.
- Sam-specific pages (Knowledge, Memory, Tools, Permissions, Activity, Settings)
  extend the same shell and tokens.

## Architecture

```
React UI ──(named Tauri commands, bounded args)──▶ Rust shell ──(fixed 127.0.0.1, token)──▶ FastAPI /desktop/v1 ──▶ engines
```

| Layer | Location | Responsibility |
|---|---|---|
| UI | `desktop/src` | Presentation only. Untrusted content rendered as text. |
| Bridge interface | `desktop/src/bridge` | 14 fixed methods (`SamBridge`). No generic request/invoke. |
| Tauri shell | `desktop/src-tauri` | 14 named commands, validation, fixed loopback client. |
| Backend bridge | `src/sam/desktop` | `/desktop/v1` router, token/loopback checks, server-bound principal. |
| Engines | existing | AgentCore, Knowledge, Memory, MCP registry (read), Voice, TTS, PermissionEngine. |

### Backend bridge (`/desktop/v1`)

Enabled only when `DESKTOP_BRIDGE_TOKEN` (≥ 32 chars) is set; otherwise every
request fails closed with 503. Each request must satisfy: loopback peer,
loopback `Host` (defeats DNS rebinding), **no `Origin` header** (browsers
always add one to cross-site requests), constant-time token match, and a body
size bound. Request models use `extra="forbid"`: sending `principal`, `scope`,
`risk`, `allow`, `endpoint`, `model`… yields HTTP 422. The principal
(`local-user`) is bound by the runtime, never supplied by the client.

Routes (complete list): `GET status, knowledge/resources, tools, permissions,
activity`; `POST chat, knowledge/{query,ingest,remove}, memory/search,
permissions/revoke, confirmations/decide, voice/utterance, tts/speak`.

### Bootstrap grants (complete list)

| Resource | Action | Scope | Condition |
|---|---|---|---|
| knowledge | write | `default:ingest` | always |
| knowledge | read | `default:list` | always |
| knowledge | read | `default:retrieve` | always |
| knowledge | delete | `default` | always (still confirmation-gated by policy) |
| voice | create/read/update | `session` | only if a transcription provider is configured |
| speech_synthesis | send | `<provider>/<profile>` | only for enabled trusted profiles; **every use asks for confirmation** (grant-level `always_require_confirmation`, because it sends text to an external provider) |

Every grant is tagged `origin=desktop_bootstrap`, has no expiry/wildcard, and
is recorded as a content-free `Bootstrap grant: …` event in Activity. A test
asserts this exact set, so any addition fails review.

Trusted grants are created in exactly one place (`bootstrap_grants`): the
single `default` Knowledge collection, voice session operations (only if voice
input is configured) and speech synthesis for the configured profile. Nothing
is granted for MCP, computer control, coding or Memory writes, and no
wildcard/root grant exists. `knowledge delete` still requires confirmation by
policy.

### Confirmation flow

1. Operation → engine returns `CONFIRM_REQUIRED` → the bridge returns a
   display-only `Challenge` built from the backend's own confirmation record.
2. The dialog shows action/resource/scope/target/risk (LOW → CRITICAL).
   **CRITICAL** additionally needs *step-up authentication*: the user types a
   secret that only the backend can verify (`DESKTOP_STEP_UP_SECRET`, ≥ 16
   chars, constant-time compare). If none is configured the backend refuses to
   approve CRITICAL actions from the desktop at all; three wrong attempts deny
   the confirmation permanently. A checkbox or click is never enough. Denying
   never needs step-up. Deny is initially focused; Escape denies.
3. Approve/Deny calls `confirmations/decide` (only PENDING, local principal).
4. On approve the UI **retries the same operation with the confirmation id**;
   the PermissionEngine re-checks the grant and the exact
   principal/action/resource/scope/target binding, expiry and one-time use. A
   denial on retry is shown as a denial.

### Surfaces

- **Sam**: chat via AgentCore; history is session-local React state.
- **Knowledge**: upload (PDF/TXT/MD/JSON/CSV, 20 MB), search with provenance
  (only fields the backend supplied), remove (confirmation). Stored in memory
  for the session.
- **Memory**: read-only; distinct from Knowledge; nothing from chat is saved.
- **Tools**: read-only view of the trusted MCP registry; no admin handle exists
  in the runtime (`MCPRegistryAdmin` is discarded after composition).
- **Permissions**: list + revoke only. No create/widen/"allow all".
- **Activity**: current-session, content-free events.
- **Settings**: honest capability states (Configured / Not configured /
  Foundation ready), UI preferences, trusted read-aloud profile selector. No
  URL/token/key/endpoint fields.

### Voice

- **Input**: explicit push-to-record (Web Audio) producing mono PCM16 WAV at
  16 kHz; ≤ 30 s; in memory only; tracks stopped and buffers cleared on
  stop/cancel/unmount; no background listening or wake word. Enabled only when a
  transcription provider is configured (none ships yet, so the control is
  disabled and says so). A transcript the Phase 9 gateway withholds as
  secret-like is never displayed or forwarded.
- **Output**: explicit "Read aloud" per Sam reply. The UI sends text and a
  *trusted profile id* the backend advertised; the profile's voice reference,
  model, endpoint and key never leave the backend. Audio plays from an
  in-memory blob whose object URL is revoked when playback ends.

## Security properties

- Fixed destination `127.0.0.1` (not configurable; only the port may be set via
  `SAM_BACKEND_PORT`, ≥ 1024), bounded request/response sizes, finite timeouts,
  no redirects, proxies ignored, no TLS stack.
- The bridge token is read from `DESKTOP_BRIDGE_TOKEN` **in Rust only**; the
  webview never sees it or the backend URL.
- Tauri capabilities grant only the 14 `allow-sam-*` command permissions
  (declared via an `AppManifest` in `build.rs`). No shell, fs, process,
  clipboard, global-shortcut, http, window creation or remote webview.
- CSP: `default-src 'none'; script-src 'self'; connect-src ipc:` — no remote
  scripts/fonts/styles, no `unsafe-eval`, no direct network from the webview.
- Source rules (lint + tests): no `dangerouslySetInnerHTML`, `innerHTML =`,
  `eval`, `new Function`, `fetch`, XHR, WebSocket, remote URLs.
- `localStorage` is used by one module and only for three allowlisted UI
  preferences (`sidebarCollapsed`, `reducedMotion`, `readAloudVoice`).
- Errors shown to the user are short fixed messages with reference ids; no
  tokens, paths, provider bodies or tracebacks.

## Running

```bash
# Backend
export DESKTOP_BRIDGE_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
make run                                   # backend on 127.0.0.1:8000
# Desktop (same token in this shell's environment)
make desktop-install
cd desktop && npm run tauri dev
```

Optional voice output: set `FISH_AUDIO_API_KEY`, `FISH_AUDIO_VOICE_REFERENCE`
(and `FISH_AUDIO_MODEL`) for the backend.

## Verification

```bash
make check                # Python (unchanged semantics)
make desktop-check        # eslint, tsc, vitest, production build
make desktop-rust-check   # cargo fmt --check, check, test, clippy -D warnings
```

## Not in this phase

Computer-control and coding-agent UIs, MCP administration, real STT, wake word,
persistent conversation history, proactive behavior (Phase 12), auto-update,
code signing/notarization.
