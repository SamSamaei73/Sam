import { isNativeSpeechRuntime, speakNative, stopNativeSpeech } from "../bridge/tauri";
import type { ResponseLanguage } from "../bridge/types";
import { canonicalLanguageTag, languageBase } from "./language";

/** Automatic replies use an installed, on-device voice. Provider speech, when
 * needed, goes through the separate backend permission flow in the session. */
export type SpeechOutcome = "ended" | "stopped" | "unavailable" | "failed";

export interface SpeechHandle {
  stop: () => void;
}

export interface LocalSpeechOutput {
  /** Completion runs once, including cancellation and unavailable voices. */
  speak: (text: string, language: ResponseLanguage, onEnd: (outcome?: SpeechOutcome) => void) => SpeechHandle | null;
}

const VOICES_READY_MS = 1_500;
const SPEECH_TIMEOUT_MS = 600_000;

/** Browser preview fallback. Voice discovery is asynchronous in WebKit. */
export function browserLocalSpeech(): LocalSpeechOutput | null {
  const synth = typeof window !== "undefined" ? window.speechSynthesis : undefined;
  const Utterance = typeof window !== "undefined" ? window.SpeechSynthesisUtterance : undefined;
  if (!synth || !Utterance) return null;
  return {
    speak(text, language, onEnd) {
      const requested = canonicalLanguageTag(language);
      if (!requested) { onEnd("unavailable"); return null; }
      let done = false;
      let started = false;
      let timer: number | undefined;
      const finish = (outcome: SpeechOutcome) => {
        if (done) return;
        done = true;
        window.clearTimeout(timer);
        synth.removeEventListener("voiceschanged", trySpeak);
        onEnd(outcome);
      };
      const trySpeak = () => {
        if (done || started) return;
        const voices = synth.getVoices().filter((voice) => voice.localService);
        const voice = voices.find((voice) => canonicalLanguageTag(voice.lang) === requested)
          ?? voices.find((voice) => languageBase(voice.lang) === languageBase(requested));
        if (!voice) return;
        started = true;
        window.clearTimeout(timer);
        synth.removeEventListener("voiceschanged", trySpeak);
        const utterance = new Utterance(text);
        utterance.voice = voice;
        utterance.lang = voice.lang;
        utterance.onend = () => finish("ended");
        utterance.onerror = () => finish("failed");
        try {
          synth.cancel();
          synth.speak(utterance);
          if (!done) timer = window.setTimeout(() => {
            finish("failed");
            synth.cancel();
          }, SPEECH_TIMEOUT_MS);
        } catch {
          finish("failed");
        }
      };
      synth.addEventListener("voiceschanged", trySpeak);
      timer = window.setTimeout(() => finish("unavailable"), VOICES_READY_MS);
      trySpeak();
      return {
        stop() {
          // Finish first: cancellation can synchronously dispatch an error.
          finish("stopped");
          if (started) synth.cancel();
        },
      };
    },
  };
}

/** The desktop uses macOS speech directly: WKWebView may expose no voices. */
export function localSpeechOutput(): LocalSpeechOutput | null {
  if (!isNativeSpeechRuntime()) return browserLocalSpeech();
  return {
    speak(text, language, onEnd) {
      if (!canonicalLanguageTag(language)) { onEnd("unavailable"); return null; }
      const speechId = crypto.randomUUID();
      let done = false;
      let stopping = false;
      const finish = (outcome: SpeechOutcome) => {
        if (done) return;
        done = true;
        onEnd(outcome);
      };
      // The backend catalog uses primary codes such as jw. Browser locale
      // aliases (jv) must not replace those codes on the native command.
      void speakNative(speechId, text, language).then(
        () => finish(stopping ? "stopped" : "ended"),
        (error: unknown) => finish(stopping ? "stopped" : error === "speech_unavailable" ? "unavailable" : "failed"),
      );
      return {
        stop() {
          if (done || stopping) return;
          stopping = true;
          // Completion reopens the microphone. Wait until the OS has really
          // stopped this utterance, or the original bounded speak call ends.
          // An IPC failure alone is neither proof of silence nor permission
          // to resume capture or request replacement cloud speech.
          void stopNativeSpeech(speechId).then(
            () => finish("stopped"),
            () => undefined,
          );
        },
      };
    },
  };
}
