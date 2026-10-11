import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { BridgeError, toBridgeError } from "./bridge/bridge";
import type { OperationResult, ResponseLanguage, VoiceSetupState } from "./bridge/types";
import type { ChatMessage } from "./components/MessageBubble";
import { translate } from "./i18n/strings";
import { playSynthesizedAudio, type Playback } from "./lib/audioPlayback";
import { detectLanguage, directionFor, languageBase, uiLanguageFor } from "./lib/language";
import { Listener } from "./lib/listener";
import { localSpeechOutput, type LocalSpeechOutput, type SpeechHandle, type SpeechOutcome } from "./lib/localSpeech";
import { Recorder, RecorderError } from "./lib/recorder";
import { SLEEP_TIMING, TURN_TIMING, type SegmentEnd } from "./lib/vad";
import { MAX_RECORDING_SAMPLES, TARGET_SAMPLE_RATE, joinWavSegments } from "./lib/wav";
import { useSam } from "./state";

/**
 * Sam's interaction state. Every value is derived from something REAL:
 *
 * - listening: the microphone is actually capturing (an owner gesture started it)
 * - thinking:  a request is actually in flight to Sam's backend
 * - speaking:  synthesized audio is actually playing (the owner asked for it)
 * - permission: a confirmation dialog is actually waiting for the owner
 * - error:     the last operation actually failed (shown briefly)
 * - offline:   Sam's backend is actually unreachable
 * - idle:      none of the above
 *
 * Push-to-talk capture starts only from a click or a key press on the voice
 * control and ends on stop, cancel or the time limit.
 *
 * Hands-free (voice activation, an owner-only setting that is OFF until the
 * owner turns it on) adds, again only from real state:
 *
 * - sleeping:  the mic is open for the LOCAL wake word only ("Sam"); speech
 *              segments are checked by Sam's local recognizer and dropped
 * - waking:    the wake word was just recognized
 * - attending: a conversation is open and Sam waits for the owner's turn
 *
 * It runs only while Home is open and the window is visible; leaving Home,
 * hiding the window or quitting closes the microphone.
 */
export type VoiceState =
  | "idle"
  | "sleeping"
  | "waking"
  | "attending"
  | "listening"
  | "thinking"
  | "speaking"
  | "permission"
  | "error"
  | "offline";

export type HandsFreePhase = "off" | "sleeping" | "waking" | "attending" | "thinking" | "speaking";

export interface HandsFreeInfo {
  /** The hands-free loop is running right now (mic open for the wake word). */
  active: boolean;
  phase: HandsFreePhase;
  activation: "on" | "off" | "unavailable";
  /** The owner's voice isn't ready yet (models, owner security or enrollment). */
  needsSetup: boolean;
  /** Which setup step comes next (drives the Home hint and Action Required). */
  setupState: VoiceSetupState | null;
  /** macOS microphone access was refused (not a Sam permission). */
  micDenied: boolean;
  /** The local listening pipeline stopped and may be retried. */
  micFailed: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  messages: ChatMessage[];
}

export interface Exchange {
  heard: ChatMessage | null;
  reply: ChatMessage | null;
}

export const ERROR_VISIBLE_MS = 6_000;
/** A conversation closes after this long without the owner speaking. */
export const CONVERSATION_IDLE_MS = 20_000;
/** After a segment that was not the wake word, ignore speech this long. */
export const WAKE_COOLDOWN_MS = 700;
/** After the backend rate-limits wake checks, back off this long. */
export const WAKE_BACKOFF_MS = 10_000;
/** How long the "awake" flash shows after the wake word. */
export const WAKE_FLASH_MS = 900;
/** Longest segment worth checking for the wake word. */
const WAKE_MAX_SECONDS = 4;
const IDENTITY_STOPS = new Set([
  "owner_verification_required",
  "voice_not_enrolled",
  "voice_identity_unavailable",
  "identity_not_configured",
]);

interface BufferedVoiceSegment {
  audio: string;
  samples: number;
  voicedSeconds: number;
}

interface PendingWakeCheck {
  generation: number;
  segments: BufferedVoiceSegment[];
  samples: number;
}

const bufferedSamples = (segments: readonly BufferedVoiceSegment[]) =>
  segments.reduce((total, segment) => total + segment.samples, 0);

/** The wake candidate and its command share the same bounded capture budget. */
function captureRemaining(listener: Listener, usedSamples: number): void {
  if (usedSamples >= MAX_RECORDING_SAMPLES) listener.pause();
  else listener.setTiming({
    endSilenceMs: TURN_TIMING.endSilenceMs,
    maxSegmentMs: ((MAX_RECORDING_SAMPLES - usedSamples) / TARGET_SAMPLE_RATE) * 1000,
  });
}

