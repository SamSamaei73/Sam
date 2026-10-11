import { describe, expect, it, vi } from "vitest";
import { Recorder, RecorderError } from "../lib/recorder";
import { Listener } from "../lib/listener";
import { MAX_RECORDING_SAMPLES, base64ToBytes, encodeWav } from "../lib/wav";
import { fakeAudioEnvironment } from "./helpers";

describe("Recorder", () => {
  it("stops a late permission stream after cancellation without creating a capture graph", async () => {
    const fake = fakeAudioEnvironment();
    let grant: ((stream: MediaStream) => void) | undefined;
    fake.env.getUserMedia = vi.fn(() => new Promise<MediaStream>((resolve) => { grant = resolve; }));
    const createContext = vi.spyOn(fake.env, "createContext");
    const recorder = new Recorder(fake.env);
    const starting = recorder.start();
    recorder.cancel();
    grant?.({ getTracks: () => [fake.track] } as unknown as MediaStream);
    await starting;
    expect(recorder.isRecording).toBe(false);
    expect(fake.track.stop).toHaveBeenCalledExactlyOnceWith();
    expect(createContext).not.toHaveBeenCalled();
  });

  it.each(["resolve", "reject"] as const)("cancellation releases a suspended context even if its old resume later %ss", async (outcome) => {
    const fake = fakeAudioEnvironment();
    let resume: (() => void) | undefined;
    let fail: (() => void) | undefined;
    Object.defineProperty(fake.context, "state", { value: "suspended" });
    fake.context.resume = vi.fn(() => new Promise<void>((resolve, reject) => {
      resume = resolve;
      fail = () => reject(new Error("old context closed"));
    }));
    const recorder = new Recorder(fake.env);
    const starting = recorder.start();
    await Promise.resolve();
    expect(fake.context.resume).toHaveBeenCalledTimes(1);
    recorder.cancel();
    expect(fake.track.stop).toHaveBeenCalled();
    expect(fake.context.close).toHaveBeenCalled();
    if (outcome === "resolve") resume?.(); else fail?.();
    await starting;
    expect(recorder.isRecording).toBe(false);
    expect(fake.processor.onaudioprocess).toBeNull();
    await expect(recorder.stop()).rejects.toMatchObject({ code: "failed" });
  });

  it("an old permission failure cannot stop a newer recording", async () => {
    const fake = fakeAudioEnvironment();
    let rejectOld: ((reason: Error) => void) | undefined;
    fake.env.getUserMedia = vi.fn()
      .mockImplementationOnce(() => new Promise<MediaStream>((_, reject) => { rejectOld = reject; }))
      .mockImplementation(fake.getUserMedia);
    const recorder = new Recorder(fake.env);
    const old = recorder.start();
    recorder.cancel();
    await recorder.start();
    rejectOld?.(new Error("old permission failed"));
    await old;
    expect(recorder.isRecording).toBe(true);
    fake.emit(new Float32Array(1_600).fill(0.2));
    expect((await recorder.stop()).durationSeconds).toBe(0.1);
  });

  it("requests the same microphone processing for enrollment and hands-free", async () => {
    const enrollmentAudio = fakeAudioEnvironment();
    const handsFreeAudio = fakeAudioEnvironment();
    const recorder = new Recorder(enrollmentAudio.env);
    const listener = new Listener(handsFreeAudio.env, () => undefined);
    try {
      await recorder.start();
      await listener.start();
      expect(enrollmentAudio.getUserMedia.mock.calls).toEqual(handsFreeAudio.getUserMedia.mock.calls);
      expect(enrollmentAudio.getUserMedia).toHaveBeenCalledWith({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
        video: false,
      });
    } finally {
      recorder.cancel();
      listener.stop();
    }
  });

  it("encodes the captured signal before wiping owned buffers", async () => {
    const fake = fakeAudioEnvironment();
    const recorder = new Recorder(fake.env);
    const signal = Float32Array.from({ length: 32_000 }, (_, i) => 0.2 * Math.sin(i / 7));
    const expected = encodeWav(signal, 16_000);
    await recorder.start();
    fake.emit(signal);
    const retained = [...(recorder as unknown as { chunks: Float32Array[] }).chunks];
    const result = await recorder.stop();
    expect(base64ToBytes(result.base64)).toEqual(expected);
    expect(result.durationSeconds).toBe(2);
    expect(retained.every((chunk) => chunk.every((sample) => sample === 0))).toBe(true);
    expect(signal.some((sample) => sample !== 0)).toBe(true); // caller still owns its input
    expect(fake.track.stop).toHaveBeenCalled();
  });

  it("wipes cancelled audio and does not include it in the next recording", async () => {
    const fake = fakeAudioEnvironment();
    const recorder = new Recorder(fake.env);
    await recorder.start();
    fake.emit(new Float32Array(3_200).fill(0.4));
    const retained = [...(recorder as unknown as { chunks: Float32Array[] }).chunks];
    recorder.cancel();
    expect(retained.every((chunk) => chunk.every((sample) => sample === 0))).toBe(true);
    await recorder.start();
    const next = new Float32Array(1_600).fill(-0.2);
    fake.emit(next);
    expect(base64ToBytes((await recorder.stop()).base64)).toEqual(encodeWav(next, 16_000));
  });

  it.each([44_100, 48_000])("preserves an audible signal when resampling from %i Hz", async (rate) => {
    const fake = fakeAudioEnvironment();
    Object.defineProperty(fake.context, "sampleRate", { value: rate });
    const recorder = new Recorder(fake.env);
    await recorder.start();
    fake.emit(Float32Array.from({ length: rate * 2 }, (_, i) => 0.2 * Math.sin(2 * Math.PI * 440 * i / rate)));
    const wav = base64ToBytes((await recorder.stop()).base64);
    const pcm = new DataView(wav.buffer, 44);
    let sum = 0;
    for (let offset = 0; offset < pcm.byteLength; offset += 2) sum += pcm.getInt16(offset, true) ** 2;
    expect(pcm.byteLength / 2).toBe(32_000);
    expect(Math.sqrt(sum / 32_000)).toBeGreaterThan(4_500);
    expect(fake.track.stop).toHaveBeenCalled();
  });

  it("does not touch the microphone until start() is called", () => {
    const fake = fakeAudioEnvironment();
    new Recorder(fake.env);
    expect(fake.getUserMedia).not.toHaveBeenCalled();
  });

  it("reports unsupported without an audio environment", async () => {
    await expect(new Recorder(null).start()).rejects.toMatchObject({ code: "unsupported" });
  });

  it("maps a denied permission to a safe error", async () => {
    const fake = fakeAudioEnvironment();
    fake.env.getUserMedia = vi.fn(async () => {
      throw Object.assign(new Error("x"), { name: "NotAllowedError" });
    });
    await expect(new Recorder(fake.env).start()).rejects.toMatchObject({ code: "denied" });
  });

  it("stops every track, disconnects and closes the context on stop()", async () => {
    const fake = fakeAudioEnvironment();
    const recorder = new Recorder(fake.env);
    await recorder.start();
    fake.emit(new Float32Array(1600).fill(0.1));
    const result = await recorder.stop();
    expect(result.base64.length).toBeGreaterThan(40);
    expect(fake.track.stop).toHaveBeenCalled();
    expect(fake.processor.disconnect).toHaveBeenCalled();
    expect(fake.context.close).toHaveBeenCalled();
    expect(recorder.isRecording).toBe(false);
  });

  it("releases everything and yields nothing on cancel()", async () => {
    const fake = fakeAudioEnvironment();
    const recorder = new Recorder(fake.env);
    await recorder.start();
    fake.emit(new Float32Array(800).fill(0.2));
    recorder.cancel();
    expect(fake.track.stop).toHaveBeenCalled();
    await expect(recorder.stop()).rejects.toBeInstanceOf(RecorderError);
  });

  it("rejects an empty recording", async () => {
    const fake = fakeAudioEnvironment();
    const recorder = new Recorder(fake.env);
    await recorder.start();
    await expect(recorder.stop()).rejects.toMatchObject({ code: "empty" });
    expect(fake.track.stop).toHaveBeenCalled();
  });

  it("discards a capped recording instead of returning an incomplete question", async () => {
    const fake = fakeAudioEnvironment();
    const onLimit = vi.fn();
    const recorder = new Recorder(fake.env, onLimit);
    await recorder.start();
    const frame = new Float32Array(MAX_RECORDING_SAMPLES).fill(0.1);
    fake.emit(frame);
    fake.emit(frame); // extra audio beyond the cap is discarded
    expect(onLimit).toHaveBeenCalled();
    const retained = [...(recorder as unknown as { chunks: Float32Array[] }).chunks];
    await expect(recorder.stop()).rejects.toMatchObject({ code: "limit" });
    expect(retained.every((chunk) => chunk.every((sample) => sample === 0))).toBe(true);
    expect(fake.track.stop).toHaveBeenCalled();
  });
});
