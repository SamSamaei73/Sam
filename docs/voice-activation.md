# Hands-free voice activation ("Sam")

Status: implemented, awaiting the owner's review. Not committed.

## What the owner experiences

1. With **Voice activation** on, Home shows Sam waiting quietly: *Say "Sam" to start*.
2. The owner says **"Sam"**. Sam wakes up; this happens locally, see below.
3. The owner speaks. When they stop talking, the turn ends by itself (voice
   activity detection). There is no button.
4. Sam checks the owner's voice **on this Mac**, thinks, answers on screen and
   **speaks the answer with a macOS on-device voice**.
5. Sam then listens for the next turn. There's no need to say "Sam" again.
6. The conversation ends when:
   - the owner says a stop phrase ("Sam, go to sleep", "That's all", "Stop
     listening", "Goodbye", Persian equivalents), and nothing is sent to the
     model;
   - 20 seconds pass without speech;
   - the voice is not the owner's;
   - the owner leaves Home, hides the window or quits.

   Sam then goes back to waiting for its name.

A small **talk now** / **stop listening** control and the keyboard remain as
fallbacks. With voice activation off, the small microphone is push-to-talk.

**Text drafts and voice are independent.**
- An unfinished message in the text box never blocks the wake word, the
  microphone or owner verification.
- The draft is kept in memory only (never persisted) while the overlay is
  closed and during any voice conversation.
- It is never read by the voice path, sent to the model, added to a
  transcript or submitted automatically.
- Only the owner's explicit **Send** clears it.

## Privacy: listening for a name is not recording

While Sam is waiting for its name:

- **Where it runs:** the microphone is open only while Home is on screen and
  the window is visible.
- **What is kept:** every audio frame goes to an in-memory voice-activity
  detector and is then dropped. The only rolling buffer is a **300 ms
  pre-roll**, so the first syllable of a word isn't clipped.
- **What is checked:** only a finished speech segment (0.25 to 4 seconds) is
  a wake candidate. It goes over the authenticated loopback bridge to Sam's
  own backend on this Mac. The backend transcribes it with the **local
  Whisper model** (`faster-whisper`, the same verified local model Phase 12
  uses) and answers **one bit**: was it the name?
- **The recognized text:** never returned, logged, audited, stored in Memory
  or Knowledge, or given to the agent or any provider. Whatever is said
  before the name, including a password, a meeting or TV audio, is dropped
  after that single comparison.
- **No cloud and no disk:** the wake stage makes **no network call** beyond
  loopback, and nothing about idle audio is written to disk, browser storage,
  the activity log or the audit log.
- **Quiet rooms:** silence never reaches the backend at all.
- **Guests:** the switch is owner-only. A guest cannot see or change it, and
  the backend refuses it in Guest Mode. The model, tools and remote content
  cannot reach it.

After the name, each turn's audio follows the existing Phase 12 path:

1. local speaker verification;
2. local transcription;
3. only then, for the owner or a legitimate guest, the transcript goes to
   AgentCore and its model router.

The routes are covered in the **Implementation** section below.

## Why there is no dedicated keyword-spotting model

No maintained, offline, pinned keyword model for the name "Sam" was
available without training one or adding a cloud service. The design instead:

- **Stage 1**, in the app: a deterministic energy VAD. It costs almost no CPU
  and makes no network call.
- **Stage 2**, in the backend: a local Whisper pass, only on short speech
  segments, bounded by a rate limit.

This reuses the verified local model; no new dependency or model download was
added. Whisper does **not** decode continuous room audio: silence never
reaches it, and segments longer than 4 s are not checked.

## False wakes

| Control | Value |
|---|---|
| Name position | must be among the first two words (after "hey", "ok", etc.): "I told Sam..." does not wake |
| Whole words only | "Samuel", "same" do not wake |
| Minimum speech | 120 ms (clicks are ignored) |
| Cooldown after a non-wake segment | 0.7 s |
| Backend rate limit | 30 wake checks per minute; then a 10 s app back-off |
| Segment length | 0.25 to 4 s |

A wake by someone else only opens a conversation. Their first turn fails
speaker verification, so nothing reaches the agent and Sam goes back to
sleep.

## Owner identity

- **Setup:** voice activation needs an enrolled owner voice profile. Without
  one, Home shows **Set up your voice** (and Action Required does too)
  instead of listening.
- **Reasons:** voice results now say why:
  - `voice_not_enrolled`: set up your voice;
  - `owner_verification_required`: Sam didn't recognize your voice;
  - `voice_identity_unavailable`;
  - `voice_not_heard`.

  Previously a missing profile was reported as the generic "Owner
  verification required".
- **Step-up secret:** enrollment requires the step-up secret. In production
  it resolves from the Keychain, like every credential.
  - **Normal setup (no Terminal):** Settings › Owner voice › *Set up owner
    verification*. The owner chooses the secret (typed twice, password
    fields, at least 16 characters). The backend stores it only through the
    Keychain boundary (a write-only capability for this one credential),
    keeps it in memory like a secret resolved at startup, and begins
    enrollment in the same request. The app clears both fields before the
    request is sent and never stores the value. This works only while no
    step-up secret exists; Guest Mode is refused.
  - **Admin / recovery:** `python -m sam.system.cli secret-set
    desktop_step_up_secret` (no echo) remains, and is the only way to
    *replace* an existing secret.

## Authorization is unchanged

Waking and conversation are **authentication-adjacent UX, not
authorization**:

- Voice activation adds no grant.
- Career SUBMIT/SEND and DELETE still require their confirmations.
- CRITICAL actions still require the step-up secret.
- A guest can never satisfy a confirmation.
- Voice activation does not touch the Proactive scheduler.

Tests cover each of these.

## Barge-in

While Sam thinks or speaks, the microphone input is **discarded**. Without
reliable acoustic echo cancellation between the Mac's speakers and
microphone, Sam would otherwise hear itself. Interrupting is therefore
explicit: the **stop** control ends the reply, and Sam listens again. Real
voice barge-in is future work and is **not** faked.

## Implementation

- **Backend:**
  - `sam.voice.wake`: wake word, stop phrases, rate limiter;
  - `POST /desktop/v1/voice/wake`: one bit, local recognizer only;
  - `POST /desktop/v1/voice/activation`: owner-only;
  - `voice/utterance` gained `hands_free` (whole-utterance stop phrases are
    held before the agent);
  - owner setting `voice.activation` (schema v3; the `owner_settings` table
    is rebuilt with its rows copied);
  - CLI: `voice-activation on|off|status`.
- **Shell:** two new fixed commands (`sam_voice_wake`,
  `sam_voice_activation`). `sam_voice_utterance` now forwards `language`
  (previously dropped) and `hands_free`.
- **App:**
  - `lib/vad.ts` and `lib/listener.ts`: local VAD and hands-free capture;
  - `lib/localSpeech.ts`: on-device voices only (`localService`);
  - `session.tsx`: the conversation state machine;
  - Home and Settings.

## Fresh installs

Voice activation is **off** by default. The Settings text explains exactly
what it does before the owner turns it on.

Each setup stage is its own state, with its own message and action (never a
generic "Owner verification required"):

| `setup_state` | Owner sees | Action |
|---|---|---|
| `models_missing` | Install voice components (size, source, licenses) | owner-started install |
| `restart_required` | Components installed and verified | Restart Sam's engine |
| `setup_required` | Set up owner verification | choose step-up secret, then enroll |
| `not_enrolled` | Set up your voice | enroll 3–5 samples |
| `enrolled` | ready | – |
| `voice_unavailable` | voice identity is off or the Keychain is unusable | – |

Text works in every state.

### Voice components (models)

Nothing is bundled and nothing downloads on launch. The owner starts the
install from Settings (`POST /desktop/v1/voice/models/install`,
`sam.voice_local.install`):

- exactly the registry's pinned files: SpeechBrain ECAPA (Apache-2.0) and
  Whisper `small` (MIT), ≈ 570 MB, each pinned to a commit, a size and a
  SHA-256;
- `https://huggingface.co/<repo>/resolve/<commit>/<file>`; redirects are
  followed manually, HTTPS only, to `huggingface.co` or `*.hf.co`, at most
  5; proxies and other environment settings are ignored;
- connect 15 s, read 60 s, one-hour overall deadline;
- streamed into a private staging directory, cut off above the pinned size,
  hashed while streaming, and renamed into place only as a complete,
  verified set. A failure leaves voice unavailable and the app usable;
- no telemetry, token or account; no code is executed from a download.

Verified models activate through a backend restart (the normal startup
path), never by hot-wiring a running backend.