let conversationCounter = 0;
let messageCounter = 0;
const newConversation = (): Conversation => ({
  id: `c${++conversationCounter}`,
  title: "New conversation",
  messages: [],
});
const nextId = () => `m${++messageCounter}`;

function titleFrom(messages: ChatMessage[]): string {
  const first = messages.find((m) => m.role === "user" || m.role === "guest");
  if (!first) return "New conversation";
  const text = first.text.replace(/\s+/g, " ").trim();
  return text.length > 36 ? `${text.slice(0, 36)}…` : text;
}

function failureText(error: unknown): string {
  return (error instanceof BridgeError ? error : toBridgeError(error)).message;
}

function resultText(result: OperationResult): string {
  return result.message ?? "That couldn't be completed.";
}

interface SessionValue {
  conversations: Conversation[];
  active: Conversation;
  selectConversation: (id: string) => void;
  newConversation: () => void;
  clearActive: () => void;
  /** The most recent exchange, for the transient transcript (not the log). */
  exchange: Exchange | null;
  dismissExchange: () => void;
  voice: VoiceState;
  /** Real microphone level while listening (0..1), otherwise 0. */
  level: number;
  recordingSeconds: number;
  error: string | null;
  micAvailable: boolean;
  chatAvailable: boolean;
  startListening: () => Promise<void>;
  stopListening: () => Promise<void>;
  cancelListening: () => void;
  sendText: (text: string, privateMessage: boolean) => Promise<void>;
  /** A text message is in flight (voice is unaffected). */
  textSending: boolean;
  /**
   * The owner's unfinished text draft: memory only, never persisted, never
   * read by the voice path, and cleared only when the owner sends it.
   */
  textDraft: string;
  setTextDraft: (text: string) => void;
  speakingId: string | null;
  speak: (message: ChatMessage) => Promise<void>;
  stopSpeaking: () => void;
  profileFor: (language: ResponseLanguage) => string | null;
  handsFree: HandsFreeInfo;
  /** Home tells the session whether it is on screen (hands-free runs only there). */
  setHomeActive: (active: boolean) => void;
  /** Manual fallback for the wake word (a click or key press). */
  wakeNow: () => void;
  /** End the conversation and go back to waiting for "Sam". */
  sleepNow: () => void;
}

const SessionContext = createContext<SessionValue | null>(null);

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("SessionProvider is missing");
  return value;
}

