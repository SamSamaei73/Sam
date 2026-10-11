import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { browserLocalSpeech, localSpeechOutput } from "../lib/localSpeech";

const native = vi.hoisted(() => ({
  isTauri: vi.fn(() => false),
  invoke: vi.fn<(...args: unknown[]) => Promise<unknown>>(),
}));
vi.mock("@tauri-apps/api/core", () => native);

class Utterance {
  voice: SpeechSynthesisVoice | null = null;
  lang = "";
  onend: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(readonly text: string) {}
}

function browser() {
  let voices: SpeechSynthesisVoice[] = [];
  const synth = Object.assign(new EventTarget(), {
    getVoices: () => voices,
    speak: vi.fn<(utterance: Utterance) => void>(),
    cancel: vi.fn(),
  });
  vi.stubGlobal("speechSynthesis", synth);
  vi.stubGlobal("SpeechSynthesisUtterance", Utterance);
  return {
    synth,
    voices(localService = true, language = "en-US") {
      voices = [{ name: "System", lang: language, localService } as SpeechSynthesisVoice];
      synth.dispatchEvent(new Event("voiceschanged"));
    },
    voiceList(languages: string[]) {
      voices = languages.map((lang) => ({ name: lang, lang, localService: true } as SpeechSynthesisVoice));
      synth.dispatchEvent(new Event("voiceschanged"));
    },
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  native.isTauri.mockReturnValue(false);
  native.invoke.mockReset();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("on-device reply speech", () => {
  it.each(["es-MX", "fr-CA", "ar-SA", "ja-JP", "zh-TW"])("selects an exact installed local %s voice and preserves the reply", (language) => {
    const fake = browser();
    fake.voiceList([language.split("-")[0]!, "en-US", language]);
    const end = vi.fn();
    browserLocalSpeech()?.speak("A reply supplied in its detected language", language, end);
    const utterance = fake.synth.speak.mock.calls[0]?.[0];
    expect(utterance?.lang).toBe(language);
    expect(utterance?.text).toBe("A reply supplied in its detected language");
    utterance?.onend?.();
    expect(end).toHaveBeenCalledExactlyOnceWith("ended");
  });

  it("falls back only to the same base language, without translating or changing the reply", () => {
    const fake = browser();
    fake.voiceList(["en-US", "es-ES"]);
    browserLocalSpeech()?.speak("Una respuesta en español.", "es-MX", vi.fn());
    expect(fake.synth.speak.mock.calls[0]?.[0]?.lang).toBe("es-ES");
    expect(fake.synth.speak.mock.calls[0]?.[0]?.text).toBe("Una respuesta en español.");
  });

  it("waits for WebKit's initially empty voice list and speaks exactly once", () => {
    const fake = browser();
    const end = vi.fn();
    browserLocalSpeech()?.speak("Hello Sam", "en", end);
    expect(fake.synth.speak).not.toHaveBeenCalled();
    fake.voices();
    fake.voices();
    expect(fake.synth.speak).toHaveBeenCalledTimes(1);
    const utterance = fake.synth.speak.mock.calls[0]?.[0];
    expect(utterance?.text).toBe("Hello Sam");
    utterance?.onend?.();
    utterance?.onerror?.();
    expect(end).toHaveBeenCalledExactlyOnceWith("ended");
  });

  it("never uses a remote browser voice and reports missing language", () => {
    const fake = browser();
    fake.voices(false);
    const end = vi.fn();
    browserLocalSpeech()?.speak("A private reply", "en", end);
    vi.advanceTimersByTime(1_500);
    expect(end).toHaveBeenCalledExactlyOnceWith("unavailable");
    expect(fake.synth.speak).not.toHaveBeenCalled();
  });

  it("cancels while voice discovery is pending without later speaking", () => {
    const fake = browser();
    const end = vi.fn();
    const handle = browserLocalSpeech()?.speak("Stop this", "en", end);
    handle?.stop();
    fake.voices();
    vi.advanceTimersByTime(2_000);
    expect(end).toHaveBeenCalledExactlyOnceWith("stopped");
    expect(fake.synth.speak).not.toHaveBeenCalled();
  });

  it("uses fixed native commands in Tauri even without Web Speech support", async () => {
    native.isTauri.mockReturnValue(true);
    native.invoke.mockResolvedValue(undefined);
    const end = vi.fn();
    localSpeechOutput()?.speak("Hello from Sam", "en", end);
    await vi.waitFor(() => expect(end).toHaveBeenCalledExactlyOnceWith("ended"));
    expect(native.invoke).toHaveBeenCalledExactlyOnceWith("sam_local_speak", {
      speechId: expect.any(String), text: "Hello from Sam", language: "en",
    });
  });

  it.each(["es", "fr", "ar", "he", "ja", "zh", "haw", "yue", "jw", "tl", "no"])("passes backend language %s to the fixed native speech bridge", async (language) => {
    native.isTauri.mockReturnValue(true);
    native.invoke.mockResolvedValue(undefined);
    const end = vi.fn();
    localSpeechOutput()?.speak("Localized model reply", language, end);
    await vi.waitFor(() => expect(end).toHaveBeenCalledExactlyOnceWith("ended"));
    expect(native.invoke).toHaveBeenCalledExactlyOnceWith("sam_local_speak", {
      speechId: expect.any(String), text: "Localized model reply", language,
    });
  });

  it("waits for native stop acknowledgement before completing and ignores late speech completion", async () => {
    native.isTauri.mockReturnValue(true);
    let complete: (() => void) | undefined;
    let stopped: (() => void) | undefined;
    native.invoke.mockImplementation((command) => command === "sam_local_speak"
      ? new Promise<void>((resolve) => { complete = resolve; })
      : new Promise<void>((resolve) => { stopped = resolve; }));
    const end = vi.fn();
    const handle = localSpeechOutput()?.speak("A reply", "en", end);
    const speechId = (native.invoke.mock.calls[0]?.[1] as { speechId: string }).speechId;
    handle?.stop();
    handle?.stop();
    await Promise.resolve();
    expect(end).not.toHaveBeenCalled();
    expect(native.invoke).toHaveBeenCalledTimes(2);
    expect(native.invoke).toHaveBeenLastCalledWith("sam_local_stop", { speechId });
    stopped?.();
    await Promise.resolve();
    expect(end).toHaveBeenCalledExactlyOnceWith("stopped");
    complete?.();
    await Promise.resolve();
    expect(end).toHaveBeenCalledExactlyOnceWith("stopped");
  });

  it.each(["pending", "rejected"] as const)("actual native speech completion safely ends cancellation when stop IPC is %s", async (status) => {
    native.isTauri.mockReturnValue(true);
    let complete: (() => void) | undefined;
    native.invoke.mockImplementation((command) => command === "sam_local_speak"
      ? new Promise<void>((resolve) => { complete = resolve; })
      : status === "rejected" ? Promise.reject("bridge unavailable") : new Promise<void>(() => undefined));
    const end = vi.fn();
    localSpeechOutput()?.speak("A reply", "en", end)?.stop();
    await Promise.resolve();
    expect(end).not.toHaveBeenCalled();
    complete?.();
    await Promise.resolve();
    // Cancellation must not become a provider fallback after the old output ends.
    expect(end).toHaveBeenCalledExactlyOnceWith("stopped");
  });

  it("reports unavailable native languages for the confirmed provider path", async () => {
    native.isTauri.mockReturnValue(true);
    native.invoke.mockRejectedValue("speech_unavailable");
    const end = vi.fn();
    localSpeechOutput()?.speak("سلام", "fa", end);
    await vi.waitFor(() => expect(end).toHaveBeenCalledExactlyOnceWith("unavailable"));
    expect(native.invoke).toHaveBeenCalledTimes(1);
  });
});
