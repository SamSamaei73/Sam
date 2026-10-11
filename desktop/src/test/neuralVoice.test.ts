import { afterEach, describe, expect, it, vi } from "vitest";
import { createNeuralSpeechInput, SpeechFrameQueue } from "../lib/neuralVoice";
import { Listener } from "../lib/listener";
import { fakeAudioEnvironment } from "./helpers";

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("bounded neural capture", () => {
  it("wipes pending and stale frames across pause, then delivers only fresh audio", () => {
    const sent: { frame: Float32Array; epoch: number }[] = [];
    const delivered: Float32Array[] = [];
    const control = vi.fn();
    const reset = vi.fn();
    const queue = new SpeechFrameQueue((frame, epoch) => sent.push({ frame, epoch }),
      (frame) => delivered.push(frame.slice()), control, reset, vi.fn());
    queue.resume();
    const old = new Float32Array(512).fill(0.4);
    const pending = new Float32Array(512).fill(0.3);
    queue.capture(old, 0);
    queue.capture(pending, 0);
    queue.pause();
    expect(pending.every((value) => value === 0)).toBe(true);
    queue.resume();
    const stale = new Float32Array(512).fill(0.2);
    queue.capture(stale, 0);
    expect(stale.every((value) => value === 0)).toBe(true);
    const fresh = new Float32Array(512).fill(-0.25);
    queue.capture(fresh, 1);
    expect(sent).toHaveLength(1); // one inference at a time
    queue.complete(old, 0.95, 0);
    expect(delivered).toHaveLength(0);
    expect(sent[1]?.epoch).toBe(1);
    queue.complete(fresh, 0.9, 1);
    expect(delivered).toEqual([new Float32Array(512).fill(-0.25)]);
    expect(old.every((value) => value === 0)).toBe(true);
    expect(fresh.every((value) => value === 0)).toBe(true);
    expect(reset).toHaveBeenCalledTimes(1);
    expect(control).toHaveBeenLastCalledWith("ack", 1);
  });

  it("fails closed with bounded memory if inference cannot keep up", () => {
    const send = vi.fn();
    const failure = vi.fn();
    const deliver = vi.fn();
    const queue = new SpeechFrameQueue(send, deliver, vi.fn(), vi.fn(), failure);
    queue.resume();
    const frames = Array.from({ length: 18 }, () => new Float32Array(512).fill(0.2));
    for (const frame of frames) queue.capture(frame, 0);
    expect(send).toHaveBeenCalledTimes(1);
    expect(failure).toHaveBeenCalledTimes(1);
    expect(frames.slice(1).every((frame) => frame.every((value) => value === 0))).toBe(true);
    queue.complete(frames[0]!, 0.9, 0);
    expect(deliver).not.toHaveBeenCalled();
    expect(frames[0]?.every((value) => value === 0)).toBe(true);
  });

  it.each([44_100, 48_000])("never labels a %i Hz audio context as model input at 16 kHz", async (sampleRate) => {
    const worker = vi.fn();
    vi.stubGlobal("Worker", worker);
    await expect(createNeuralSpeechInput({ sampleRate, audioWorklet: {} } as AudioContext,
      {} as MediaStream, vi.fn(), vi.fn())).rejects.toThrow("unavailable");
    expect(worker).not.toHaveBeenCalled();
  });

  it.each(["context", "detector"] as const)("closes the granted microphone when %s setup fails", async (stage) => {
    const fake = fakeAudioEnvironment();
    const onSegment = vi.fn();
    if (stage === "context") {
      fake.env.createContext = () => { throw new Error("Audio context unavailable"); };
    } else {
      fake.env.createSpeechInput = async () => { throw new Error("Local model unavailable"); };
    }
    const listener = new Listener(fake.env, onSegment);
    await expect(listener.start()).rejects.toMatchObject({ code: "failed" });
    expect(fake.track.stop).toHaveBeenCalledExactlyOnceWith();
    expect(listener.isActive).toBe(false);
    expect(onSegment).not.toHaveBeenCalled();
    if (stage === "detector") expect(fake.context.close).toHaveBeenCalledExactlyOnceWith();
  });

  it("bounds startup even if resuming the audio context never resolves", async () => {
    vi.useFakeTimers();
    const terminate = vi.fn();
    class WorkerStub {
      onmessage: ((event: { data: unknown }) => void) | null = null;
      postMessage(message: { type: string }) {
        if (message.type === "init") queueMicrotask(() => this.onmessage?.({ data: { type: "ready" } }));
      }
      terminate = terminate;
    }
    const disconnect = vi.fn();
    const close = vi.fn();
    vi.stubGlobal("Worker", WorkerStub);
    vi.stubGlobal("AudioWorkletNode", class {
      port = { postMessage: vi.fn(), close };
      connect = vi.fn();
      disconnect = disconnect;
    });
    const resume = vi.fn(() => new Promise<void>(() => undefined));
    const context = {
      sampleRate: 16_000, audioWorklet: { addModule: vi.fn(async () => undefined) },
      createMediaStreamSource: () => ({ connect: vi.fn(), disconnect }),
      destination: {}, resume,
    } as unknown as AudioContext;
    const failed = expect(createNeuralSpeechInput(context, {} as MediaStream, vi.fn(), vi.fn()))
      .rejects.toThrow("Local speech detector failed");
    await vi.advanceTimersByTimeAsync(1);
    expect(resume).toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(15_000);
    await failed;
    expect(terminate).toHaveBeenCalledTimes(1);
    expect(disconnect).toHaveBeenCalledTimes(2);
    expect(close).toHaveBeenCalled();
  });

  it("an old initialization failure cannot stop a restarted listener", async () => {
    const fake = fakeAudioEnvironment();
    let failFirst: ((failure: Error) => void) | undefined;
    const oldFactory = fake.env.createSpeechInput!;
    fake.env.createSpeechInput = vi.fn()
      .mockImplementationOnce(() => new Promise((_, reject) => { failFirst = reject; }))
      .mockImplementation(oldFactory);
    const listener = new Listener(fake.env, vi.fn());
    const first = listener.start();
    await Promise.resolve();
    listener.stop();
    await listener.start();
    expect(listener.isActive).toBe(true);
    failFirst?.(new Error("old worker failed"));
    await first;
    expect(listener.isActive).toBe(true);
    listener.stop();
  });
});
