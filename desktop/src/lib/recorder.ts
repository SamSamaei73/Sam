import {
  MAX_RECORDING_SAMPLES,
  TARGET_SAMPLE_RATE,
  bytesToBase64,
  encodeWav,
  resample,
} from "./wav";

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

export type RecorderErrorCode = "unsupported" | "denied" | "failed" | "empty";

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
  createContext: () => AudioContext;
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
    createContext: () => new Ctor(),
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

  constructor(
    private readonly env: AudioEnvironment | null = browserAudioEnvironment(),
    private readonly onLimit: () => void = () => undefined,
  ) {}

  get isRecording(): boolean {
    return this.active;
  }

  async start(): Promise<void> {
    if (this.active) return;
    if (!this.env) throw new RecorderError("unsupported");
    let stream: MediaStream;
    try {
      stream = await this.env.getUserMedia({ audio: true, video: false });
    } catch (error) {
      const name = (error as { name?: string } | null)?.name ?? "";
      throw new RecorderError(
        name === "NotAllowedError" || name === "SecurityError" ? "denied" : "failed",
      );
    }
    try {
      const context = this.env.createContext();
      const source = context.createMediaStreamSource(stream);
      const processor = context.createScriptProcessor(4096, 1, 1);
      this.stream = stream;
      this.context = context;
      this.source = source;
      this.processor = processor;
      this.chunks = [];
      this.total = 0;
      this.active = true;
      processor.onaudioprocess = (event) => this.capture(event.inputBuffer.getChannelData(0));
      source.connect(processor);
      // Connected only so the node runs; its output buffer is silent.
      processor.connect(context.destination);
    } catch {
      this.release();
      throw new RecorderError("failed");
    }
  }

  private capture(frame: Float32Array): void {
    if (!this.active) return;
    const rate = this.context?.sampleRate ?? TARGET_SAMPLE_RATE;
    const limit = Math.floor((MAX_RECORDING_SAMPLES * rate) / TARGET_SAMPLE_RATE);
    const room = limit - this.total;
    if (room <= 0) return;
    const take = frame.length > room ? frame.slice(0, room) : new Float32Array(frame);
    this.chunks.push(take);
    this.total += take.length;
    if (this.total >= limit) {
      this.onLimit();
    }
  }

  /** Stop, encode, release everything. Resolves with the WAV (base64). */
  async stop(): Promise<RecordingResult> {
    if (!this.active) throw new RecorderError("failed");
    const rate = this.context?.sampleRate ?? TARGET_SAMPLE_RATE;
    const chunks = this.chunks;
    const total = this.total;
    this.release();
    if (total === 0) throw new RecorderError("empty");
    const joined = new Float32Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      joined.set(chunk, offset);
      offset += chunk.length;
    }
    chunks.length = 0;
    const wav = encodeWav(resample(joined, rate, TARGET_SAMPLE_RATE), TARGET_SAMPLE_RATE);
    joined.fill(0);
    return { base64: bytesToBase64(wav), durationSeconds: total / rate };
  }

  /** Abandon the recording: stop tracks and discard audio. */
  cancel(): void {
    this.release();
  }

  private release(): void {
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
