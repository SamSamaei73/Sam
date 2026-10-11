import { RecorderError, browserAudioEnvironment, voiceCaptureConstraints, type AudioEnvironment } from "./recorder";
import { SLEEP_TIMING, VoiceSegmenter, type SegmentEnd, type VadTiming } from "./vad";
import { TARGET_SAMPLE_RATE, bytesToBase64, encodeWav, resample } from "./wav";
import type { SpeechInput } from "./neuralVoice";

/**
 * Hands-free microphone for voice activation. It runs only while the owner
 * has voice activation ON and Home is open, and it never records a stream:
 *
 * - every frame goes to the local voice-activity detector and is dropped;
 * - only a finished SPEECH segment (seconds, not minutes) is encoded, in
 *   memory, and handed to the caller, which sends it to Sam's LOCAL backend;
 * - while paused (Sam thinking or speaking) frames are discarded unseen;
 * - `stop()` stops every track, closes the audio graph and wipes buffers.
 *
 * Nothing is written to disk, browser storage or a URL.
 */
export class Listener {
  private stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private input: SpeechInput | null = null;
  private segmenter: VoiceSegmenter | null = null;
  private paused = false;
  private generation = 0;
  private timing: VadTiming = SLEEP_TIMING;

  constructor(
    private readonly env: AudioEnvironment | null = browserAudioEnvironment(),
    private readonly onSegment: (wavBase64: string, seconds: number, endedBy: SegmentEnd, voicedSeconds: number) => void,
    private readonly onLevel: (level: number) => void = () => undefined,
    private readonly onSpeech: (active: boolean) => void = () => undefined,
    private readonly onFailure: () => void = () => undefined,
  ) {}

  get isActive(): boolean {
    return this.input !== null;
  }

  get isSpeaking(): boolean {
    return this.segmenter?.hasSpeechActivity ?? false;
  }

  async start(timing: VadTiming = SLEEP_TIMING): Promise<void> {
    if (this.isActive) return;
    if (!this.env?.createSpeechInput) throw new RecorderError("unsupported");
    const generation = ++this.generation;
    this.timing = timing;
    let stream: MediaStream;
    try {
      stream = await this.env.getUserMedia(voiceCaptureConstraints());
    } catch (error) {
      const name = (error as { name?: string } | null)?.name ?? "";
      throw new RecorderError(name === "NotAllowedError" || name === "SecurityError" ? "denied" : "failed");
    }
    if (generation !== this.generation) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    try {
      // Own the granted stream before audio graph creation can fail, so the
      // common failure path always closes the microphone.
      this.stream = stream;
      // Browser-native sample-rate conversion provides anti-aliased 16 kHz
      // input for the streaming model, independently of hardware sample rate.
      const context = this.env.createContext(TARGET_SAMPLE_RATE);
      this.context = context;
      const rate = context.sampleRate;
      this.segmenter = new VoiceSegmenter(
        rate,
        (samples, endedBy, voicedSeconds) => this.emit(samples, rate, endedBy, voicedSeconds),
        this.timing,
        (active) => this.onSpeech(active),
      );
      const input = await this.env.createSpeechInput(context, stream,
        (frame, probability) => { if (generation === this.generation) this.frame(frame, probability); },
        () => {
          if (generation !== this.generation) return;
          this.stop();
          this.onFailure();
        },
      );
      if (generation !== this.generation) {
        input.stop();
        stream.getTracks().forEach((track) => track.stop());
        void context.close().catch(() => undefined);
        return;
      }
      this.input = input;
      if (this.paused) input.pause();
    } catch {
      if (generation !== this.generation) return;
      this.stop();
      throw new RecorderError("failed");
    }
  }

  /** Conversation turns wait longer for the end of speech than the wake word. */
  setTiming(timing: VadTiming): void {
    this.timing = timing;
    this.segmenter?.setTiming(timing);
  }

  /** Stop hearing (Sam is thinking or speaking): frames are discarded unseen. */
  pause(): void {
    this.paused = true;
    this.input?.pause();
    this.segmenter?.reset();
    this.onLevel(0);
  }

  resume({ preserveBufferedAudio = false }: { preserveBufferedAudio?: boolean } = {}): void {
    if (!preserveBufferedAudio) this.segmenter?.reset();
    this.paused = false;
    this.input?.resume();
  }

  stop(): void {
    this.generation++;
    this.input?.stop();
    this.stream?.getTracks().forEach((track) => track.stop());
    void this.context?.close().catch(() => undefined);
    this.segmenter?.reset();
    this.segmenter = null;
    this.input = null;
    this.stream = null;
    this.context = null;
    this.paused = false;
    this.onLevel(0);
  }

  private frame(frame: Float32Array, probability: number): void {
    if (this.paused || !this.segmenter) return;
    const level = this.segmenter.push(frame, probability);
    this.onLevel(Math.min(1, level * 4));
  }

  private emit(samples: Float32Array, rate: number, endedBy: SegmentEnd, voicedSeconds: number): void {
    const pcm = resample(samples, rate, TARGET_SAMPLE_RATE);
    const wav = encodeWav(pcm, TARGET_SAMPLE_RATE);
    const seconds = pcm.length / TARGET_SAMPLE_RATE;
    pcm.fill(0);
    const base64 = bytesToBase64(wav);
    wav.fill(0);
    this.onSegment(base64, seconds, endedBy, voicedSeconds);
  }
}
