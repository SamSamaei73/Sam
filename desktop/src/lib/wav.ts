/**
 * Mono 16-bit PCM WAV encoding for the explicit push-to-record flow.
 * Produces exactly the canonical 44-byte header layout Sam's Phase 9 audio
 * validator accepts (RIFF/WAVE, PCM tag 1, mono, 16-bit, consistent sizes).
 */

export const TARGET_SAMPLE_RATE = 16_000;
/** Matches the backend's bounded per-turn voice limit. */
export const MAX_RECORDING_SECONDS = 60;
export const MAX_RECORDING_SAMPLES = TARGET_SAMPLE_RATE * MAX_RECORDING_SECONDS;

interface ResampleFilter {
  ratio: number;
  radius: number;
  kernels: Float32Array[];
}

// One small polyphase filter cache, shared by enrollment and hands-free audio.
// It contains coefficients only, never microphone samples.
let resampleFilter: ResampleFilter | undefined;

function downsampleFilter(ratio: number): ResampleFilter {
  if (resampleFilter?.ratio === ratio) return resampleFilter;
  const radius = Math.ceil(16 * ratio);
  const phases = 256;
  const cutoff = 0.45 / ratio; // leave a transition band below the output Nyquist
  const kernels = Array.from({ length: phases }, (_, phase) => {
    const kernel = new Float32Array(radius * 2 + 1);
    let total = 0;
    for (let tap = -radius; tap <= radius; tap++) {
      const distance = tap - phase / phases;
      const x = 2 * Math.PI * cutoff * distance;
      const window = Math.abs(distance) > radius ? 0 :
        0.42 + 0.5 * Math.cos(Math.PI * distance / radius) +
        0.08 * Math.cos(2 * Math.PI * distance / radius);
      const coefficient = 2 * cutoff * (Math.abs(x) < 1e-8 ? 1 : Math.sin(x) / x) * window;
      kernel[tap + radius] = coefficient;
      total += coefficient;
    }
    for (let tap = 0; tap < kernel.length; tap++) kernel[tap] = kernel[tap]! / total;
    return kernel;
  });
  resampleFilter = { ratio, radius, kernels };
  return resampleFilter;
}

/** Bounded resampling with a Blackman-windowed sinc anti-alias filter on downsampling. */
export function resample(input: Float32Array, from: number, to: number): Float32Array {
  if (from === to || input.length === 0) return input;
  const length = Math.max(1, Math.floor((input.length * to) / from));
  const out = new Float32Array(length);
  const ratio = from / to;
  if (ratio > 1) {
    const { radius, kernels } = downsampleFilter(ratio);
    for (let i = 0; i < length; i++) {
      const position = i * ratio;
      const center = Math.floor(position);
      const kernel = kernels[Math.floor((position - center) * kernels.length)]!;
      const first = center - radius;
      let sample = 0;
      if (first >= 0 && first + kernel.length <= input.length) {
        for (let tap = 0; tap < kernel.length; tap++) sample += input[first + tap]! * kernel[tap]!;
      } else {
        // Extend the boundary sample: constant signals remain constant and
        // the first syllable is not shortened by the filter's look-around.
        for (let tap = 0; tap < kernel.length; tap++) {
          const index = Math.max(0, Math.min(input.length - 1, first + tap));
          sample += input[index]! * kernel[tap]!;
        }
      }
      out[i] = sample;
    }
    return out;
  }
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

/** Join locally encoded microphone segments without resampling their PCM. */
export function joinWavSegments(segments: readonly string[]): string {
  const parts: Uint8Array[] = [];
  let joined: Uint8Array | undefined;
  try {
    let dataBytes = 0;
    for (const segment of segments) {
      const bytes = base64ToBytes(segment);
      parts.push(bytes);
      const header = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      if (
        bytes.length < 44 || (bytes.length - 44) % 2 !== 0 ||
        header.getUint32(0) !== 0x52494646 || header.getUint32(8) !== 0x57415645 ||
        header.getUint32(12) !== 0x666d7420 || header.getUint32(16, true) !== 16 ||
        header.getUint16(20, true) !== 1 || header.getUint16(22, true) !== 1 ||
        header.getUint32(24, true) !== TARGET_SAMPLE_RATE || header.getUint16(34, true) !== 16 ||
        header.getUint32(36) !== 0x64617461 || header.getUint32(40, true) !== bytes.length - 44
      ) throw new Error("Invalid local audio segment");
      dataBytes += bytes.length - 44;
      if (dataBytes > MAX_RECORDING_SAMPLES * 2) throw new Error("Voice turn exceeds the capture limit");
    }
    const first = parts[0];
    if (!first) throw new Error("Voice turn is empty");
    joined = new Uint8Array(44 + dataBytes);
    joined.set(first.subarray(0, 44));
    let offset = 44;
    for (const part of parts) {
      joined.set(part.subarray(44), offset);
      offset += part.length - 44;
    }
    const header = new DataView(joined.buffer);
    header.setUint32(4, 36 + dataBytes, true);
    header.setUint32(40, dataBytes, true);
    return bytesToBase64(joined);
  } finally {
    for (const part of parts) part.fill(0);
    joined?.fill(0);
  }
}