export function SessionProvider({
  children,
  speech = localSpeechOutput(),
}: {
  children: ReactNode;
  /** Local voice for voice replies; unavailable voices use confirmed speech. */
  speech?: LocalSpeechOutput | null;
}) {
  const {
    bridge,
    status,
    identity,
    connection,
    prefs,
    confirmable,
    confirmationPending,
    t,
    refreshIdentity,
    audioEnvironment,
  } = useSam();
  // Conversation history is session-local React state: never persisted.
  const [conversations, setConversations] = useState<Conversation[]>(() => [newConversation()]);
  const [activeId, setActiveId] = useState<string>(() => conversations[0]?.id ?? "");
  const active = conversations.find((c) => c.id === activeId) ?? conversations[0]!;
  const [exchange, setExchange] = useState<Exchange | null>(null);
  const [busy, setBusy] = useState(false);
  const [textSending, setTextSending] = useState(false);
  const [textDraft, setTextDraft] = useState("");
  const [recording, setRecording] = useState(false);
  const [recordingSeconds, setRecordingSeconds] = useState(0);
  const [level, setLevel] = useState(0);
  const levelMeter = useRef({ value: 0, time: -Infinity });
  const publishLevel = useCallback((value: number) => {
    // The meter needs a smooth visual cadence, not an update for every audio
    // buffer. Quantization also keeps steady room noise from rerendering Home.
    const next = Math.round(value * 50) / 50;
    const now = performance.now();
    if (next === levelMeter.current.value) return;
    if (next > 0 && now - levelMeter.current.time < 80) return;
    levelMeter.current = { value: next, time: next === 0 ? -Infinity : now };
    setLevel(next);
  }, []);
  const [speakingId, setSpeakingId] = useState<string | null>(null);
  const [error, setErrorState] = useState<string | null>(null);
  const recorder = useRef<Recorder | null>(null);
  const phaseRef = useRef<HandsFreePhase>("off");
  const lastVoiceLanguage = useRef<ResponseLanguage | null>(null);
  const speechHandle = useRef<SpeechHandle | null>(null);
  const speechGeneration = useRef(0);
  const automaticSpeech = useRef<(message: ChatMessage, isCurrent: () => boolean) => Promise<void>>(async () => undefined);
  const playback = useRef<Playback | null>(null);
  const finishRef = useRef<() => void>(() => undefined);
  const errorTimer = useRef<number | undefined>(undefined);
  // Text context only: every request still supplies fresh audio for owner verification.
  const voiceContextId = useRef<string | undefined>(undefined);
  const contextGeneration = useRef(0);
  const contextEnd = useRef<Promise<void> | null>(null);
  const clearVoiceContext = useCallback(() => {
    const id = voiceContextId.current;
    voiceContextId.current = undefined;
    contextGeneration.current++;
    // Serialize erasure before the next turn, including a first turn still in flight.
    const ending = (contextEnd.current ?? Promise.resolve())
      .then(() => bridge.endVoiceConversation(id))
      .then(() => undefined, () => undefined);
    contextEnd.current = ending;
    void ending.then(() => { if (contextEnd.current === ending) contextEnd.current = null; });
  }, [bridge]);

  const setError = useCallback((message: string | null) => {
    window.clearTimeout(errorTimer.current);
    setErrorState(message);
    if (message) errorTimer.current = window.setTimeout(() => setErrorState(null), ERROR_VISIBLE_MS);
  }, []);

  useEffect(
    () => () => {
      recorder.current?.cancel();
      playback.current?.stop();
      speechGeneration.current++;
      speechHandle.current?.stop();
      clearVoiceContext();
      window.clearTimeout(errorTimer.current);
    },
    [clearVoiceContext],
  );

  useEffect(() => {
    if (!recording) return;
    const started = Date.now();
    const timer = window.setInterval(() => setRecordingSeconds(Math.floor((Date.now() - started) / 1000)), 250);
    return () => window.clearInterval(timer);
  }, [recording]);

  const append = useCallback(
    (message: Omit<ChatMessage, "id">): ChatMessage => {
      const created = { id: nextId(), ...message };
      setConversations((current) =>
        current.map((c) => {
          if (c.id !== activeId) return c;
          const messages = [...c.messages, created];
          return { ...c, messages, title: titleFrom(messages) };
        }),
      );
      return created;
    },
    [activeId],
  );

  const micAvailable = status?.voice_input === "configured";
  const chatAvailable = status?.agent === "configured" && connection !== "unavailable";

  const micError = useCallback(
    (code: string) =>
      t(`mic.${code === "denied" || code === "unsupported" || code === "empty" || code === "limit" ? code : "failed"}` as "mic.failed"),
    [t],
  );

  const sendVoice = useCallback(
    async (audioBase64: string) => {
      const generation = speechGeneration.current;
      const context = contextGeneration.current;
      const isCurrent = () => context === contextGeneration.current && generation === speechGeneration.current;
      setBusy(true);
      try {
        if (contextEnd.current) await contextEnd.current;
        if (!isCurrent()) return;
        const result = await confirmable((confirmationId) =>
          bridge.voiceUtterance(audioBase64, confirmationId, prefs.responseLanguage, false, voiceContextId.current),
        );
        if (!isCurrent()) return;
        voiceContextId.current = result.status === "ok" ? result.conversation_id ?? undefined : undefined;
        if (result.status !== "ok") clearVoiceContext();
        void refreshIdentity();
        if (result.reason_code === "owner_verification_required") {
          // A non-owner speaker is stopped before any transcription: a fixed
          // notice, never a transcript.
          const notice = append({ role: "notice", text: t("voice.ownerRequired"), status: "failed" });
          setExchange({ heard: null, reply: notice });
        } else if (result.status === "ok" && result.transcript) {
          lastVoiceLanguage.current = result.language ?? detectLanguage(result.transcript) ?? "en";
          const heard = append({
            role: result.speaker === "guest" ? "guest" : "user",
            text: result.transcript,
            source: "voice",
            language: result.language ?? detectLanguage(result.transcript),
          });
          const reply = result.reply
            ? append({
                role: "sam",
                text: result.reply,
                status: "ok",
                language: result.language,
                direction: result.direction,
              })
            : null;
          setExchange({ heard, reply });
          if (reply && generation === speechGeneration.current) {
            await automaticSpeech.current(reply, () => generation === speechGeneration.current);
          }
        } else {
          // Secret-looking or otherwise ineligible transcripts are never shown.
          const notice = append({
            role: "notice",
            text: resultText(result),
            status: "failed",
            referenceId: result.reference_id,
          });
          setExchange({ heard: null, reply: notice });
        }
      } catch (failure) {
        setError(failureText(failure));
      } finally {
        setBusy(false);
      }
    },
    [append, bridge, clearVoiceContext, confirmable, prefs.responseLanguage, refreshIdentity, setError, t],
  );

  const stopListening = useCallback(async () => {
    const current = recorder.current;
    if (!current) return;
    recorder.current = null;
    setRecording(false);
    setRecordingSeconds(0);
    publishLevel(0);
    try {
      const recorded = await current.stop();
      await sendVoice(recorded.base64);
    } catch (failure) {
      setError(micError(failure instanceof RecorderError ? failure.code : "failed"));
    }
  }, [micError, publishLevel, sendVoice, setError]);

  useEffect(() => {
    finishRef.current = () => void stopListening();
  });

  const startListening = useCallback(async () => {
    if (recorder.current || busy || phaseRef.current !== "off") return;
    playback.current?.stop();
    setError(null);
    const next = new Recorder(
      audioEnvironment,
      () => finishRef.current(),
      publishLevel,
    );
    recorder.current = next;
    try {
      await next.start();
      if (recorder.current !== next || !next.isRecording) return;
      setRecording(true);
      setExchange(null);
    } catch (failure) {
      if (recorder.current !== next) return;
      recorder.current = null;
      setError(micError(failure instanceof RecorderError ? failure.code : "failed"));
    }
  }, [audioEnvironment, busy, micError, publishLevel, setError]);

  const cancelListening = useCallback(() => {
    recorder.current?.cancel();
    recorder.current = null;
    setRecording(false);
    setRecordingSeconds(0);
    publishLevel(0);
  }, [publishLevel]);

  const sendText = useCallback(
    async (text: string, privateMessage: boolean) => {
      setError(null);
      const heard = append({ role: "user", text });
      setExchange({ heard, reply: null });
      setBusy(true);
      setTextSending(true);
      try {
        const result = await bridge.chat(text, prefs.responseLanguage, privateMessage ? "private" : "normal");
        if (result.status === "ok" && result.reply !== null) {
          const reply = append({
            role: "sam",
            text: result.reply,
            status: "ok",
            referenceId: result.reference_id,
            language: result.language,
            direction: result.direction,
          });
          setExchange({ heard, reply });
        } else {
          const notice = append({
            role: "notice",
            text: resultText(result),
            status: "failed",
            referenceId: result.reference_id,
          });
          setExchange({ heard, reply: notice });
        }
      } catch (failure) {
        const notice = append({ role: "notice", text: failureText(failure), status: "failed" });
        setExchange({ heard, reply: notice });
      } finally {
        setBusy(false);
        setTextSending(false);
      }
    },
    [append, bridge, prefs.responseLanguage, setError],
  );

  const profiles = status?.speech_profiles;
  const profileFor = useCallback(
    (language: ResponseLanguage): string | null => {
      if (status?.speech_output !== "configured") return null;
      const base = languageBase(language);
      if (!base) return null;
      const capable = (profiles ?? []).filter((p) => p.languages.some((candidate) =>
        candidate === language || languageBase(candidate) === base,
      ));
      const preferred = capable.find((p) => p.profile_id === prefs.readAloudVoice);
      return (preferred ?? capable[0])?.profile_id ?? null;
    },
    [prefs.readAloudVoice, profiles, status?.speech_output],
  );

  const stopSpeaking = useCallback(() => {
    speechGeneration.current++;
    playback.current?.stop();
    playback.current = null;
    setSpeakingId(null);
    speechHandle.current?.stop(); // a hands-free reply: back to listening
  }, []);

  const speakThroughProvider = useCallback(
    (message: ChatMessage, isCurrent: () => boolean): Promise<void> => {
      const language = message.language ?? detectLanguage(message.text) ?? "en";
      const profile = profileFor(language);
      if (!profile) {
        setError("Sam couldn't speak this reply. No voice is available for this language.");
        return Promise.resolve();
      }
      return new Promise<void>((resolve) => {
        let done = false;
        let output: Playback | null = null;
        const finish = () => {
          if (done) return;
          done = true;
          output?.stop();
          if (playback.current === output) playback.current = null;
          if (speechHandle.current === handle) {
            speechHandle.current = null;
            setSpeakingId(null);
            setBusy(false);
          }
          resolve();
        };
        const handle: SpeechHandle = { stop: finish };
        speechHandle.current = handle;
        setBusy(true);
        void (async () => {
          try {
            // Recheck cancellation before the confirmed retry sends any text.
            const result = await confirmable((confirmationId) => {
              if (done || !isCurrent()) throw new Error("speech_cancelled");
              return bridge.speak({ text: message.text, voiceProfile: profile, language, confirmationId });
            });
            if (done || !isCurrent()) { finish(); return; }
            if (result.status !== "ok" || !result.audio_base64) {
              setError(result.message ?? "Sam couldn't read that aloud.");
              finish();
              return;
            }
            setSpeakingId(message.id);
            output = playSynthesizedAudio(result.audio_base64, result.audio_format, (failed) => {
              if (failed && isCurrent()) setError("Sam couldn't play this reply. Check your sound output.");
              finish();
            });
            if (!done) playback.current = output;
            else output.stop();
          } catch {
            if (!done && isCurrent()) setError("Sam couldn't read that aloud.");
            finish();
          }
        })();
      });
    },
    [bridge, confirmable, profileFor, setError],
  );

  // ------------------------------------------------------------ hands-free
  const speechRef = useRef(speech);
  useEffect(() => {
    speechRef.current = speech;
  });
  const [homeActive, setHomeActive] = useState(false);
  const [visible, setVisible] = useState(
    () => typeof document === "undefined" || document.visibilityState !== "hidden",
  );
  useEffect(() => {
    const update = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  const [phase, setPhaseState] = useState<HandsFreePhase>("off");
  const [heardSpeech, setHeardSpeech] = useState(false);
  const [justWoke, setJustWoke] = useState(false);
  const [micDenied, setMicDenied] = useState(false);
  const [micFailed, setMicFailed] = useState(false);
  const listenerRef = useRef<Listener | null>(null);
  const idleTimer = useRef<number | undefined>(undefined);
  const wakeTimer = useRef<number | undefined>(undefined);
  const cooldownUntil = useRef(0);
  const wakeBusy = useRef<PendingWakeCheck | null>(null);
  const voiceGeneration = useRef(0);
  const bufferedTurn = useRef<BufferedVoiceSegment[]>([]);
  const runTurnRef = useRef<(audio: string) => Promise<void>>(async () => undefined);
  const invalidateVoiceRequests = useCallback(() => {
    voiceGeneration.current++;
    wakeBusy.current?.segments.splice(0);
    wakeBusy.current = null;
    bufferedTurn.current = [];
    clearVoiceContext();
  }, [clearVoiceContext]);
  const setPhase = useCallback((next: HandsFreePhase) => {
    phaseRef.current = next;
    setPhaseState(next);
    if (next !== "attending") publishLevel(0);
  }, [publishLevel]);

  const activation = status?.voice_activation ?? "unavailable";
  const enrolled = identity?.available === true && identity.enrolled === true && identity.profile_compatible !== false;
  // Owner voice setup still to do: its own state, never a generic block. Not
  // offered in Guest Mode (a guest can't set up or change the owner's voice).
  const setupState: VoiceSetupState | null =
    identity && identity.mode !== "guest_mode" && identity.setup_state !== "enrolled"
      ? identity.setup_state === "voice_unavailable"
        ? null
        : identity.setup_state
      : null;
  const needsSetup = setupState !== null;
  const handsFreeOn =
    activation === "on" &&
    enrolled &&
    micAvailable &&
    (connection === "connected" || connection === "degraded") &&
    homeActive &&
    visible &&
    !micDenied && !micFailed;

  const clearIdle = useCallback(() => window.clearTimeout(idleTimer.current), []);
  const sleep = useCallback(() => {
    clearIdle();
    invalidateVoiceRequests();
    const listener = listenerRef.current;
    if (!listener) return;
    setPhase("sleeping");
    setJustWoke(false);
    listener.setTiming(SLEEP_TIMING);
    listener.resume();
    cooldownUntil.current = Date.now() + WAKE_COOLDOWN_MS;
  }, [clearIdle, invalidateVoiceRequests, setPhase]);

  const rejectLongTurn = useCallback(() => {
    sleep();
    const notice = append({ role: "notice", text: t("mic.limit"), status: "failed" });
    setExchange({ heard: null, reply: notice });
  }, [append, sleep, t]);

  const armIdle = useCallback(() => {
    clearIdle();
    if (phaseRef.current !== "attending" || listenerRef.current?.isSpeaking) return;
    idleTimer.current = window.setTimeout(() => {
      if (phaseRef.current === "attending" && !listenerRef.current?.isSpeaking) sleep();
    }, CONVERSATION_IDLE_MS);
  }, [clearIdle, sleep]);

  const flushBufferedTurn = useCallback(() => {
    const segments = bufferedTurn.current;
    if (segments.length === 0) return;
    bufferedTurn.current = [];
    if (bufferedSamples(segments) >= MAX_RECORDING_SAMPLES) {
      rejectLongTurn();
      return;
    }
    // A size boundary can leave only trailing silence after a bare wake word.
    // It ends capture, but it must not become a speaker-verification request.
    if (!segments.some((segment) => segment.voicedSeconds > 0)) {
      armIdle();
      return;
    }
    try {
      void runTurnRef.current(joinWavSegments(segments.map((segment) => segment.audio)));
    } catch {
      sleep();
      setError("Sam couldn't finish that recording. Please try again.");
    }
  }, [armIdle, rejectLongTurn, setError, sleep]);

  const attend = useCallback((preserveBufferedAudio = false) => {
    const listener = listenerRef.current;
    if (!listener) return;
    setPhase("attending");
    captureRemaining(listener, bufferedSamples(bufferedTurn.current));
    listener.resume({ preserveBufferedAudio });
    clearIdle();
    if (!listener.isSpeaking && bufferedTurn.current.length > 0) flushBufferedTurn();
    else armIdle();
  }, [armIdle, clearIdle, flushBufferedTurn, setPhase]);

  const onSpeech = useCallback((active: boolean) => {
    setHeardSpeech(active);
    if (phaseRef.current !== "attending") return;
    if (active) clearIdle();
    else {
      armIdle();
      // A wake result can arrive during the first few speech frames. If that
      // candidate was just a click, release an earlier completed turn once
      // the segmenter's synchronous end callbacks have finished. A genuine
      // completed segment flushes itself before this check runs.
      queueMicrotask(() => {
        if (phaseRef.current === "attending" && listenerRef.current?.isSpeaking === false && bufferedTurn.current.length > 0) {
          flushBufferedTurn();
        }
      });
    }
  }, [armIdle, clearIdle, flushBufferedTurn]);

  const flashAwake = useCallback(() => {
    setJustWoke(true);
    window.clearTimeout(wakeTimer.current);
    wakeTimer.current = window.setTimeout(() => setJustWoke(false), WAKE_FLASH_MS);
  }, []);

  /** The listener stays paused until actual speech ends or is stopped. */
  const speakLocally = useCallback((text: string, language: ResponseLanguage) => {
    return new Promise<SpeechOutcome>((resolve) => {
      let ended = false;
      let handle: SpeechHandle | null | undefined = null;
      handle = speechRef.current?.speak(text, language, (outcome = "ended") => {
        ended = true;
        if (speechHandle.current === handle) {
          speechHandle.current = null;
          setSpeakingId(null);
        }
        resolve(outcome);
      });
      if (!handle) resolve("unavailable");
      else if (!ended) speechHandle.current = handle;
    });
  }, []);

  const speakAutomatically = useCallback(async (message: ChatMessage, isCurrent: () => boolean) => {
    setSpeakingId(message.id);
    const outcome = await speakLocally(message.text, message.language ?? detectLanguage(message.text) ?? "en");
    if (isCurrent() && (outcome === "unavailable" || outcome === "failed")) {
      await speakThroughProvider(message, isCurrent);
    }
    if (isCurrent()) setSpeakingId(null);
  }, [speakLocally, speakThroughProvider]);

  useEffect(() => { automaticSpeech.current = speakAutomatically; }, [speakAutomatically]);

  const speak = useCallback(async (message: ChatMessage) => {
    const previous = phaseRef.current;
    if (previous === "thinking") clearVoiceContext();
    // Cancel the prior reply's continuation before stopping its playback.
    voiceGeneration.current++;
    wakeBusy.current = null;
    bufferedTurn.current = [];
    const voice = voiceGeneration.current;
    stopSpeaking();
    clearIdle();
    listenerRef.current?.pause();
    if (previous !== "off") setPhase("speaking");
    const generation = speechGeneration.current;
    const isCurrent = () => generation === speechGeneration.current && voice === voiceGeneration.current;
    await speakThroughProvider(message, isCurrent);
    if (voice === voiceGeneration.current && phaseRef.current !== "off") {
      if (previous === "sleeping") sleep();
      else attend();
    }
  }, [attend, clearIdle, clearVoiceContext, setPhase, sleep, speakThroughProvider, stopSpeaking]);

  const runTurn = useCallback(
    async (audioBase64: string) => {
      const generation = voiceGeneration.current;
      const isCurrent = () => generation === voiceGeneration.current && phaseRef.current !== "off";
      setPhase("thinking");
      clearIdle();
      listenerRef.current?.pause(); // Sam doesn't hear while it thinks or speaks
      setError(null);
      try {
        if (contextEnd.current) await contextEnd.current;
        if (!isCurrent()) return;
        const result = await confirmable((confirmationId) =>
          bridge.voiceUtterance(audioBase64, confirmationId, prefs.responseLanguage, true, voiceContextId.current),
        );
        if (!isCurrent()) return;
        voiceContextId.current = result.status === "ok" ? result.conversation_id ?? undefined : undefined;
        if (result.status !== "ok") clearVoiceContext();
        void refreshIdentity();
        if (result.reason_code === "conversation_ended") {
          sleep();
          return;
        }
        if (result.status === "ok" && result.transcript) {
          lastVoiceLanguage.current = result.language ?? detectLanguage(result.transcript) ?? "en";
          const heard = append({
            role: result.speaker === "guest" ? "guest" : "user",
            text: result.transcript,
            source: "voice",
            language: result.language ?? detectLanguage(result.transcript),
          });
          const reply = result.reply
            ? append({
                role: "sam",
                text: result.reply,
                status: "ok",
                language: result.language,
                direction: result.direction,
              })
            : null;
          setExchange({ heard, reply });
          if (reply) {
            setPhase("speaking");
            await speakAutomatically(reply, isCurrent);
          }
          if (isCurrent()) attend();
          return;
        }
        if (result.reason_code === "voice_not_heard") {
          const spokenLanguage = prefs.responseLanguage === "auto"
            ? lastVoiceLanguage.current ?? prefs.uiLanguage
            : prefs.responseLanguage;
          const language = uiLanguageFor(spokenLanguage, prefs.uiLanguage);
          const notice = append({
            role: "notice",
            text: translate(language, "voice.moreSpeech"),
            status: "failed",
            language,
            direction: directionFor(language),
          });
          setExchange({ heard: null, reply: notice });
          // Keep capture paused while the retry cue plays. The notice remains
          // visible afterward; an unavailable local voice cannot block retry.
          setPhase("speaking");
          try {
            await speakLocally(translate(language, "voice.repeatQuestion"), language);
          } catch {
            // The visible notice is sufficient if local speech cannot start.
          }
          if (isCurrent()) attend();
          return;
        }
        const notice = append({
          role: "notice",
          text: resultText(result),
          status: "failed",
          referenceId: result.reference_id,
        });
        setExchange({ heard: null, reply: notice });
        // An identity problem ends the conversation (fail closed).
        if (IDENTITY_STOPS.has(result.reason_code ?? "")) sleep();
        else attend();
      } catch (failure) {
        if (!isCurrent()) return;
        setError(failureText(failure));
        sleep();
      }
    },
    [append, attend, bridge, clearIdle, clearVoiceContext, confirmable, prefs.responseLanguage, prefs.uiLanguage, refreshIdentity, setError, setPhase, sleep, speakAutomatically, speakLocally],
  );

  useEffect(() => {
    runTurnRef.current = runTurn;
  }, [runTurn]);

  const onSegment = useCallback(
    async (audioBase64: string, seconds: number, endedBy: SegmentEnd, voicedSeconds: number) => {
      const current = phaseRef.current;
      const listener = listenerRef.current;
      if (!listener) return;
      const segment = { audio: audioBase64, samples: Math.round(seconds * TARGET_SAMPLE_RATE), voicedSeconds };
      if (current === "attending") {
        bufferedTurn.current.push(segment);
        const samples = bufferedSamples(bufferedTurn.current);
        if (endedBy === "limit" || samples >= MAX_RECORDING_SAMPLES) rejectLongTurn();
        else if (endedBy === "silence") flushBufferedTurn();
        else captureRemaining(listener, samples);
        return;
      }
      if (current !== "sleeping") return;
      const pending = wakeBusy.current;
      if (pending) {
        pending.segments.push(segment);
        pending.samples += segment.samples;
        captureRemaining(listener, pending.samples);
        return;
      }
      if (Date.now() < cooldownUntil.current || seconds > WAKE_MAX_SECONDS) return;
      const check: PendingWakeCheck = {
        generation: voiceGeneration.current,
        segments: [],
        samples: segment.samples,
      };
      wakeBusy.current = check;
      captureRemaining(listener, check.samples);
      const isCurrent = () =>
        check.generation === voiceGeneration.current && wakeBusy.current === check && phaseRef.current === "sleeping";
      try {
        const result = await bridge.voiceWake(audioBase64);
        if (!isCurrent()) return;
        wakeBusy.current = null;
        if (result.status === "ok" && result.wake) {
          if (check.samples >= MAX_RECORDING_SAMPLES) { rejectLongTurn(); return; }
          bufferedTurn.current = result.followed ? [segment, ...check.segments] : check.segments;
          flashAwake();
          attend(true);
        } else {
          listener.setTiming(SLEEP_TIMING);
          listener.resume();
          cooldownUntil.current =
            Date.now() + (result.reason_code === "rate_limited" ? WAKE_BACKOFF_MS : WAKE_COOLDOWN_MS);
        }
      } catch {
        if (!isCurrent()) return;
        listener.setTiming(SLEEP_TIMING);
        listener.resume();
        cooldownUntil.current = Date.now() + WAKE_BACKOFF_MS;
      } finally {
        if (wakeBusy.current === check) wakeBusy.current = null;
      }
    },
    [attend, bridge, flashAwake, flushBufferedTurn, rejectLongTurn],
  );
  const onSegmentRef = useRef(onSegment);
  useEffect(() => {
    onSegmentRef.current = onSegment;
  });

  useEffect(() => {
    if (!handsFreeOn) return;
    let cancelled = false;
    invalidateVoiceRequests();
    cooldownUntil.current = 0;
    const listener = new Listener(
      audioEnvironment,
      (audio, seconds, endedBy, voicedSeconds) => {
        if (listenerRef.current === listener) void onSegmentRef.current(audio, seconds, endedBy, voicedSeconds);
      },
      (value) => {
        if (listenerRef.current === listener && phaseRef.current === "attending") publishLevel(value);
      },
      onSpeech,
      () => {
        if (cancelled || listenerRef.current !== listener) return;
        invalidateVoiceRequests();
        setPhase("off");
        setMicFailed(true);
        setError(t("home.micFailed"));
      },
    );
    listenerRef.current = listener;
    listener
      .start(SLEEP_TIMING)
      .then(() => {
        if (cancelled) listener.stop();
        else {
          phaseRef.current = "sleeping";
          setPhaseState("sleeping");
        }
      })
      .catch((failure: unknown) => {
        if (cancelled) return;
        listenerRef.current = null;
        const code = failure instanceof RecorderError ? failure.code : "failed";
        if (code === "denied") setMicDenied(true);
        else { setMicFailed(true); setError(micError(code)); }
      });
    return () => {
      cancelled = true;
      invalidateVoiceRequests();
      phaseRef.current = "off";
      listener.stop();
      listenerRef.current = null;
      window.clearTimeout(idleTimer.current);
      window.clearTimeout(wakeTimer.current);
      speechHandle.current?.stop();
      speechHandle.current = null;
      phaseRef.current = "off";
      setPhaseState("off");
      setHeardSpeech(false);
      setJustWoke(false);
      publishLevel(0);
    };
  }, [handsFreeOn, audioEnvironment, invalidateVoiceRequests, micError, onSpeech, publishLevel, setError, setPhase, t]);

  useEffect(() => {
    if (!homeActive || !visible) {
      clearVoiceContext();
      cancelListening();
      stopSpeaking();
    }
  }, [homeActive, visible, clearVoiceContext, cancelListening, stopSpeaking]);

  const wakeNow = useCallback(() => {
    if (micDenied || micFailed) {
      setMicDenied(false); // try the microphone again
      setMicFailed(false);
      return;
    }
    if (phaseRef.current === "sleeping") {
      const pending = wakeBusy.current?.segments.slice() ?? [];
      invalidateVoiceRequests();
      bufferedTurn.current = pending;
      flashAwake();
      attend(true);
    }
  }, [attend, flashAwake, invalidateVoiceRequests, micDenied, micFailed]);

  const sleepNow = useCallback(() => {
    cancelListening();
    stopSpeaking();
    speechHandle.current?.stop();
    if (phaseRef.current !== "off") sleep();
    else invalidateVoiceRequests();
  }, [cancelListening, invalidateVoiceRequests, sleep, stopSpeaking]);

  const voice: VoiceState = confirmationPending
    ? "permission"
    : recording
      ? "listening"
      : speakingId || phase === "speaking"
        ? "speaking"
        : busy || phase === "thinking"
          ? "thinking"
          : justWoke && (phase === "attending" || phase === "waking")
            ? "waking"
            : phase === "attending"
              ? heardSpeech
                ? "listening"
                : "attending"
              : error
                ? "error"
                : connection === "unavailable"
                  ? "offline"
                  : phase === "sleeping"
                    ? "sleeping"
                    : "idle";

  const value = useMemo<SessionValue>(
    () => ({
      conversations,
      active,
      selectConversation: (id: string) => { sleepNow(); setActiveId(id); setExchange(null); },
      newConversation: () => {
        if (active.messages.length === 0) return;
        sleepNow();
        const created = newConversation();
        setConversations((current) => [created, ...current]);
        setActiveId(created.id);
        setExchange(null);
      },
      clearActive: () => {
        sleepNow();
        setConversations((current) =>
          current.map((c) => (c.id === activeId ? { ...c, messages: [], title: "New conversation" } : c)),
        );
        setExchange(null);
      },
      exchange,
      dismissExchange: () => setExchange(null),
      voice,
      level: recording || phase === "attending" ? level : 0,
      recordingSeconds,
      error,
      micAvailable,
      chatAvailable,
      startListening,
      stopListening,
      cancelListening,
      sendText,
      textSending,
      textDraft,
      setTextDraft,
      speakingId,
      speak,
      stopSpeaking,
      profileFor,
      handsFree: { active: phase !== "off", phase, activation, needsSetup, setupState, micDenied, micFailed },
      setHomeActive,
      wakeNow,
      sleepNow,
    }),
    [
      textSending,
      textDraft,
      phase,
      activation,
      needsSetup,
      setupState,
      micDenied,
      micFailed,
      wakeNow,
      sleepNow,
      conversations,
      active,
      activeId,
      exchange,
      voice,
      recording,
      level,
      recordingSeconds,
      error,
      micAvailable,
      chatAvailable,
      startListening,
      stopListening,
      cancelListening,
      sendText,
      speakingId,
      speak,
      stopSpeaking,
      profileFor,
    ],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
