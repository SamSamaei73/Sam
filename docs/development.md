# Development and verification

Use an isolated checkout or worktree if the current checkout contains recovered
or unfinished work. Leave that work intact when preparing an unrelated PR.

## Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- Node.js 22.22.2+ within the 22.x line for the locked desktop dependencies.
  Node 16 is incompatible; check `node --version` and `npm --version` first.
- Rust and the platform prerequisites for native Tauri development.
- macOS on Apple silicon for optional local owner-voice processing and Keychain
  profiles. Core tests use fakes and do not need those models.

The lockfiles are reproducible dependency inputs. Do not regenerate them to
work around an old local Node or Python installation.

## Python backend

From the repository root:

```bash
uv sync --frozen
cp -n .env.example .env
make run
```

Check `http://127.0.0.1:8000/health` in another terminal. Configure providers
using [.env.example](../.env.example) and [model routing](model-routing.md).
An explicitly configured OpenRouter account can incur usage charges; ordinary
fake-based tests do not need a provider key.

## Browser preview

```bash
cd desktop
npm ci
npm run dev -- --host 127.0.0.1
```

Open Vite's printed URL. A plain browser in development uses
`desktop/src/bridge/demo.ts`, gated by `import.meta.env.DEV`. Its data and
capabilities are synthetic; production bundles exclude it.
The `?fixture=setup`, `?fixture=degraded` and `?fixture=attention` URLs support
visual checks. Demo behavior is not evidence of live speaker verification,
backend authorization, integrations or provider calls.

Use synthetic fixtures for screenshots and label them as development previews.
Do not capture the owner's documents, keys, enrollment audio or biometric
results. Avoid microphone-enabled fixtures unless microphone testing is intended.

## Native desktop

The real app needs Rust/Tauri prerequisites and an authenticated local bridge.
Follow the [desktop running instructions](desktop.md#running); the backend and
desktop must receive the same local bridge token. Keep tokens out of source,
screenshots and logs. See [owner identity](voice_identity.md) and
[voice activation](voice-activation.md) for optional local voice setup.

The packaging scripts in `desktop/scripts/` belong to Phase 17. Read
[production hardening](production-hardening.md) before packaging or operating
durable state. A successful build alone is not production approval.

## Checks

Run from the repository root:

```bash
make check                # Ruff, pytest, mypy
make desktop-check        # ESLint, TypeScript, Vitest, Vite build
make desktop-rust-check   # Rust formatting, check, tests, Clippy
git diff --check
```

`make check` deliberately excludes desktop and Rust checks. Run focused tests
during implementation, then applicable full checks before review. Keep live
provider/model tests explicit and separate; never add credentials or weaken a
test to obtain a green result.

For docs-only changes, verify linked paths, commands against Makefile/package
scripts, rendered Markdown and claims against committed source. Report which
checks actually ran. Documentation validation is not a full application or
security review.

## Known verification limits

[PR #1](https://github.com/SamSamaei73/Sam/pull/1) records checks for the
OpenRouter/voice work merged on 11 October 2026. It reported five Python
failures reproduced on its base: four involving the installed Git executable's
CPU architecture and one date-dependent professional-profile expectation.
That is historical evidence, not a current suite result.

Short-phrase owner matching remains unresolved. Automated signal/state tests
do not establish biometric accuracy. See [voice validation](voice-activation.md#manual-validation).
Earlier [test-stack notes](phase-history.md#test-stack-compatibility) explain
runtime `httpx` versus test-only `httpx2`.

There is no repository CI workflow at this documentation update, so the README
does not display a CI-passing badge.
