# Owner Voice Identity, Guest Mode and local voice (Phase 12)

> **Voice identity is an authentication signal, not authorization.**
> **Speaker verification is probabilistic and cannot prove identity.**
> **Guest Mode is temporary, explicit, restricted and owner-controlled.**
> **Paid fallback is disabled.**

`PermissionEngine` remains the sole authorization authority. A speaker match
never creates a grant, answers a confirmation, satisfies the Desktop step-up,
or counts as CRITICAL authentication (`tests/test_voice_identity_flows.py`,
`tests/test_desktop_identity.py` assert each of these).

## Flow

```
visible microphone capture (push-to-talk or opted-in local wake detection)
   -> audio validation (Phase 9)
   -> speaker verification            (local selected speaker model, no network)
   -> owner / guest / blocked         (fail closed)
   -> speech-to-text                  (local Whisper, no network)
   -> language policy -> AgentCore -> reply in the detected language (Automatic)
   -> automatic read-aloud            (local native voice or approved provider speech)
```

The speaker is classified **before** any transcription, so a non-owner's speech
is never turned into text for the agent while Sam is owner-only.
This check runs on every conversational turn. Temporary text context never
reuses a previous speaker decision. Short speech is compared when it contains
at least 250 ms of detected speech; quality failures and non-matches remain
blocked. The configured threshold is applied consistently, without a retry
that silently accepts a lower score.
See [voice activation](voice-activation.md) for the separate local wake-name
comparison, capture lifecycle and current validation limits.

## Architecture

| Package | Role |
|---|---|
| `sam.voice_identity` | Provider-independent models, policy, enrollment, verification, challenge, guest, coordinator, secure store, audit. No ML import. |
| `sam.voice_local` | Trusted model registry, explicit setup, ECAPA or WeSpeaker speaker provider, Whisper STT provider. Heavy libraries imported lazily. |
| `sam.language` | `LanguagePolicy` and the request-scoped STT language hint. |
| `sam.desktop` | `/desktop/v1/voice/identity*`, `/voice/guest/*`; composes the above; classification precedes STT. |

Abstractions: `SpeakerEmbeddingProvider`, `SpeakerVerificationProvider`,
`VoiceProfileStore` (Protocols). The security model does not depend on
SpeechBrain; tests use deterministic fake embeddings only.

Effective identity comes only from the trusted stored profile + a backend
verification + backend guest state. The frontend has no way to send a
principal, an owner flag or a capability (`extra="forbid"` requests, and
static tests scan the UI/Rust for such fields).

## Enrollment

Owner starts it explicitly, after **step-up** (the backend-verified Phase 11
secret `DESKTOP_STEP_UP_SECRET`, ≥ 16 chars, constant-time compare, 3 tries then
a 5-minute cooldown). Then 3-5 varied samples. Enrollment is language-agnostic:
English example sentences are suggested, but the owner may speak any language.
The system verifies the speaker, not a spoken phrase or language. Per sample:
≥ 2 s of detected speech, not near-silent, not clipped, not a
duplicate. Samples are embedded in memory and the audio is dropped.

`complete` requires ≥ 3 samples that are mutually consistent
(pairwise cosine ≥ 0.30) and that each **verify against the mean of the others
at the greater of 0.50 or the configured verification threshold** (held-out
check). This retains stronger checks for the longer enrollment samples. The
aggregate is the L2-normalized mean.

Re-enrollment and deletion also need step-up. Re-enrollment replaces the
template atomically (the old one stops verifying). Deleting removes the
Keychain item and ends any Guest Mode. Nothing auto-enrolls from conversation.

## Biometric privacy

* **Never persisted:** raw enrollment/verification audio, per-sample
  embeddings (dropped when the session ends), transcripts, provider debug output.
* **Never in:** Memory, Knowledge, audit, logs, LLM prompts, MCP, localStorage,
  IndexedDB, Git, exceptions, `repr()` (redacted) or API responses.
* **Template format** (`OwnerTemplate.to_json`, the store is its only sink):
  `{"v":1,"model_id","model_revision","sample_count","created_at","vector":[...]}`
  (192 floats for ECAPA, 256 for WeSpeaker).
  Model id and revision are recorded so a template is rejected if the model
  changes (`model_mismatch`, fail closed).

### Persistence: macOS Keychain only

`MacOSKeychainVoiceProfileStore` uses the `keyring` package's native macOS
backend (generic password `app.sam.voice-identity` / `owner-template`,
encrypted at rest by the OS). It refuses to run on another platform or with
any other keyring backend. There is **no** JSON/SQLite/`.env`/file/browser
fallback; a store error makes verification fail closed and is never reported
as "not enrolled". Validated against the real login Keychain with a synthetic
test item (save, load, delete, idempotent delete; removed afterwards).

## Verification and threshold

Results: `OWNER_VERIFIED`, `OWNER_NOT_VERIFIED`, `UNKNOWN`, `NOT_ENROLLED`,
`INSUFFICIENT_AUDIO`, `VERIFICATION_ERROR`. The coordinator classifies:

