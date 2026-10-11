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
    expect(MAX_RECORDING_SAMPLES).toBe(TARGET_SAMPLE_RATE * 60);
    // 60 s of 16-bit mono stays below the backend's audio size limit.
    expect(MAX_RECORDING_SAMPLES * 2 + 44).toBeLessThan(8 * 1024 * 1024);
  });

  it.each([44_100, 48_000])("preserves speech-band PCM and rejects aliased frequencies at %i Hz", (rate) => {
    const amplitude = 0.4;
    const outputRms = (frequency: number) => {
      const input = Float32Array.from({ length: rate }, (_, i) => amplitude * Math.sin(2 * Math.PI * frequency * i / rate));
      const wav = encodeWav(resample(input, rate, TARGET_SAMPLE_RATE), TARGET_SAMPLE_RATE);
      const pcm = new DataView(wav.buffer, 44);
      let sum = 0;
      const margin = 100; // omit the finite clip boundaries, not the filter response
      const length = pcm.byteLength / 2;
      for (let i = margin; i < length - margin; i++) sum += (pcm.getInt16(i * 2, true) / 32768) ** 2;
      expect(input[1]).not.toBe(0); // resampling does not mutate capture buffers
      return Math.sqrt(sum / (length - margin * 2));
    };
    const reference = amplitude / Math.sqrt(2);
    expect(outputRms(1_000) / reference).toBeCloseTo(1, 2);
    expect(outputRms(6_000) / reference).toBeGreaterThan(0.98);
    for (const frequency of [9_000, 10_000, 18_000]) {
      expect(outputRms(frequency) / reference).toBeLessThan(0.01); // at least 40 dB rejection
    }
  });

  it.each([44_100, 48_000])("preserves duration and constant level at filtered clip boundaries (%i Hz)", (rate) => {
    const input = new Float32Array(rate).fill(0.25);
    const output = resample(input, rate, TARGET_SAMPLE_RATE);
    expect(output.length).toBe(TARGET_SAMPLE_RATE);
    expect(output[0]).toBeCloseTo(0.25, 6);
    expect(output[output.length - 1]).toBeCloseTo(0.25, 6);
    expect(input[0]).toBe(0.25);
  });
});
