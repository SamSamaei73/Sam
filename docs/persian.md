# Persian (Farsi) as a first-class language (Phase 12)

> **Persian is a first-class Sam language.** Language state is
> presentation/instruction only - never authorization.

## Language policy

`sam.language.LanguagePolicy` derives, from the user's preference
(`auto` / `فارسی` / `English`) and the input, a `LanguageDecision`: response
language, direction (`rtl`/`ltr`) and TTS language.

* `auto` (default) follows the speaker: the recognizer's language tag if
  present, else script detection (Persian vs Latin **word** counts; a tie
  follows the first word). A Persian sentence containing `API`, `Docker`,
  `FastAPI` stays Persian; an English sentence with two Persian words stays
  English.
* An explicit preference overrides detection.
* AgentCore receives `response_language` as a separate keyword (the
  `AgentRequest` model is unchanged) and prepends a **fixed, reviewed system
  instruction**: answer in Persian script, keep technical terms/code/URLs/
  product names in Latin form, never transliterate Persian. The instruction
  text is static; it is never built from user or model text.

## Speech-to-text

`LocalWhisperTranscriptionProvider` (`faster-whisper`, offline after setup;
multilingual models only) implements the Phase 9 protocol. The user's language
preference reaches it as a request-scoped hint (`fa`/`en`), because
auto-detection on very short clips is unreliable.

**Measured (7 real Persian recordings: 1-2 word Wikimedia Commons/Lingua Libre
clips, CC0 / CC-BY-SA 4.0, converted to 16 kHz WAV; temporary, not committed):**

| Expected | small + `fa` hint | medium + `fa` hint |
|---|---|---|
| سلام | سالو | **سلام** |
| صبح بخیر | سو بحب خی | صبح بخه |
| شیراز | چی را از | چی راست |
| هزار | خزار | **هزار** |
| سپتامبر | سپتونف | سبتم |
| پرنده | باراند | برانده |
| خرما | خورم آو | خورما |

Exact matches: `small` 0/7, `medium` 2/7 (several near misses). Without the hint,
language auto-detection picked English for most of these ~1 s clips. **These
are hard, isolated-word clips; no long Persian speech was available, so
sentence-level accuracy was not measured and no quality claim is made.** English
sentences transcribed correctly (`small`). Whisper decodes one language per
window, so code-switching inside one utterance is best effort.
`medium` is more accurate but ≈ 4× slower on this machine (see
`docs/voice_identity.md`).

## RTL and rendering

* Each chat message carries its own `lang`/`dir` (`fa`+`rtl` for Persian). The
  rest of the app does **not** flip just because one message is Persian.
* Fenced code blocks, inline code and URLs are pinned `dir="ltr"` inside Persian
  text. Text is rendered as React text nodes (never HTML/Markdown), so markup
  in a reply is inert.
* Persian digits, ZWNJ (`می‌کند`) and mixed Latin terms round-trip unchanged
  (tested); nothing is transliterated.
* The composer uses `dir="auto"`.
* **Interface language** (Settings): English or فارسی. Choosing Persian sets
  `<html lang="fa" dir="rtl">` and shows reviewed static strings for
  navigation, chat, connection, microphone, Owner voice, Guest Mode,
  Enrollment, confirmations and key errors. Other pages (Knowledge, Memory,
  Tools, Permissions, Activity) remain English. The Persian strings are static
  and **should be reviewed by a native speaker before release**.

## Persian speech output (TTS)

TTS stays provider-independent and language-aware. Each trusted voice profile
declares the languages it can speak (`speech_profiles[].languages`). The UI
shows **Read aloud** only for a response language some trusted profile speaks,
and the backend enforces it: a request whose language the chosen profile can't
speak returns `language_unsupported` - **text only**, no provider call, no
substitute voice.

The Phase 10 Fish profile is treated as **English only**: Fish's documentation
does not list Persian for any model, so Sam does not send Persian to it.

### Optional Gemini Persian TTS

Persian speech comes from an **optional** Gemini provider (`sam_persian`
profile, `gemini_tts.py`):

* **Optional and disabled by default.** It exists only when `GEMINI_API_KEY`
  is configured locally. With no key there is no Persian voice and Persian
  replies are **text-only**.
* **Persian synthesis text is sent to Google** (an external service, free
  tier). Every synthesis asks for confirmation first (Phase 11 SEND grant);
  secret-looking text is withheld and makes **zero** network calls. Settings
  shows this disclosure.
