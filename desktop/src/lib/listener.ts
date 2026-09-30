import { RecorderError, browserAudioEnvironment, type AudioEnvironment } from "./recorder";
import { SLEEP_TIMING, VoiceSegmenter, type VadTiming } from "./vad";
import { TARGET_SAMPLE_RATE, bytesToBase64, encodeWav, resample } from "./wav";

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
  private source: MediaStreamAudioSourceNode | null = null;
  private processor: ScriptProcessorNode | null = null;
  private segmenter: VoiceSegmenter | null = null;
  private paused = false;
  private timing: VadTiming = SLEEP_TIMING;

  constructor(
    private readonly env: AudioEnvironment | null = browserAudioEnvironment(),
    private readonly onSegment: (wavBase64: string, seconds: number) => void,
    private readonly onLevel: (level: number) => void = () => undefined,
    private readonly onSpeech: (active: boolean) => void = () => undefined,
  ) {}

  get isActive(): boolean {
    return this.processor !== null;
  }

  async start(timing: VadTiming = SLEEP_TIMING): Promise<void> {
    if (this.isActive) return;
    if (!this.env) throw new RecorderError("unsupported");
    this.timing = timing;
    let stream: MediaStream;
    try {
      stream = await this.env.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
        video: false,
      });
    } catch (error) {
      const name = (error as { name?: string } | null)?.name ?? "";
      throw new RecorderError(name === "NotAllowedError" || name === "SecurityError" ? "denied" : "failed");
    }
    try {
      const context = this.env.createContext();
      const rate = context.sampleRate;
      const source = context.createMediaStreamSource(stream);
      const processor = context.createScriptProcessor(2048, 1, 1);
      this.segmenter = new VoiceSegmenter(
        rate,
        (samples) => this.emit(samples, rate),
        this.timing,
        (active) => this.onSpeech(active),
      );
      this.stream = stream;
      this.context = context;
      this.source = source;
      this.processor = processor;
      processor.onaudioprocess = (event) => this.frame(event.inputBuffer.getChannelData(0));
      source.connect(processor);
      processor.connect(context.destination); // silent output; keeps the node running
    } catch {
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
    this.segmenter?.reset();
    this.onLevel(0);
  }

  resume(): void {
    this.segmenter?.reset();
    this.paused = false;
  }

  stop(): void {
    if (this.processor) {
      this.processor.onaudioprocess = null;
      this.processor.disconnect();
    }
    this.source?.disconnect();
    this.stream?.getTracks().forEach((track) => track.stop());
    void this.context?.close().catch(() => undefined);
    this.segmenter?.reset();
    this.segmenter = null;
    this.processor = null;
    this.source = null;
    this.stream = null;
    this.context = null;
    this.paused = false;
    this.onLevel(0);
  }

  private frame(frame: Float32Array): void {
    if (this.paused || !this.segmenter) return;
    const level = this.segmenter.push(frame);
    this.onLevel(Math.min(1, level * 4));
  }

  private emit(samples: Float32Array, rate: number): void {
    const pcm = resample(samples, rate, TARGET_SAMPLE_RATE);
    const wav = encodeWav(pcm, TARGET_SAMPLE_RATE);
    const seconds = pcm.length / TARGET_SAMPLE_RATE;
    pcm.fill(0);
    const base64 = bytesToBase64(wav);
    wav.fill(0);
    this.onSegment(base64, seconds);
  }
}
