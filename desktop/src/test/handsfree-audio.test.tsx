import { act, render, screen, waitFor } from "@testing-library/react";
import { Profiler } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { VoiceResponse, VoiceWakeResponse } from "../bridge/types";
import type { LocalSpeechOutput } from "../lib/localSpeech";
import { base64ToBytes } from "../lib/wav";
import { baseIdentity, baseStatus, fakeAudioEnvironment, mockBridge, ok } from "./helpers";

const FRAME = 2048; // fixture batches; neural capture consumes four 512-sample frames
const state = () => (document.querySelector(".presence") as HTMLElement | null)?.dataset.state;
const response: VoiceResponse = {
  ...ok, transcript: "a captured request", reply: null, forwarded_to_agent: true,
  speaker: "owner", speaker_result: "owner_verified", language: "en", direction: "ltr",
};

function pcm(audio: string): Int16Array {
  const bytes = base64ToBytes(audio);
  const view = new DataView(bytes.buffer, 44);
  return Int16Array.from({ length: view.byteLength / 2 }, (_, i) => view.getInt16(i * 2, true));
}

function sampleCount(audio: string, level: number): number {
  const expected = Math.trunc(Math.fround(level) * (level < 0 ? 32768 : 32767));
  return pcm(audio).filter((sample) => sample === expected).length;
}

