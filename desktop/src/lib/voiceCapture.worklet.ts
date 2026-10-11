export {};

declare const sampleRate: number;
declare class AudioWorkletProcessor {
  readonly port: MessagePort;
}
declare function registerProcessor(name: string, processor: typeof AudioWorkletProcessor): void;

/** Capture only. Inference runs in a separate worker, never on the audio thread. */
class VoiceCapture extends AudioWorkletProcessor {
  private frame = new Float32Array(512);
  private used = 0;
  private epoch = 0;
  private active = false;
  private outstanding = 0;

  constructor() {
    super();
    this.port.onmessage = ({ data }: MessageEvent<{ type: string; epoch: number }>) => {
      if (data.type === "ack") {
        if (data.epoch === this.epoch) this.outstanding = Math.max(0, this.outstanding - 1);
        return;
      }
      this.frame.fill(0);
      this.used = 0;
      this.outstanding = 0;
      this.epoch = data.epoch;
      this.active = data.type === "resume" && sampleRate === 16_000;
    };
  }

  process(inputs: Float32Array[][]): boolean {
    const input = inputs[0]?.[0];
    if (!this.active || !input) return true;
    for (const sample of input) {
      this.frame[this.used++] = sample;
      if (this.used === this.frame.length) {
        // At most 512 ms of audio can await processing, even if the UI stalls.
        // An overrun fails visibly; it must never silently drop syllables.
        if (this.outstanding >= 16) {
          this.active = false;
          this.frame.fill(0);
          this.used = 0;
          this.port.postMessage({ type: "overrun", epoch: this.epoch });
          break;
        }
        this.port.postMessage({ type: "frame", epoch: this.epoch, frame: this.frame }, [this.frame.buffer]);
        this.outstanding++;
        this.frame = new Float32Array(512);
        this.used = 0;
      }
    }
    return true; // output stays silent
  }
}

registerProcessor("sam-voice-capture", VoiceCapture);