* **The credential stays local**: it is read once by `Settings`, injected at the
  provider boundary, sent only in the `x-goog-api-key` header, and never
  appears in a URL, request model, log, audit event, error, `repr`, the UI,
  Memory, Knowledge or Obsidian.
* **`PAID_FALLBACK = OFF`. Sam never enables billing**, upgrades a tier, buys
  credits or falls back to another provider (there is no Gemini -> Fish path).
* **Quota, error, timeout or no key -> text-only.** A 429 (free quota
  exhausted), 401/403, a 5xx, a timeout or a malformed response each become a
  safe typed result. There is exactly one request per synthesis and **no
  automatic retry** (Google's guidance to retry rare 500s is deliberately not
  followed).
* Endpoint (`POST https://generativelanguage.googleapis.com/v1beta/interactions`)
  and model (`gemini-3.1-flash-tts-preview`) are constants in Sam's code; the
  voice comes from the trusted profile. No request, LLM or provider response can
  change them. The client refuses redirects and ignores proxy/environment
  settings. Output is capped, decoded from `output_audio.data` as 24 kHz mono
  16-bit PCM, wrapped into a WAV by Sam and validated again by the gateway.
* Automated tests use only mocked HTTP; no test needs a network or a key.

**Live status: OPTIONAL / CURRENTLY UNAVAILABLE on the owner's Google project
(recorded 2026-09).** The request Sam sends matches Google's documented
Interactions TTS schema (endpoint, `x-goog-api-key` header, `model`, `input`,
`response_format`, `generation_config.speech_config`). Two owner-authorized live
requests were made in total (one smoke, one diagnostic). Both returned **HTTP
400**. The error body was **not a JSON object**, contrary to the documented
error contract, so no error code or message could be extracted. Nothing was
retried, no other provider was tried, billing was not enabled, and **no live
Persian audio was ever generated**: the response-to-audio path
(`output_audio.data` -> WAV) is verified only against mocks. Treat Gemini
Persian TTS as *implemented but unverified live*. Safe behavior when it is
unavailable is unchanged: **no retry, no alternate provider, no paid fallback,
text-only response.** Do not make further live calls without a new, explicit
decision.

## Measured local STT quality (owner recordings, 2026-09)

Real owner recordings, faster-whisper on CPU, `int8`, local hash-verified model
directory, `HF_HUB_OFFLINE=1`, **socket connections disabled during inference**
(no network was used), language auto-detected, one speaker, three clips of
9-12 s. Latency covers decoding; model load was 1.9 s (small) and 4.4 s
(medium). Similarity is a normalized character-match ratio, not a formal word
error rate. **One speaker and three clips is not a benchmark.**

| Clip | Model | Detected | Latency | Similarity |
|---|---|---|---|---|
| Persian (9.0 s) | small | `fa` | 7.6 s | 0.90 |
| Persian (9.0 s) | medium | `fa` | 22.6 s | 0.94 |
| Persian + English terms (11.9 s) | small | `fa` | 5.0 s | 0.79 |
| Persian + English terms (11.9 s) | medium | `fa` | 18.2 s | 0.65 |
| English (10.3 s) | small | `en` | 3.5 s | 1.00 |
| English (10.3 s) | medium | `en` | 12.0 s | 1.00 |

* **English: exact** on both models.
* **Persian: approximate.** Both models spell colloquially and garble some
  words (for example "هوش مصنوعی" and "معماری"); medium is closer than small.
* **Mixed Persian-English: best-effort.** Whisper decodes one language per
  window, so technical terms are often mangled (`FastAPI`, `API endpoint`,
  `RAG`). Small kept some Latin terms; **medium was worse**, rewriting them in
  Persian script and adding words that were not spoken. Larger is not
  uniformly better on mixed speech and is 3-4x slower on CPU.
* Treat Persian transcripts as **approximate**, keep the transcript visible to
  the owner, and do not rely on them for exact commands or technical terms.

## Cost and privacy

* Local STT and speaker verification: **no network**, no per-use cost.
* `PAID_FALLBACK = OFF`: no code path enables billing, upgrades a tier, or
  falls back from an unavailable/limited voice to another (paid) provider - a
  scan test asserts no fallback/billing/paid logic exists in the desktop code.
  If a voice is unavailable the response is text-only.
* Read-aloud (external providers) sends the response text off the device only
  when configured and explicitly requested, and every such synthesis asks for
  confirmation (Phase 11).
