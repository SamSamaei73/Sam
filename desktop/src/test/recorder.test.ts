import { describe, expect, it, vi } from "vitest";
import { Recorder, RecorderError } from "../lib/recorder";
import { MAX_RECORDING_SAMPLES } from "../lib/wav";
import { fakeAudioEnvironment } from "./helpers";

describe("Recorder", () => {
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

  it("caps the recording length and signals the limit", async () => {
    const fake = fakeAudioEnvironment();
    const onLimit = vi.fn();
    const recorder = new Recorder(fake.env, onLimit);
    await recorder.start();
    const frame = new Float32Array(MAX_RECORDING_SAMPLES).fill(0.1);
    fake.emit(frame);
    fake.emit(frame); // extra audio beyond the cap is discarded
    expect(onLimit).toHaveBeenCalled();
    const result = await recorder.stop();
    expect(result.durationSeconds).toBeLessThanOrEqual(30.01);
  });
});
