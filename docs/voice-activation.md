# Hands-free voice activation ("Sam")

Status: local voice pipeline improvements implemented. Short-phrase owner
verification remains unreliable and is not a completed fix.

## Changes and remaining limits

The installation has one owner. Every command, including a follow-up, receives
a fresh speaker check before transcription or a model call. Voice matching
does not replace the existing permissions or sensitive-action confirmations.

| Area | Implemented behavior |
|---|---|
| Microphone capture | Encode owned PCM before clearing it; share microphone processing settings across enrollment and commands; anti-alias recorded input when downsampling. |
| Wake handoff | Preserve bounded audio arriving while wake recognition is pending, retain active speech, and reject stale callbacks. A separately spoken bare wake name is not included in the following command. |
| Turn detection | Use a local Silero v6 worker and AudioWorklet with bounded queues, one inference thread and a 600 ms quiet interval. Suspend capture during model replies and speech playback. |
| Complete requests | Support complete 16 kHz mono turns up to 60 seconds; reject a capture that reaches the cap instead of answering a truncated request. |
| Spoken answers | Use matching native macOS voices, wait for playback or cancellation to finish, and show failures. Approved cloud speech can use the configured OpenRouter key. |
| Language | Preserve supported ASR language tags through the answer and speech paths. Enrollment checks the speaker independently of words or language. |
| Follow-ups | Retain bounded, expiring conversation text in memory. The context handle never replaces a fresh voice match. |
| Speaker preparation | Apply the same speech trimming to enrollment and verification. Enrollment needs two seconds of detected speech; verification permits clips with at least 250 ms, subject to quality and identity checks. |
| Identity status | Discard stale status responses so an older request cannot overwrite a newer verification result. |

**Known unresolved issue:** in the final owner microphone trial, short requests
containing approximately 1.57 and 1.28 seconds of detected speech were rejected,
while a longer request containing 3.20 seconds verified and answered. The three
recordings had usable levels, no detected clipping and buffered speech edges.
Independent decoding, speech preparation, features and embedding inference
agreed with the reference pipeline. This narrows the failure to model/profile/
scoring behavior; it does not establish its exact cause or biometric accuracy.
No owner recording, template or similarity score was saved for debugging.

