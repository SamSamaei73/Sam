import { describe, expect, it } from "vitest";
import {
  MAX_RECORDING_SAMPLES,
  TARGET_SAMPLE_RATE,
  base64ToBytes,
  bytesToBase64,
  encodeWav,
  resample,
} from "../lib/wav";

const ascii = (bytes: Uint8Array, start: number, end: number) => String.fromCharCode(...bytes.slice(start, end));

describe("wav encoding", () => {
  it("produces a canonical mono PCM16 header with consistent sizes", () => {
    const wav = encodeWav(new Float32Array([0, 0.5, -0.5, 1, -1]), 16_000);
    const view = new DataView(wav.buffer);
    expect(ascii(wav, 0, 4)).toBe("RIFF");
    expect(ascii(wav, 8, 12)).toBe("WAVE");
    expect(ascii(wav, 12, 16)).toBe("fmt ");
    expect(view.getUint16(20, true)).toBe(1); // PCM
    expect(view.getUint16(22, true)).toBe(1); // mono
    expect(view.getUint32(24, true)).toBe(16_000);
    expect(view.getUint32(28, true)).toBe(32_000);
    expect(view.getUint16(32, true)).toBe(2);
    expect(view.getUint16(34, true)).toBe(16);
    expect(ascii(wav, 36, 40)).toBe("data");
    expect(view.getUint32(40, true)).toBe(10);
    expect(view.getUint32(4, true)).toBe(wav.length - 8);
    expect(wav.length).toBe(44 + 10);
  });

  it("clamps out-of-range samples", () => {
    const wav = encodeWav(new Float32Array([5, -5]), 16_000);
    const view = new DataView(wav.buffer);
    expect(view.getInt16(44, true)).toBe(32767);
    expect(view.getInt16(46, true)).toBe(-32768);
  });

  it("round-trips base64", () => {
    const bytes = new Uint8Array([0, 1, 2, 250, 255]);
    expect(Array.from(base64ToBytes(bytes ? bytesToBase64(bytes) : ""))).toEqual(Array.from(bytes));
  });

  it("resamples to the target length and bounds the recording", () => {
    const out = resample(new Float32Array(48_000), 48_000, TARGET_SAMPLE_RATE);
    expect(out.length).toBe(16_000);
    expect(MAX_RECORDING_SAMPLES).toBe(TARGET_SAMPLE_RATE * 30);
    // 30 s (the Phase 9 backend limit) of 16-bit mono stays far below the backend's audio size limit.
    expect(MAX_RECORDING_SAMPLES * 2 + 44).toBeLessThan(8 * 1024 * 1024);
  });
});
