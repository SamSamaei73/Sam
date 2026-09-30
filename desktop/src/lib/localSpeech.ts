/**
 * Sam's hands-free replies are spoken by the operating system's ON-DEVICE
 * voices (the Web Speech API), never by a cloud voice: only voices that
 * declare `localService` are used, so no reply text leaves the Mac to be
 * spoken. If no local voice speaks the language, Sam simply does not speak.
 */

export interface SpeechHandle {
  stop: () => void;
}

export interface LocalSpeechOutput {
  /** Speak `text`; `onEnd` runs once when it finishes or is stopped. Null: no local voice. */
  speak: (text: string, language: "fa" | "en", onEnd: () => void) => SpeechHandle | null;
}

export function browserLocalSpeech(): LocalSpeechOutput | null {
  const synth = typeof window !== "undefined" ? window.speechSynthesis : undefined;
  const Utterance = typeof window !== "undefined" ? window.SpeechSynthesisUtterance : undefined;
  if (!synth || !Utterance) return null;
  return {
    speak(text, language, onEnd) {
      const voice = synth
        .getVoices()
        .find((v) => v.localService && v.lang.toLowerCase().startsWith(language));
      if (!voice) return null;
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        onEnd();
      };
      const utterance = new Utterance(text);
      utterance.voice = voice;
      utterance.lang = voice.lang;
      utterance.onend = finish;
      utterance.onerror = finish;
      synth.cancel();
      synth.speak(utterance);
      return {
        stop() {
          synth.cancel();
          finish();
        },
      };
    },
  };
}