`LOCAL_SPEAKER_MODEL` defaults to `ecapa`; `wespeaker` is an optional alternative,
not a proven short-command fix. Changing model or preprocessing revision
requires explicit re-enrollment because embedding spaces are incompatible.
Existing Keychain profiles are preserved until enrollment completes. Do not
repeatedly re-enroll a compatible profile as a workaround for short commands.
The default cosine cutoff remains 0.50; no lower-threshold fallback is used.
See [verification limits](voice_identity.md#verification-and-threshold).

The quiet interval is acoustic endpointing, not semantic understanding of
whether a sentence is complete. Wake detection still uses local Whisper after
speech detection rather than a dedicated keyword model. Local inference is
thread-bounded, but active latency, memory and room-noise behavior still need
measurement on the target laptop. These changes do not establish Siri-level
resource use or reliable recognition of every brief utterance.

Generic WAV uploads also have an 8 MiB byte cap. All input limits apply together;
higher sample rates or multiple channels can reach that cap before 60 seconds.
The desktop captures 16 kHz mono audio, which fits within the byte cap.

Before release, validate fresh owner and other-speaker recordings, short and
long requests, supported languages, pauses, microphone denial, cancellation,
window hiding and repeated follow-ups. Automated regression tests cover signal
handling and state transitions; they cannot establish biometric accuracy.

## What the owner experiences

1. With **Voice activation** on, Home shows Sam waiting quietly: *Say "Sam" to start*.
2. The owner says **"Sam"**. Sam wakes up; this happens locally, see below.
3. The owner speaks. When they stop talking, the turn ends by itself (voice
   activity detection). There is no button.
4. Sam checks the owner's voice **on this Mac**, thinks, answers on screen and
   **speaks the answer with a macOS on-device voice**. If that language has no
   installed native voice, Sam offers the existing provider speech approval;
   with OpenRouter configured, that uses the same API key. Playback failures
   are shown explicitly and the written answer remains available.
5. Sam then listens for the next turn. There's no need to say "Sam" again.
   Every turn independently verifies the owner. Turns may last up to 60 seconds;
   reaching that limit asks for a shorter request instead of submitting part
   of it. Short questions such as “Who are you?” are evaluated without a
   one-second speech requirement. A clip still needs at least 250 ms of
   detected speech and a successful fresh voice match; ambiguous recordings
   may require a retry.
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

### Manual validation

If Settings reports a compatible enrolled profile, keep it. When setup is
required, install the selected local models and explicitly enroll through the
existing owner step-up flow. Record three different full sentences, 4–6 seconds
each, at the usual microphone distance. English examples are provided, but
enrollment does not require a particular language.

Return to Home, leave **Voice activation** on and use **Automatic** for response
language. Then:

1. Say “Sam, who are you?” as one sentence, at your normal pace.
   Confirm the whole question appears and the answer is both written and spoken.
2. After Sam finishes speaking, ask “What can you do?” without repeating the
   wake word. Then ask “Please explain your first suggestion in more detail.”
   Confirm it uses the previous answer and verifies each new recording.
3. Ask a complete question in another language you speak. Check the recognized
   language, written answer and spoken answer. An installed matching macOS
   voice is preferred; a missing voice can require provider speech approval.
4. Try another short question and a longer question below 60 seconds, including a
   brief pause. A pause longer than about 600 ms can end the turn; semantic
   completion is not guaranteed. A one-word reply may need more speech to
   verify its speaker.
5. Say “Sam, go to sleep,” or hide the window. Confirm listening ends. Return
   to Home and say the wake word to start a new conversation.
6. Have a different person ask the same short question. In owner-only mode,
   their request must be rejected before it reaches the agent.

If a short question fails, also try **Talk now → “Who are you?”** and a longer
question through the same control. This separates matching from wake capture.
Do not remove the profile or repeatedly enroll as a workaround; report the
notice and which step failed. No owner recordings need to be saved or shared.

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
- **What is kept:** audio is held only in bounded in-memory segments with a
  **300 ms pre-roll**. While a wake check is pending, subsequent command audio
  is retained within the same **60-second maximum** so speech during local
  recognition is not lost. Rejected wake candidates and their pending audio
  are discarded.
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

Successful owner exchanges are kept only in backend process memory, up to
eight completed pairs and 24,000 characters. The conversation ID locates text;
it is never an identity credential or authorization. The app erases context on
sleep, hide, navigation and rejection; the backend also invalidates it on
identity changes and has a ten-minute idle and thirty-minute absolute expiry.
No conversation audio, embeddings or verification result is cached there.

The routes are covered in the **Implementation** section below.

## Current wake implementation

The current implementation reuses the local speech recognizer. A dedicated
keyword detector is a separate model-selection and validation task described
in the audit above. The existing two-stage path is:

- **Stage 1**, in the app: AudioWorklet capture at a checked 16 kHz sample rate
  and Silero v6 inference in a dedicated worker with one inference thread.
  The 512-sample frames are processed in order; queue overflow fails visibly
  instead of dropping syllables. Input and inference pause while Sam replies.
  Speech starts at probability 0.5, releases at 0.35, and a turn ends after
  600 ms of quiet (608 ms at the frame boundary). This detects speech activity,
  not semantic completion: a longer thinking pause can still end a turn.
- **Stage 2**, in the backend: a local Whisper pass, only on short speech
  segments, bounded by a rate limit.

The browser model comes from pinned `@ricky0123/vad-web@0.0.31`; the build checks
its SHA-256 before copying it alongside `onnxruntime-web@1.22.0` WASM assets.
All runtime asset paths are local, with no CDN, telemetry or API key. A narrow
CSP allowance permits same-origin assets, workers and WebAssembly compilation;
general JavaScript evaluation and remote connections remain blocked. There is
no silent fallback to volume-only detection if loading fails. The error offers
a retry and push-to-talk remains available. Whisper does **not** decode
continuous room audio, and wake segments longer than 4 s are not checked.

A real macOS WKWebView smoke test processed 64 generated silence frames through
the built AudioWorklet, worker, WASM and model in 2.303 seconds, with a maximum
worker roundtrip of 6 ms and zero CSP violations. All seven asset requests
stayed on loopback. This validates runtime compatibility and queue throughput;
it does not replace owner microphone, room-noise or speaker-accuracy tests.

Whisper uses two CPU threads and one worker. Wake and command decoding use
greedy decoding without temperature retries. For command input, one encoder
output is shared between detection across Whisper's 100 canonical languages
and transcription. The actual winning language is retained; it is not
re-ranked into an English/Persian subset. With response language set to
Automatic, that language drives both the written and spoken answer. For
clips longer than 30 seconds, the upstream decoder encodes the second window
and keeps its complete text under the same deadline. An
answer-language preference does not force the language spoken. Native speech
requires an installed matching voice; the configured OpenRouter speech model
advertises its separate, documented 57-language capability, not all recognizer
languages. See [speech output](tts.md). Both speaker providers use two
CPU threads; WeSpeaker's ONNX workers do not spin while idle. Bundled Silero
runs locally with one thread. These bounds and
the idle UI fixes reduce avoidable work; they are not
a claim of Siri-level resource use. Measure idle, noisy-room, wake, verification
and response CPU separately. Use a built frontend during interactive testing
to avoid development reloads interrupting microphone or playback state.

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
    held before the agent) and an optional context-only `conversation_id`;
  - `sam.desktop.conversation`: bounded ephemeral text, stale-turn protection
    and expiry; `POST /desktop/v1/voice/conversation/end` explicitly erases it;
  - owner setting `voice.activation` (schema v3; the `owner_settings` table
    is rebuilt with its rows copied);
  - CLI: `voice-activation on|off|status`.
- **Shell:** two new fixed commands (`sam_voice_wake`,
  `sam_voice_activation`). `sam_voice_utterance` now forwards `language`
  (previously dropped) and `hands_free`.
- **App:**
  - `lib/vad.ts` and `lib/listener.ts`: local VAD and hands-free capture;
  - `lib/localSpeech.ts`: native macOS speech commands in the desktop; delayed
    local voice discovery in browser previews;
  - `src-tauri/src/local_speech.rs`: bounded native speech with cancellation,
    fixed executable/options and reply text passed only through stdin;
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

An existing profile with `profile_compatible: false` is preserved and reports
`not_enrolled` until its owner explicitly re-enrolls using the saved owner secret.

Text works in every state.

### Voice components (models)

The small browser speech detector and its runtime are bundled at build time.
Whisper and the selected speaker model never download on launch: the owner starts their
install from Settings (`POST /desktop/v1/voice/models/install`,
`sam.voice_local.install`):

- exactly the registry's pinned files: SpeechBrain ECAPA (Apache-2.0, default)
  or WeSpeaker ResNet34-LM (CC-BY-4.0, opt-in), plus Whisper `small` (MIT),
  ≈ 570 MB or 513 MB respectively, each pinned to a commit, a size and a SHA-256;
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
