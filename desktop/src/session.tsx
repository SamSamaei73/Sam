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
import type { OperationResult, ResponseLanguage } from "./bridge/types";
import type { ChatMessage } from "./components/MessageBubble";
import { playSynthesizedAudio, type Playback } from "./lib/audioPlayback";
import { detectLanguage } from "./lib/language";
import { Listener } from "./lib/listener";
import { browserLocalSpeech, type LocalSpeechOutput, type SpeechHandle } from "./lib/localSpeech";
import { Recorder, RecorderError } from "./lib/recorder";
import { SLEEP_TIMING, TURN_TIMING } from "./lib/vad";
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
  /** Voice activation exists but Sam doesn't know the owner's voice yet. */
  needsSetup: boolean;
  /** macOS microphone access was refused (not a Sam permission). */
  micDenied: boolean;
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
  speech = browserLocalSpeech(),
}: {
  children: ReactNode;
  /** Local (on-device) voice for hands-free replies; null: Sam doesn't speak. */
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
  const [speakingId, setSpeakingId] = useState<string | null>(null);
  const [error, setErrorState] = useState<string | null>(null);
  const recorder = useRef<Recorder | null>(null);
  const phaseRef = useRef<HandsFreePhase>("off");
  const speechHandle = useRef<SpeechHandle | null>(null);
  const playback = useRef<Playback | null>(null);
  const finishRef = useRef<() => void>(() => undefined);
  const errorTimer = useRef<number | undefined>(undefined);

  const setError = useCallback((message: string | null) => {
    window.clearTimeout(errorTimer.current);
    setErrorState(message);
    if (message) errorTimer.current = window.setTimeout(() => setErrorState(null), ERROR_VISIBLE_MS);
  }, []);

  useEffect(
    () => () => {
      recorder.current?.cancel();
      playback.current?.stop();
      window.clearTimeout(errorTimer.current);
    },
    [],
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
      t(`mic.${code === "denied" || code === "unsupported" || code === "empty" ? code : "failed"}` as "mic.failed"),
    [t],
  );

  const sendVoice = useCallback(
    async (audioBase64: string) => {
      setBusy(true);
      try {
        const result = await confirmable((confirmationId) =>
          bridge.voiceUtterance(audioBase64, confirmationId, prefs.responseLanguage),
        );
        void refreshIdentity();
        if (result.reason_code === "owner_verification_required") {
          // A non-owner speaker is stopped before any transcription: a fixed
          // notice, never a transcript.
          const notice = append({ role: "notice", text: t("voice.ownerRequired"), status: "failed" });
          setExchange({ heard: null, reply: notice });
        } else if (result.status === "ok" && result.transcript) {
          const heard = append({
            role: result.speaker === "guest" ? "guest" : "user",
            text: result.transcript,
            source: "voice",
            language: detectLanguage(result.transcript),
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
    [append, bridge, confirmable, prefs.responseLanguage, refreshIdentity, setError, t],
  );

  const stopListening = useCallback(async () => {
    const current = recorder.current;
    if (!current) return;
    recorder.current = null;
    setRecording(false);
    setRecordingSeconds(0);
    setLevel(0);
    try {
      const recorded = await current.stop();
      await sendVoice(recorded.base64);
    } catch (failure) {
      setError(micError(failure instanceof RecorderError ? failure.code : "failed"));
    }
  }, [micError, sendVoice, setError]);

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
      (value) => setLevel(value),
    );
    recorder.current = next;
    try {
      await next.start();
      setRecording(true);
      setExchange(null);
    } catch (failure) {
      recorder.current = null;
      setError(micError(failure instanceof RecorderError ? failure.code : "failed"));
    }
  }, [audioEnvironment, busy, micError, setError]);

  const cancelListening = useCallback(() => {
    recorder.current?.cancel();
    recorder.current = null;
    setRecording(false);
    setRecordingSeconds(0);
    setLevel(0);
  }, []);

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
      const capable = (profiles ?? []).filter((p) => p.languages.includes(language));
      const preferred = capable.find((p) => p.profile_id === prefs.readAloudVoice);
      return (preferred ?? capable[0])?.profile_id ?? null;
    },
    [prefs.readAloudVoice, profiles, status?.speech_output],
  );

  const stopSpeaking = useCallback(() => {
    playback.current?.stop();
    playback.current = null;
    setSpeakingId(null);
    speechHandle.current?.stop(); // a hands-free reply: back to listening
  }, []);

  const speak = useCallback(
    async (message: ChatMessage) => {
      const language = message.language ?? detectLanguage(message.text) ?? "en";
      const profile = profileFor(language);
      if (!profile) return;
      stopSpeaking();
      setBusy(true);
      try {
        // Speech sends text to a provider: every synthesis asks the owner.
        const result = await confirmable((confirmationId) =>
          bridge.speak({ text: message.text, voiceProfile: profile, language, confirmationId }),
        );
        if (result.status !== "ok" || !result.audio_base64) {
          setError(result.message ?? "Sam couldn't read that aloud.");
          return;
        }
        setSpeakingId(message.id);
        playback.current = playSynthesizedAudio(result.audio_base64, result.audio_format, () => {
          playback.current = null;
          setSpeakingId(null);
        });
      } catch {
        setError("Sam couldn't read that aloud.");
      } finally {
        setBusy(false);
      }
    },
    [bridge, confirmable, profileFor, setError, stopSpeaking],
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
  const listenerRef = useRef<Listener | null>(null);
  const idleTimer = useRef<number | undefined>(undefined);
  const wakeTimer = useRef<number | undefined>(undefined);
  const cooldownUntil = useRef(0);
  const wakeBusy = useRef(false);
  const setPhase = useCallback((next: HandsFreePhase) => {
    phaseRef.current = next;
    setPhaseState(next);
  }, []);

  const activation = status?.voice_activation ?? "unavailable";
  const enrolled = identity?.available === true && identity.enrolled === true;
  const needsSetup = activation !== "unavailable" && identity?.available === true && identity.enrolled !== true;
  const handsFreeOn =
    activation === "on" &&
    enrolled &&
    micAvailable &&
    (connection === "connected" || connection === "degraded") &&
    homeActive &&
    visible &&
    !micDenied;

  const clearIdle = useCallback(() => window.clearTimeout(idleTimer.current), []);
  /** Read through a function so TypeScript doesn't narrow a mutable ref. */
  const handsFreeStopped = () => phaseRef.current === "off";

  const sleep = useCallback(() => {
    clearIdle();
    const listener = listenerRef.current;
    if (!listener) return;
    setPhase("sleeping");
    setJustWoke(false);
    listener.setTiming(SLEEP_TIMING);
    listener.resume();
    cooldownUntil.current = Date.now() + WAKE_COOLDOWN_MS;
  }, [clearIdle, setPhase]);

  const attend = useCallback(() => {
    const listener = listenerRef.current;
    if (!listener) return;
    setPhase("attending");
    listener.setTiming(TURN_TIMING);
    listener.resume();
    clearIdle();
    idleTimer.current = window.setTimeout(() => sleep(), CONVERSATION_IDLE_MS);
  }, [clearIdle, setPhase, sleep]);

  const flashAwake = useCallback(() => {
    setJustWoke(true);
    window.clearTimeout(wakeTimer.current);
    wakeTimer.current = window.setTimeout(() => setJustWoke(false), WAKE_FLASH_MS);
  }, []);

  /** Speak a hands-free reply with a LOCAL voice; resolves when it ends. */
  const speakLocally = useCallback((text: string, language: "fa" | "en") => {
    return new Promise<void>((resolve) => {
      const handle = speechRef.current?.speak(text, language, () => {
        speechHandle.current = null;
        resolve();
      });
      if (!handle) resolve();
      else speechHandle.current = handle;
    });
  }, []);

  const runTurn = useCallback(
    async (audioBase64: string) => {
      clearIdle();
      listenerRef.current?.pause(); // Sam doesn't hear while it thinks or speaks
      setPhase("thinking");
      setError(null);
      try {
        const result = await confirmable((confirmationId) =>
          bridge.voiceUtterance(audioBase64, confirmationId, prefs.responseLanguage, true),
        );
        if (handsFreeStopped()) return;
        void refreshIdentity();
        if (result.reason_code === "conversation_ended") {
          sleep();
          return;
        }
        if (result.status === "ok" && result.transcript) {
          const heard = append({
            role: result.speaker === "guest" ? "guest" : "user",
            text: result.transcript,
            source: "voice",
            language: detectLanguage(result.transcript),
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
            await speakLocally(reply.text, reply.language ?? detectLanguage(reply.text) ?? "en");
          }
          if (!handsFreeStopped()) attend();
          return;
        }
        if (result.reason_code === "voice_not_heard") {
          attend(); // nothing usable was said: keep listening
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
        setError(failureText(failure));
        if (!handsFreeStopped()) sleep();
      }
    },
    [append, attend, bridge, clearIdle, confirmable, prefs.responseLanguage, refreshIdentity, setError, setPhase, sleep, speakLocally],
  );

  const onSegment = useCallback(
    async (audioBase64: string, seconds: number) => {
      const current = phaseRef.current;
      if (current === "attending") {
        void runTurn(audioBase64);
        return;
      }
      if (current !== "sleeping") return;
      if (wakeBusy.current || Date.now() < cooldownUntil.current || seconds > WAKE_MAX_SECONDS) return;
      wakeBusy.current = true;
      try {
        const result = await bridge.voiceWake(audioBase64);
        if (phaseRef.current !== "sleeping") return;
        if (result.status === "ok" && result.wake) {
          flashAwake();
          if (result.followed) void runTurn(audioBase64); // "Sam, what's...?"
          else attend();
        } else {
          cooldownUntil.current =
            Date.now() + (result.reason_code === "rate_limited" ? WAKE_BACKOFF_MS : WAKE_COOLDOWN_MS);
        }
      } catch {
        cooldownUntil.current = Date.now() + WAKE_BACKOFF_MS;
      } finally {
        wakeBusy.current = false;
      }
    },
    [attend, bridge, flashAwake, runTurn],
  );
  const onSegmentRef = useRef(onSegment);
  useEffect(() => {
    onSegmentRef.current = onSegment;
  });

  useEffect(() => {
    if (!handsFreeOn) return;
    let cancelled = false;
    const listener = new Listener(
      audioEnvironment,
      (audio, seconds) => void onSegmentRef.current(audio, seconds),
      (value) => setLevel(value),
      (active) => setHeardSpeech(active),
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
        else setError(micError(code));
      });
    return () => {
      cancelled = true;
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
      setLevel(0);
    };
  }, [handsFreeOn, audioEnvironment, micError, setError]);

  const wakeNow = useCallback(() => {
    if (micDenied) {
      setMicDenied(false); // try the microphone again
      return;
    }
    if (phaseRef.current === "sleeping") {
      flashAwake();
      attend();
    }
  }, [attend, flashAwake, micDenied]);

  const sleepNow = useCallback(() => {
    speechHandle.current?.stop();
    if (phaseRef.current !== "off") sleep();
  }, [sleep]);

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
      selectConversation: (id: string) => setActiveId(id),
      newConversation: () => {
        if (active.messages.length === 0) return;
        const created = newConversation();
        setConversations((current) => [created, ...current]);
        setActiveId(created.id);
        setExchange(null);
      },
      clearActive: () => {
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
      handsFree: { active: phase !== "off", phase, activation, needsSetup, micDenied },
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
      micDenied,
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
