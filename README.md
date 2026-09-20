# Sam

Phase 2 provides Sam's agent core foundation: a typed FastAPI service with environment-based configuration, centralized logging, a provider abstraction, a lifecycle-managed Claude provider, and a bounded `/agent` endpoint.

Phase 3 adds Sam's Permission Engine: a deterministic, provider-neutral authorization boundary in `src/sam/permissions/` that will sit between a future planner/tool router and any real tool execution. It decides ALLOW / DENY / CONFIRM_REQUIRED for a typed `(principal, action, resource, scope)` request — never the LLM — and fails closed on anything unknown, malformed, revoked, expired, or unexpected. See [`docs/permissions.md`](docs/permissions.md) for the full model, precedence rules, and threat model. No tool, MCP server, or integration is implemented yet, and no new API endpoints were added — see that doc's "Future integration boundary" section for why.

Phase 4 adds Sam's Memory Engine: a cloud-ready-but-not-cloud-dependent store for working, episodic, semantic, and project memory in `src/sam/memory/`. A `MemoryCandidate` always passes through a deterministic `MemoryPolicy` before anything is persisted — the LLM can propose a memory, never write one directly — and every read/update/delete is scoped to an explicit principal, enforced by the store itself. See [`docs/memory.md`](docs/memory.md) for the full model, the secret-detection policy, and the intended future PostgreSQL + pgvector migration. Only an in-memory reference store is implemented; no database, embeddings, or new API endpoints were added.

Phase 5 adds Sam's Computer Control foundation: a typed, modular abstraction in `src/sam/computer/` for screenshots, active-window queries, and bounded mouse/keyboard interaction — never an unrestricted autonomous computer agent. Every `ComputerAction` maps to a `PermissionRequest` and is authorized exclusively by the existing Phase 3 Permission Engine (extended with one new `COMPUTER` resource) before `ComputerController` ever calls a backend; keyboard actions require confirmation, never a grant alone. See [`docs/computer-control.md`](docs/computer-control.md) for the full architecture, risk model, and security write-up. No real OS backend ships in Phase 5 — only a deterministic fake for tests and a structurally-complete placeholder that honestly reports itself unavailable — and no shell, terminal, filesystem, browser, or clipboard capability exists anywhere in the package.

Phase 6 adds Sam's Coding Agent foundation: a controlled engineering workflow in `src/sam/coding/` for repository inspection, bounded file writes with optimistic concurrency (a SHA-256 hash check that refuses to overwrite a file changed since it was read), and a closed set of allowlisted verification commands (tests/lint/typecheck/`git diff --check`) — never an unrestricted shell agent. Every operation passes through the existing Phase 3 Permission Engine (extended with one new `CODE` resource, reusing the pre-existing `GIT` resource for git status/diff) before `CodingExecutor` ever calls `RepositoryBackend`; running tests/lint/typecheck always requires confirmation. Command execution never uses a shell, resolves executables only from a small trusted directory list (never the repository or the ambient `PATH`), and passes the spawned process a minimal explicit environment. See [`docs/coding-agent.md`](docs/coding-agent.md) for the full architecture and security write-up. No real Claude Code/Codex provider integration was implemented — only a narrow `CodingProvider` Protocol and a deterministic fake — and `git commit`/`push`/`reset`/`clean`/`restore` do not exist as operations at all.

Phase 7 adds Sam's Knowledge Layer: a provider-independent system in `src/sam/knowledge/` for ingesting user-provided reference material (PDF, TXT, Markdown, JSON, CSV) into deterministically-chunked, source-attributed storage — separate from Personal Memory and never wired into it automatically. Every operation (ingest / get / list / retrieve / remove) passes through the existing Phase 3 Permission Engine (extended with one new `KNOWLEDGE` resource) before `KnowledgeEngine` ever calls its store, index, or parser; deleting a resource always requires confirmation. A best-effort PDF text/page extractor (standard-library-only — no external PDF dependency) preserves page boundaries and never fabricates a citation for content it did not actually find; a scanned/image-only PDF is honestly rejected rather than silently "learned" as empty. See [`docs/knowledge.md`](docs/knowledge.md) for the full architecture and security write-up. Only an in-memory store and a deterministic lexical index are implemented — no vector database, no real embedding provider (`FakeEmbeddingProvider` only), and no wiring into `AgentCore`.

