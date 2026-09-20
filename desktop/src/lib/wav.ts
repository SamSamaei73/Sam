/**
 * Mono 16-bit PCM WAV encoding for the explicit push-to-record flow.
 * Produces exactly the canonical 44-byte header layout Sam's Phase 9 audio
 * validator accepts (RIFF/WAVE, PCM tag 1, mono, 16-bit, consistent sizes).
 */

export const TARGET_SAMPLE_RATE = 16_000;
/** Matches the backend voice limit (Phase 9 MAX_AUDIO_DURATION_SECONDS = 30). */
export const MAX_RECORDING_SECONDS = 30;
export const MAX_RECORDING_SAMPLES = TARGET_SAMPLE_RATE * MAX_RECORDING_SECONDS;

/** Linear-interpolating resampler (bounded, allocation = output size). */
export function resample(input: Float32Array, from: number, to: number): Float32Array {
  if (from === to || input.length === 0) return input;
  const length = Math.max(1, Math.floor((input.length * to) / from));
  const out = new Float32Array(length);
  const ratio = from / to;
  for (let i = 0; i < length; i++) {
    const pos = i * ratio;
    const idx = Math.floor(pos);
    const frac = pos - idx;
    const a = input[idx] ?? 0;
    const b = input[Math.min(idx + 1, input.length - 1)] ?? a;
    out[i] = a + (b - a) * frac;
  }
  return out;
}

export function encodeWav(samples: Float32Array, sampleRate: number): Uint8Array {
  const dataBytes = samples.length * 2;
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);
  const text = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i));
  };
  text(0, "RIFF");
  view.setUint32(4, 36 + dataBytes, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  text(36, "data");
  view.setUint32(40, dataBytes, true);
  let offset = 44;
  for (const sample of samples) {
    const clamped = Math.max(-1, Math.min(1, sample));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    offset += 2;
  }
  return new Uint8Array(buffer);
}

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

export function base64ToBytes(value: string): Uint8Array {
  const binary = atob(value);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}