| Result | Guest Mode off | Guest Mode on |
|---|---|---|
| owner verified | owner | owner |
| owner not verified | **blocked** ("Owner verification required.") | guest |
| unknown / insufficient audio / error | blocked | blocked |
| not enrolled | **blocked** (no profile never implies owner; enrollment is gated by the Desktop step-up secret) | blocked |

Sam owns the cosine threshold: `SPEAKER_VERIFICATION_THRESHOLD` (bounded
0.30–0.90, default **0.50**). It is trusted configuration only. A threshold
change needs representative owner and other-speaker evaluation: lowering it
can reduce false rejections while increasing false acceptance. There is no
automatic lower-threshold retry or verification cache.

The minimum of 250 ms of detected speech is an input gate, not a claim that
every such clip contains enough information for reliable speaker matching.
Silence padding adds recording duration without adding speaker evidence;
surrounding silence is trimmed before inference. Audio repetition and automatic
profile adaptation are not used.

**Short-command matching remains unresolved.** In the final owner trial on
11 October 2026, requests with approximately 1.57 and 1.28 seconds of detected
speech failed the current WeSpeaker match, while a 3.20-second control verified
and answered. All three used a compatible profile and matched the independent
reference processing through embedding inference. Levels were usable, speech
edges were buffered and no clipping was detected. No owner audio, template or
similarity score was retained. This is evidence of an unresolved matching
limitation, not a calibrated false-reject or false-accept rate.