function setup(rate = 16_000, utterance?: () => Promise<VoiceResponse>, speech: LocalSpeechOutput | null = null) {
  const fake = fakeAudioEnvironment();
  Object.defineProperty(fake.context, "sampleRate", { value: rate });
  const wakeResults: ((result: VoiceWakeResponse) => void)[] = [];
  const bridge = mockBridge({
    status: vi.fn(async () => ({
      ...baseStatus, voice_input: "configured" as const,
      voice_identity: "configured" as const, voice_activation: "on" as const,
    })),
    identityStatus: vi.fn(async () => ({
      ...baseIdentity, available: true, enrolled: true, setup_state: "enrolled" as const,
      step_up_configured: true, speaker_model: "configured" as const,
    })),
    voiceWake: vi.fn(() => new Promise<VoiceWakeResponse>((resolve) => wakeResults.push(resolve))),
    voiceUtterance: vi.fn(utterance ?? (async () => response)),
  });
  const commits = vi.fn();
  render(<Profiler id="voice" onRender={commits}><App bridge={bridge} audioEnvironment={fake.env} speech={speech} /></Profiler>);
  const frames = (count: number, level: number) => act(() => {
    for (let i = 0; i < count; i++) fake.emit(new Float32Array(FRAME).fill(level));
  });
  const settleWake = async (index = 0, followed = false) => {
    await act(async () => wakeResults[index]?.({ ...ok, wake: true, followed }));
  };
  const startWake = async () => {
    await waitFor(() => expect(state()).toBe("sleeping"));
    frames(Math.ceil(rate * 0.4 / FRAME), 0.15);
    frames(Math.ceil(rate * 0.5 / FRAME), 0);
    expect(bridge.voiceWake).toHaveBeenCalledTimes(1);
  };
  const command = () => bridge.voiceUtterance.mock.calls[0]?.[0] as string;
  return { fake, bridge, frames, settleWake, startWake, command, commits };
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("hands-free audio continuity", () => {
  it.each([
    { language: "es", transcript: "¿Puedes explicar esto?", reply: "Claro, puedo explicarlo.", direction: "ltr" as const },
    { language: "ar", transcript: "هل يمكنك توضيح ذلك؟", reply: "نعم، يمكنني توضيح ذلك.", direction: "rtl" as const },
    { language: "ja", transcript: "これを説明できますか？", reply: "はい、説明できます。", direction: "ltr" as const },
  ])("preserves a $language reply for display and speech, then safely localizes a retry cue", async ({ language, transcript, reply, direction }) => {
    const spoken: { text: string; language: string }[] = [];
    const endings: (() => void)[] = [];
    const speech: LocalSpeechOutput = {
      speak: (text, spokenLanguage, onEnd) => {
        spoken.push({ text, language: spokenLanguage });
        endings.push(() => onEnd());
        return { stop: () => onEnd("stopped") };
      },
    };
    const utterance = vi.fn<() => Promise<VoiceResponse>>()
      .mockResolvedValueOnce({ ...response, language, transcript, reply, direction })
      .mockResolvedValueOnce({ ...response, status: "rejected", reason_code: "voice_not_heard",
        transcript: null, reply: null, forwarded_to_agent: false, speaker: null, speaker_result: "insufficient_audio", language: null });
    const { frames, settleWake, startWake } = setup(16_000, utterance, speech);
    await startWake();
    await settleWake();
    frames(12, 0.3);
    frames(5, 0);
    expect((await screen.findByText(reply)).closest("[lang]")).toHaveAttribute("lang", language);
    expect(screen.getByText(transcript).closest("[lang]")).toHaveAttribute("lang", language);
    expect(spoken[0]).toEqual({ text: reply, language });
    await act(async () => endings.shift()?.());
    frames(2, 0.2);
    frames(5, 0);
    expect(await screen.findByText("I couldn't verify your voice from that recording. Please try again.")).toBeInTheDocument();
    expect(spoken[1]?.language).toBe("en"); // static UI catalogs are independent of model response tags
    await act(async () => endings.shift()?.());
    expect(["attending", "waking"]).toContain(state());
  });

  it.each(["en", "fa"] as const)("explains a short follow-up and captures a fresh turn after the local retry cue (%s)", async (language) => {
    const spoken: { text: string; language: string }[] = [];
    const endings: (() => void)[] = [];
    const speech: LocalSpeechOutput = {
      speak: (text, spokenLanguage, onEnd) => {
        spoken.push({ text, language: spokenLanguage });
        endings.push(() => onEnd());
        return { stop: () => onEnd("stopped") };
      },
    };
    const utterance = vi.fn<() => Promise<VoiceResponse>>()
      .mockResolvedValueOnce({ ...response, reply: language === "fa" ? "آماده‌ام." : "I'm ready.", language })
      .mockResolvedValueOnce({
        ...response, status: "rejected", reason_code: "voice_not_heard", transcript: null,
        reply: null, forwarded_to_agent: false, speaker: null, speaker_result: "insufficient_audio", language: null,
      })
      .mockResolvedValueOnce(response);
    const { bridge, frames, settleWake, startWake } = setup(16_000, utterance, speech);
    await startWake();
    await settleWake();
    frames(12, 0.35);
    frames(5, 0);
    await waitFor(() => expect(state()).toBe("speaking"));
    frames(8, 0.6); // output is heard by the mic, but capture is paused
    expect(utterance.mock.calls.length).toBe(1);
    await act(async () => endings.shift()?.());
    frames(2, 0.18); // a short follow-up, not enough for owner verification
    frames(5, 0);
    const notice = language === "fa"
      ? "نتوانستم صدایت را از این ضبط تأیید کنم. لطفاً دوباره بگو."
      : "I couldn't verify your voice from that recording. Please try again.";
    expect(await screen.findByText(notice)).toBeInTheDocument();
    await waitFor(() => expect(spoken).toHaveLength(2));
    expect(spoken[1]?.language).toBe(language);
    expect(state()).toBe("speaking");
    frames(8, 0.7); // the retry cue must never become another captured turn
    frames(5, 0);
    expect(utterance.mock.calls.length).toBe(2);
    await act(async () => endings.shift()?.());
    expect(screen.getByText(notice)).toBeInTheDocument(); // remains visible while listening
    frames(12, -0.3);
    frames(5, 0);
    await waitFor(() => expect(utterance).toHaveBeenCalledTimes(3));
    const followup = bridge.voiceUtterance.mock.calls[2]?.[0] as string;
    expect(sampleCount(followup, -0.3)).toBe(12 * FRAME);
    expect(sampleCount(followup, 0.7)).toBe(0);
    expect(bridge.voiceWake).toHaveBeenCalledTimes(1); // no repeated wake required
    expect(bridge.speak.mock.calls.length).toBe(0); // the retry cue stays local
  });

  it("keeps the retry notice visible and resumes when no local voice is available", async () => {
    const utterance = vi.fn(async (): Promise<VoiceResponse> => ({
      ...response, status: "rejected", reason_code: "voice_not_heard", transcript: null,
      reply: null, forwarded_to_agent: false, speaker: null, speaker_result: "insufficient_audio", language: null,
    }));
    const { bridge, frames, settleWake, startWake } = setup(16_000, utterance);
    await startWake();
    await settleWake();
    frames(2, 0.18);
    frames(5, 0);
    expect(await screen.findByText("I couldn't verify your voice from that recording. Please try again.")).toBeInTheDocument();
    expect(["attending", "waking"]).toContain(state());
    expect(bridge.speak.mock.calls.length).toBe(0);
    frames(12, 0.35);
    frames(5, 0);
    await waitFor(() => expect(utterance).toHaveBeenCalledTimes(2));
  });

  it("does not render Home for sleeping microphone frames", async () => {
    const { fake, bridge, commits } = setup(48_000);
    await waitFor(() => expect(state()).toBe("sleeping"));
    commits.mockClear();
    for (let i = 0; i < 100; i++) {
      act(() => fake.emit(new Float32Array(FRAME).fill(0.001 + (i % 9) * 0.001)));
    }
    expect(commits.mock.calls.length).toBe(0);
    expect(bridge.voiceWake.mock.calls.length).toBe(0);
  });

  it("bounds active microphone meter rendering while preserving audio capture", async () => {
    const { fake, frames, bridge, settleWake, startWake, commits } = setup(48_000);
    await startWake();
    await settleWake();
    let now = performance.now();
    vi.spyOn(performance, "now").mockImplementation(() => now);
    commits.mockClear();
    for (let i = 0; i < 24; i++) {
      now += 1000 * FRAME / 48_000;
      act(() => fake.emit(new Float32Array(FRAME).fill(0.04 + (i % 10) * 0.03)));
    }
    expect(commits.mock.calls.length).toBeGreaterThan(5);
    expect(commits.mock.calls.length).toBeLessThanOrEqual(14);
    frames(24, 0);
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalledTimes(1));
  });

  it("retains a complete command spoken while the wake check is pending", async () => {
    const { bridge, frames, settleWake, startWake, command } = setup();
    await startWake();
    frames(8, 0.35);
    frames(8, 0);
    expect(bridge.voiceUtterance.mock.calls.length).toBe(0);
    await settleWake();
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalledTimes(1));
    expect(sampleCount(command(), 0.35)).toBe(8 * FRAME);
    expect(sampleCount(command(), 0.15)).toBe(0); // the bare wake word is excluded
  });

  it("submits a finished turn within one frame of 600 ms despite moderate room noise", async () => {
    const { bridge, frames, settleWake, startWake, command } = setup();
    await startWake();
    await settleWake();
    frames(8, 0.35);
    frames(4, 0.01); // 512 ms inside the sentence: keep listening
    expect(bridge.voiceUtterance.mock.calls.length).toBe(0);
    frames(8, -0.2);
    frames(4, 0.01);
    expect(bridge.voiceUtterance.mock.calls.length).toBe(0);
    frames(1, 0.01); // 640 ms since speech ended, using real 2048-sample frames
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalledTimes(1));
    expect(sampleCount(command(), 0.35)).toBe(8 * FRAME);
    expect(sampleCount(command(), -0.2)).toBe(8 * FRAME);
  });

  it("keeps the beginning of a command still being captured when wake succeeds", async () => {
    const { bridge, frames, settleWake, startWake, command } = setup();
    await startWake();
    frames(8, 0.35);
    await settleWake();
    expect(bridge.voiceUtterance).not.toHaveBeenCalled();
    frames(4, -0.2);
    frames(8, 0);
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalledTimes(1));
    expect(sampleCount(command(), 0.35)).toBe(8 * FRAME);
    expect(sampleCount(command(), -0.2)).toBe(4 * FRAME);
  });

  it("joins queued speech with the active continuation before submitting it", async () => {
    const { bridge, frames, settleWake, startWake, command } = setup();
    await startWake();
    frames(8, 0.35);
    frames(8, 0);
    frames(4, -0.2);
    await settleWake();
    expect(bridge.voiceUtterance).not.toHaveBeenCalled();
    frames(4, -0.2);
    frames(8, 0);
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalledTimes(1));
    expect(sampleCount(command(), 0.35)).toBe(8 * FRAME);
    expect(sampleCount(command(), -0.2)).toBe(8 * FRAME);
  });

  it("keeps an unconfirmed speech onset when a wake result arrives during the first 96 ms", async () => {
    const { fake, bridge, frames, settleWake, startWake, command } = setup();
    await startWake();
    frames(8, 0.35);
    frames(8, 0);
    act(() => fake.emit(new Float32Array(3 * 512).fill(-0.2)));
    await settleWake();
    expect(bridge.voiceUtterance.mock.calls.length).toBe(0);
    frames(8, -0.2);
    frames(8, 0);
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalledTimes(1));
    expect(sampleCount(command(), 0.35)).toBe(8 * FRAME);
    expect(sampleCount(command(), -0.2)).toBe(8 * FRAME + 3 * 512);
  });

  it("releases a completed pending turn when an unconfirmed wake-handoff onset is only a click", async () => {
    const { fake, bridge, frames, settleWake, startWake, command } = setup();
    await startWake();
    frames(8, 0.35);
    frames(8, 0);
    act(() => fake.emit(new Float32Array(3 * 512).fill(-0.2)));
    await settleWake();
    expect(bridge.voiceUtterance.mock.calls.length).toBe(0);
    act(() => fake.emit(new Float32Array(512)));
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalledTimes(1));
    expect(sampleCount(command(), 0.35)).toBe(8 * FRAME);
    expect(sampleCount(command(), -0.2)).toBe(0);
  });

  it.each([false, true])("does not submit trailing silence after a capped wake-only segment (mixed frame: %s)", async (mixedFrame) => {
    const { fake, bridge, frames, settleWake } = setup();
    await waitFor(() => expect(state()).toBe("sleeping"));
    frames(31, 0.25);
    const lastFrame = new Float32Array(mixedFrame ? FRAME : 512);
    lastFrame.fill(0.25, 0, 512); // speech ends exactly at the 4s wake cap
    act(() => fake.emit(lastFrame));
    expect(bridge.voiceWake).toHaveBeenCalledTimes(1);
    await settleWake(0, false);
    frames(8, 0);
    expect(bridge.voiceUtterance.mock.calls.length).toBe(0);
    frames(8, 0.35);
    frames(8, 0);
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalledTimes(1));
  });

  it.each([16_000, 44_100, 48_000])("keeps a continuous wake-and-command through the 4s cap at %i Hz", async (rate) => {
    const { bridge, frames, settleWake, command } = setup(rate);
    await waitFor(() => expect(state()).toBe("sleeping"));
    const count = Math.ceil(rate * 5.5 / FRAME);
    frames(count, 0.25);
    expect(bridge.voiceWake).toHaveBeenCalledTimes(1);
    expect(pcm(bridge.voiceWake.mock.calls[0]?.[0] as string).length).toBe(4 * 16_000);
    await settleWake(0, true);
    expect(bridge.voiceUtterance).not.toHaveBeenCalled();
    frames(Math.ceil(rate / FRAME), 0);
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalledTimes(1));
    const expected = Math.floor(count * FRAME * 16_000 / rate);
    // A low-pass filter smooths the speech/silence edge; its midpoint still
    // marks the full captured duration, including both sides of the cap.
    const voiced = pcm(command()).filter((sample) => sample > 0.125 * 32767).length;
    expect(Math.abs(voiced - expected)).toBeLessThanOrEqual(1);
  });

  it("discards a capped turn during a slow wake check instead of submitting its first 60 seconds", async () => {
    const { bridge, frames, settleWake, startWake } = setup();
    await startWake();
    frames(600, 0.35); // over 76s; capture must stop at its bounded budget
    expect(bridge.voiceWake).toHaveBeenCalledTimes(1);
    expect(bridge.voiceUtterance).not.toHaveBeenCalled();
    await settleWake(0, true);
    expect(await screen.findByText("That recording reached 60 seconds. Please try a shorter request.")).toBeInTheDocument();
    expect(bridge.voiceUtterance).not.toHaveBeenCalled();
    expect(state()).toBe("sleeping");
  });

  it("discards a continuous 60-second question without dispatching a partial request", async () => {
    const { bridge, frames, settleWake, startWake } = setup();
    await startWake();
    await settleWake();
    frames(469, 0.35); // passes the exact 960,000-sample cap
    expect(await screen.findByText("That recording reached 60 seconds. Please try a shorter request.")).toBeInTheDocument();
    expect(bridge.voiceUtterance).not.toHaveBeenCalled();
    expect(state()).toBe("sleeping");
  });

  it("retains a complete question longer than the old 30-second limit", async () => {
    const { bridge, frames, settleWake, startWake, command } = setup();
    await startWake();
    await settleWake();
    frames(313, 0.35); // 40.064 seconds of actual samples
    expect(bridge.voiceUtterance).not.toHaveBeenCalled();
    frames(5, 0);
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalledTimes(1));
    expect(sampleCount(command(), 0.35)).toBe(313 * FRAME);
    expect(pcm(command()).length).toBeLessThan(60 * 16_000);
  });

  it("keeps listening through speech and starts the idle period after the turn", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { fake, bridge, frames, settleWake, startWake } = setup();
    await startWake();
    await settleWake();
    act(() => vi.advanceTimersByTime(18_000));
    frames(2, 0.35);
    expect(state()).toBe("listening");
    act(() => {
      for (let i = 0; i < 20; i++) {
        fake.emit(new Float32Array(FRAME).fill(0.35));
        vi.advanceTimersByTime(128);
      }
    });
    expect(state()).toBe("listening");
    expect(bridge.voiceUtterance).not.toHaveBeenCalled();
    frames(8, 0);
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(state()).toBe("attending"));
    act(() => vi.advanceTimersByTime(19_000));
    expect(state()).toBe("attending");
    act(() => vi.advanceTimersByTime(1_100));
    expect(state()).toBe("sleeping");
  });

  it("ignores an old wake result without releasing a newer pending wake check", async () => {
    const { bridge, frames, settleWake, startWake, command } = setup();
    await startWake();
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    visibility.mockReturnValue("visible");
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() => expect(state()).toBe("sleeping"));
    frames(4, 0.15);
    frames(4, 0);
    expect(bridge.voiceWake).toHaveBeenCalledTimes(2);
    await settleWake(0, true);
    expect(state()).toBe("sleeping");
    expect(bridge.voiceUtterance).not.toHaveBeenCalled();
    frames(8, 0.35);
    frames(8, 0);
    expect(bridge.voiceWake).toHaveBeenCalledTimes(2);
    await settleWake(1);
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalledTimes(1));
    expect(sampleCount(command(), 0.35)).toBe(8 * FRAME);
  });

  it("does not deliver an old turn result into a restarted listening session", async () => {
    let finish: ((result: VoiceResponse) => void) | undefined;
    const { bridge, frames, settleWake, startWake } = setup(16_000, () => new Promise((resolve) => { finish = resolve; }));
    await startWake();
    await settleWake();
    frames(8, 0.35);
    frames(8, 0);
    expect(bridge.voiceUtterance).toHaveBeenCalledTimes(1);
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    visibility.mockReturnValue("visible");
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() => expect(state()).toBe("sleeping"));
    await act(async () => finish?.(response));
    expect(state()).toBe("sleeping");
    expect(document.body.textContent).not.toContain(response.transcript);
  });
});
