import {
  MAX_RECORDING_SAMPLES,
  TARGET_SAMPLE_RATE,
  bytesToBase64,
  encodeWav,
  resample,
} from "./wav";
import { createNeuralSpeechInput, type SpeechInputFactory } from "./neuralVoice";

/**
 * Explicit, bounded, in-memory microphone capture.
 *
 * - Only ever started by a user gesture (the caller's responsibility, and the
 *   only call site is a click/keypress handler in VoiceRecorder).
 * - Never continuous: it stops itself at MAX_RECORDING_SECONDS.
 * - `stop()`/`cancel()` stop every track, disconnect the audio graph, close
 *   the context and drop all buffered samples. Nothing is written to disk,
 *   localStorage, IndexedDB or a URL.
 */

export type RecorderErrorCode = "unsupported" | "denied" | "failed" | "empty" | "limit";

export class RecorderError extends Error {
  readonly code: RecorderErrorCode;
  constructor(code: RecorderErrorCode) {
    super(code);
    this.name = "RecorderError";
    this.code = code;
  }
}

export interface AudioEnvironment {
  getUserMedia: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  createContext: (sampleRate?: number) => AudioContext;
  createSpeechInput?: SpeechInputFactory;
}

/** Enrollment, push-to-talk and hands-free use the same microphone processing. */
export function voiceCaptureConstraints(): MediaStreamConstraints {
  return {
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    video: false,
  };
}

export function browserAudioEnvironment(): AudioEnvironment | null {
  const media = typeof navigator !== "undefined" ? navigator.mediaDevices : undefined;
  const Ctor =
    typeof window !== "undefined"
      ? (window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext)
      : undefined;
  if (!media || typeof media.getUserMedia !== "function" || !Ctor) return null;
  return {
    getUserMedia: (c) => media.getUserMedia(c),
    createContext: (sampleRate) => new Ctor(sampleRate ? { sampleRate } : undefined),
    createSpeechInput: createNeuralSpeechInput,
  };
}

export interface RecordingResult {
  base64: string;
  durationSeconds: number;
}

export class Recorder {
  private stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private processor: ScriptProcessorNode | null = null;
  private chunks: Float32Array[] = [];
  private total = 0;
  private active = false;
  private limitReached = false;
  private generation = 0;

  constructor(
    private readonly env: AudioEnvironment | null = browserAudioEnvironment(),
    private readonly onLimit: () => void = () => undefined,
    /** The real input level (RMS, 0..1) of each captured frame, for the UI. */
    private readonly onLevel: (level: number) => void = () => undefined,
  ) {}

  get isRecording(): boolean {
    return this.active;
  }

  async start(): Promise<void> {
    if (this.active) return;
    if (!this.env) throw new RecorderError("unsupported");
    const generation = ++this.generation;
    let stream: MediaStream;
    try {
      stream = await this.env.getUserMedia(voiceCaptureConstraints());
    } catch (error) {
      if (generation !== this.generation) return;
      const name = (error as { name?: string } | null)?.name ?? "";
      throw new RecorderError(
        name === "NotAllowedError" || name === "SecurityError" ? "denied" : "failed",
      );
    }
    if (generation !== this.generation) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    try {
      this.stream = stream;
      const context = this.env.createContext();
      this.context = context;
      const source = context.createMediaStreamSource(stream);
      this.source = source;
      const processor = context.createScriptProcessor(4096, 1, 1);
      this.processor = processor;
      this.chunks = [];
      this.total = 0;
      this.limitReached = false;
      processor.onaudioprocess = (event) => {
        if (generation === this.generation) this.capture(event.inputBuffer.getChannelData(0));
      };
      source.connect(processor);
      // Connected only so the node runs; its output buffer is silent.
      processor.connect(context.destination);
      if (context.state === "suspended") await context.resume();
      if (generation !== this.generation) return;
      this.active = true;
    } catch {
      if (generation !== this.generation) return;
      this.release();
      throw new RecorderError("failed");
    }
  }

  private capture(frame: Float32Array): void {
    if (!this.active) return;
    let sum = 0;
    for (const sample of frame) sum += sample * sample;
    this.onLevel(Math.min(1, Math.sqrt(sum / Math.max(1, frame.length)) * 4));
    const rate = this.context?.sampleRate ?? TARGET_SAMPLE_RATE;
    const limit = Math.floor((MAX_RECORDING_SAMPLES * rate) / TARGET_SAMPLE_RATE);
    const room = limit - this.total;
    if (room <= 0) return;
    const take = frame.length > room ? frame.slice(0, room) : new Float32Array(frame);
    this.chunks.push(take);
    this.total += take.length;
    if (this.total >= limit) {
      this.limitReached = true;
      this.onLimit();
    }
  }

  /** Stop, encode, release everything. Resolves with the WAV (base64). */
  async stop(): Promise<RecordingResult> {
    if (!this.active) throw new RecorderError("failed");
    if (this.limitReached) {
      this.release();
      throw new RecorderError("limit");
    }
    const rate = this.context?.sampleRate ?? TARGET_SAMPLE_RATE;
    const chunks = this.chunks;
    const total = this.total;
    // Transfer ownership before releasing the capture graph. release() wipes
    // buffers it owns; these samples still belong to the recording being sent.
    this.chunks = [];
    this.release();
    if (total === 0) throw new RecorderError("empty");
    const joined = new Float32Array(total);
    let pcm: Float32Array | undefined;
    let wav: Uint8Array | undefined;
    try {
      let offset = 0;
      for (const chunk of chunks) {
        joined.set(chunk, offset);
        offset += chunk.length;
      }
      pcm = resample(joined, rate, TARGET_SAMPLE_RATE);
      wav = encodeWav(pcm, TARGET_SAMPLE_RATE);
      return { base64: bytesToBase64(wav), durationSeconds: total / rate };
    } finally {
      for (const chunk of chunks) chunk.fill(0);
      chunks.length = 0;
      joined.fill(0);
      pcm?.fill(0);
      wav?.fill(0);
    }
  }

  /** Abandon the recording: stop tracks and discard audio. */
  cancel(): void {
    this.release();
  }

  private release(): void {
    this.generation++;
    this.active = false;
    if (this.processor) {
      this.processor.onaudioprocess = null;
      this.processor.disconnect();
    }
    this.source?.disconnect();
    this.stream?.getTracks().forEach((track) => track.stop());
    void this.context?.close().catch(() => undefined);
    this.processor = null;
    this.source = null;
    this.stream = null;
    this.context = null;
    for (const chunk of this.chunks) chunk.fill(0);
    this.chunks = [];
    this.total = 0;
  }
}