`LOCAL_SPEAKER_MODEL=wespeaker` selects the optional **WeSpeaker ResNet34-LM**
provider; the project default remains `ecapa`. It uses the author's 80-bin
Hamming-window filterbanks and temporal mean subtraction with a pinned,
integrity-checked ONNX model. The author's documentation says LM fine-tuning
can improve longer audio, such as clips over three seconds; that observation
does not prove why a particular short request fails.
See [the upstream model documentation](https://github.com/wenet-e2e/wespeaker/blob/master/docs/pretrained.md).

Changing the speaker model requires **explicit re-enrollment**: the old
Keychain profile remains intact but cannot verify against another model's
embedding space. The UI blocks recording and directs the owner to re-enroll.
Canceling enrollment preserves the old profile; completing enrollment replaces
it atomically. No profile migration or fallback match is performed. A compatible
profile should not be repeatedly replaced to troubleshoot short requests.

Enrollment consistency checks cover the enrollment samples; they do not certify
short-command recognition. Fresh owner and other-speaker recordings on the
intended microphone, languages and acoustic conditions remain necessary before
deployment. A successful voice match never replaces sensitive-action
confirmation or step-up.

## Anti-replay: challenge-response

Sensitive identity transitions (starting Guest Mode) need a fresh, random,
one-time, session-bound, 60-second challenge - four random digits plus a colour
word, spoken in English or Persian ("Please say: 7 4 9 2 blue" / "بگویید: 7 4 9 2 آبی").
Verification needs **both** a speaker match **and** the exact digits+word in the
transcript, in the **same utterance**. The challenge is consumed on its first
evaluation (pass or fail). On failure the caller learns only "verification
failed" (no oracle for which half failed). The resulting `OwnerProof` is
HMAC-signed, session-bound, single-use and expires in 60 s.

**Limits.** This defeats replay of a *previous* recording. It does **not** stop
a real-time voice clone, high-quality synthetic speech, or hostile code in the
same process. For HIGH/CRITICAL, financial, security, credential and
permission-management actions voice verification never replaces stronger
authentication: the Phase 11 step-up and confirmation flow stay authoritative.

## Guest Mode

Off by default. Start requires **all of**: owner speaker match + fresh
challenge (one utterance) + the step-up secret. A spoken command such as "allow
guest conversation" is worthless on its own; a recorded copy cannot start it.

* Time-bounded: default 15 min, **hard max 30**, revocable at any moment
  (ending needs no proof: it only removes access). Expires on its own.
* Conversation only. The guest has its **own principal** (`guest-<id>`) and its
  own permission store holding only the voice-session grants; the owner's
  store, Memory, Knowledge, tools and permissions are never touched. Every
  engine call the guest could make is default-deny (tested).
* A guest cannot: read personal Memory or private Knowledge, use Gmail/Calendar/
  Drive/MCP/coding/computer control, manage permissions/settings/credentials,
  SEND/PUBLISH/DELETE/EXECUTE/APPROVE, extend itself, start another guest,
  become the owner, change providers, or re-enroll/delete the owner profile.
  A guest utterance cannot satisfy a confirmation (never forwarded).
* Guest transcripts are session-local, bounded, never written to Memory or
  Knowledge, and erased on expiry/revocation.
* The Desktop shows a persistent banner with a countdown and an **End Guest
  Mode** button on every screen.

Limitation: the Desktop has no owner login, so anyone at the keyboard already
has owner-level UI access. Guest Mode restricts *voice* conversation; it is not
a lock-screen.

## Local models: provenance and setup

Model acquisition is an explicit, one-time step, never part of a request.
In the app the owner starts it from Settings (see `docs/voice-activation.md`,
*Voice components*); the admin CLI does the same:

```bash
uv sync --group voice-local                       # macOS / Apple silicon only
uv run python -m sam.voice_local.setup --speaker --stt small
# Optional selected model; also set LOCAL_SPEAKER_MODEL=wespeaker for runtime:
uv run python -m sam.voice_local.setup --speaker --speaker-model wespeaker
```

Cache: `~/Library/Application Support/Sam/models` (`speaker-ecapa` ≈ 83 MB,
`speaker-wespeaker-resnet34-lm` ≈ 27 MB,
`stt-small` ≈ 486 MB, `stt-medium` ≈ 1.5 GB, `stt-large-v3` ≈ 3 GB).

| Model | Source @ pinned commit | License |
|---|---|---|
| Speaker (ECAPA, default) | `speechbrain/spkrec-ecapa-voxceleb` @ `0f99f2d0ebe8...` | Apache-2.0 |
| Speaker (WeSpeaker, opt-in) | `Wespeaker/wespeaker-voxceleb-resnet34-LM` @ `f0c48c298fd8...` | CC-BY-4.0 |
| STT small | `Systran/faster-whisper-small` @ `536b0662742c...` | MIT |
| STT medium | `Systran/faster-whisper-medium` @ `08e178d48790...` | MIT |
| STT large-v3 | `Systran/faster-whisper-large-v3` @ `edaa852ec7e1...` | MIT |

`sam.voice_local.registry` pins every file to a commit **and a SHA-256**; the
setup step fails and deletes a file whose hash differs, the Hub endpoint is a
constant, and each load re-verifies. Only multilingual Whisper sizes on the
allowlist are usable (no `.en` models, no arbitrary ids).

Supply-chain choices: `trust_remote_code`, `from_pretrained`, `from_hparams`,
`snapshot_download`, pickle loading and `eval` do not appear in the runtime
code (enforced by tests). The ECAPA network is **rebuilt in Sam code** with the
published hyperparameters and only `embedding_model.ckpt` is loaded with
`torch.load(weights_only=True)`; the repository's `hyperparams.yaml`
(HyperPyYAML `!new:` = object construction) and any `custom.py` are never
evaluated. WeSpeaker loads only its verified ONNX graph through the CPU
execution provider, with two intra-op threads, one inter-op thread, and idle
thread spinning disabled. Its preprocessing is implemented locally; downloaded
Python and YAML are never executed. Runtime processing makes no network calls.

## Dependencies (optional `voice-local` group, macOS arm64)

Not installed by default; the core suite runs on fakes. Direct: `torch==2.11.0`
(BSD-3-Clause), `torchaudio==2.11.0` (BSD), `speechbrain==1.1.1` (Apache-2.0),
`faster-whisper==1.2.1` (MIT), `onnxruntime==1.31.0` (MIT),
`keyring>=25.7,<26` (MIT). Notable transitive:
`ctranslate2` 4.8.2 (MIT), `av` 18.1.0
(BSD-3-Clause), `tokenizers` 0.23.2 and `huggingface-hub` 1.32.0 (Apache-2.0),
`numpy` 2.5.3, `scipy` 1.18.1, `sentencepiece` 0.2.2, `hyperpyyaml` 1.2.3.
+41 locked packages, purely additive; no CUDA/nvidia/triton packages (all
requirements are marked `sys_platform == 'darwin' and platform_machine == 'arm64'`).
`torch` and `torchaudio` are pinned as a matching pair.

## Measured performance (Apple silicon, 8 GB RAM, CPU-only int8)

| Item | Measurement |
|---|---|
| ECAPA model load | ≈ 1.3 s; first embed ≈ 2.5 s incl. lazy import; then ≈ 0.04 s |
| WeSpeaker warm inference | ≈ 19 / 28 / 36 / 68 ms median for 0.5 / 0.75 / 1 / 2-second public query crops; CPU-only, two threads |
| STT `small` load | 1.1-3.6 s; 3 s English clip 2.7-3.4 s warm (first 6.4 s) |
| STT `medium` load | 3.8-5.8 s; 3 s English clip 11-14 s warm |
| Peak RSS | ≈ 760 MB (small), ≈ 1.1 GB (medium), ≈ 1.0 GB small + speaker |

Models load lazily, once, per provider instance (no global singleton). Default
STT is `small` for latency on this machine; `medium` is a documented opt-in.

## Audit

Content-free events only: `owner_enrollment_started|completed|failed`,
`owner_profile_deleted`, `owner_voice_verified|not_verified`,
`challenge_issued|passed|failed`, `guest_mode_started|expired|revoked`,
`speaker_blocked`, `language_detected` (with a code from the closed, validated
Whisper language catalog). They
appear in Activity. No audio, embedding, template, challenge audio, transcript
or Keychain value can be recorded (the vocabulary and fields are closed).

## Known limitations

See `docs/persian.md` for STT accuracy. Speaker verification depends on the
microphone/room; there are no measured real-world error rates. No liveness
detection beyond the spoken challenge. Enrollment/verification on non-macOS
platforms is unsupported (Keychain is mandatory). Hostile code in the Sam
process could read an in-memory template while it is loaded.