Phase 8 adds Sam's MCP Gateway & Integrations Framework: a secure, provider-independent path (`src/sam/mcp/`) from a proposed tool call to an external side effect, built and tested entirely against deterministic fakes — **no real integration is connected**. MCP is treated as an integration protocol, not a security boundary: a trusted registry (not the server), split into a read-only runtime view and a separate administrative interface that the runtime path never receives, owns each tool's permission binding, scope, credential reference, timeout, size limits and verification requirement; tool descriptions, server metadata and tool results are untrusted data; and every call passes canonical-id resolution, schema validation, deterministic scope derivation and the existing Phase 3 `PermissionEngine` (the sole authorization authority, with confirmations bound to the exact arguments) before a credential is resolved from a trusted reference and at most one bounded, timed transport call is made. Results are validated, checked for credential echo, optionally verified, audited content-free, and never written to Memory or Knowledge. AgentCore is unchanged; `MCPAgentBoundary` is the typed integration surface. See [`docs/mcp.md`](docs/mcp.md) for the trust model, lifecycle, and limitations.

Phase 9 adds Sam's Secure Voice Input & Speech Understanding Foundation: a provider-independent path (`src/sam/voice/`) from one explicitly supplied, bounded audio input to a normalized transcript with provenance, built and tested entirely against deterministic fakes — **no real speech-to-text, microphone, wake word, background listening, or biometric enrollment**. Only 16-bit PCM and PCM-WAV are accepted (structurally validated, no codecs, no ffmpeg, no subprocess); every operation passes the existing Phase 3 `PermissionEngine` (a new `VOICE` resource) before any provider is called; the transcription provider is untrusted (validated results, contained errors, Sam-owned timeouts, at most one call, no retries); and an optional voice-identity result is only a transient, signed, one-time, session/utterance/audio-bound *authentication signal* — **voice identity is not authorization, and voice input never bypasses PermissionEngine, confirmation, or stronger authentication**. Raw audio and transcripts are never persisted, audited, or written to Memory or Knowledge, and a transcript that looks like it contains a secret is withheld entirely and never forwarded to AgentCore or an LLM provider. The voice layer creates no threads: provider timeouts are Sam-owned and passed to the provider, with late results discarded. AgentCore is unchanged; `VoiceAgentBoundary` forwards only the validated transcript text as ordinary user input. See [`docs/voice.md`](docs/voice.md) for the trust model and limitations.

Phase 10 adds Sam's Secure Voice Synthesis layer (`src/sam/tts/`): explicit, non-streaming REST text-to-speech with a provider-independent `TTSGateway` and **Fish Audio as the first real provider** (`FishAudioProvider`, exercised in tests only through `httpx.MockTransport` — no live call, no real key, no billing). **Fish Audio is a synthesis provider, not an authorization or security boundary**, and **Phase 10 does not implement voice cloning**. Synthesis has its own `PermissionResource.SPEECH_SYNTHESIS` (`SEND`, MEDIUM) so a Phase 9 `voice` grant can never authorize sending text to an external service; the voice and model come only from a trusted, read-only voice profile catalog (never from the LLM, the text, or Fish); the endpoint is pinned (`https://api.fish.audio/v1/tts`, no redirects, no ambient proxy/env, no base-URL setting); the API key exists only at the provider execution boundary; **secret-containing text is never sent to Fish Audio**; provider output is validated (size, MP3 signature, digest computed by Sam); there is at most one HTTP call per request and no retries; and text and audio are never persisted, audited, or written to Memory or Knowledge. See [`docs/tts.md`](docs/tts.md) for the verified Fish API assumptions, trust model, and limitations.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
cp .env.example .env
```

## Run

```bash
make run
```

The server reads `API_HOST` and `API_PORT` from the Pydantic settings model, so
values in `.env` control the development binding. The defaults bind only to
`127.0.0.1:8000`.

The service exposes `GET /health`.

The agent exposes `POST /agent` with a JSON body containing a `message`. Claude
configuration is loaded from `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`,
`CLAUDE_BASE_URL`, `CLAUDE_TIMEOUT`, and `CLAUDE_MAX_RETRIES`. The endpoint
must use HTTPS, timeouts are finite and bounded, and provider retries are
explicitly limited. No live provider call is made unless the agent endpoint is
invoked with a configured API key.

## Checks

```bash
make check
```

Phase 2 intentionally contains no tools with side effects, integrations beyond
the Claude provider, memory, UI, voice, automation, or permission system.
Phase 3 intentionally contains no memory, database, Redis, MCP integrations,
real external tools (filesystem, terminal, browser, Gmail, Git, ...), or
autonomous/background execution — see `docs/permissions.md` for the full
scope boundary.
Phase 4 intentionally contains no PostgreSQL, pgvector, Supabase, Redis,
vector database, embeddings, MCP integrations, real authentication, or
autonomous/background memory behavior — see `docs/memory.md` for the full
scope boundary.
Phase 5 intentionally contains no shell/terminal execution, filesystem
manipulation, browser automation, clipboard access, MCP, real
authentication, a real OS backend, or autonomous/background action
loops — see `docs/computer-control.md` for the full scope boundary.
Phase 6 intentionally contains no arbitrary shell/command execution,
`eval`/`exec`, file deletion, git commit/push/reset/clean/restore, a
real Claude Code/Codex provider adapter, MCP, real authentication, or
autonomous/background coding loops — see `docs/coding-agent.md` for the
full scope boundary.
Phase 7 intentionally contains no automatic writes to Memory, PostgreSQL/
pgvector/Supabase/Redis/vector database, real embedding provider, OCR,
MCP, AgentCore wiring, UI, or autonomous/background ingestion — see
`docs/knowledge.md` for the full scope boundary.
Phase 8 intentionally contains no real Gmail/Calendar/GitHub/Meta/Buffer/
Metricool/RevenueCat integration, real OAuth or credential storage, network
or subprocess transport, MCP SDK dependency, automatic Memory/Knowledge
writes, AgentCore rewrite, autonomous tool loop, or background polling — see
`docs/mcp.md` for the full scope boundary.
Phase 9 intentionally contains no real speech-to-text or cloud STT API, text-to-speech
(Fish Audio is Phase 10), microphone or audio-device access, wake word,
always-on/background listening, biometric enrollment or database, challenge-
response, OS-level authentication, MCP/tool execution from voice, automatic
Memory/Knowledge writes, or AgentCore rewrite — see `docs/voice.md` for the
full scope boundary.
Phase 10 intentionally contains no voice cloning, voice creation/enrollment or reference-audio
upload, WebSocket/streaming synthesis, arbitrary Fish API operations, Fish ASR, audio
playback, UI, automatic synthesis of agent output, MCP/tool execution from speech, or
automatic Memory/Knowledge writes — see `docs/tts.md` for the full scope boundary.

## Test-stack compatibility

Starlette 1.6.0 still imports a deprecated AnyIO type alias from its test client.
The test configuration narrowly filters that single upstream warning until a
Starlette release includes the existing upstream fix. All other warnings are
treated as errors, and no warning suppression exists for the `httpx`
deprecation described below.

Two HTTP clients are declared, for two different purposes:

```text
httpx   (runtime dependency)
  -> Sam's production HTTP client: the Fish Audio provider, and the transport
     the Anthropic SDK is driven with. Tests use httpx.MockTransport for both.

httpx2  (development/test dependency only, in the `dev` group)
  -> required by the current Starlette TestClient, which prefers httpx2 and
     deprecates httpx. Nothing under src/ imports it; Sam and Fish traffic
     never use it.
```

`httpx2` is the Pydantic-maintained continuation of `httpx` (PyPI project
`httpx2`, source `github.com/pydantic/httpx2`). It resolves from the standard
PyPI registry with no path, git, or index override, and brings `httpcore2` and
`truststore` transitively. Its `httpx2-jsfetch` dependency is gated to
`sys_platform == 'emscripten'` and is never installed on macOS or Linux.
Because it is declared and locked, `uv sync --frozen` reproduces the suite
exactly; an undeclared copy in a virtual environment is not relied on.

## Desktop UI (Phase 11)

A Tauri + React desktop app lives in `desktop/` and talks to the backend through a narrow local bridge. The UI is not an authorization boundary; `PermissionEngine` stays the sole authority. See [docs/desktop.md](docs/desktop.md). Use `make desktop-check` and `make desktop-rust-check` (kept separate from `make check`).
