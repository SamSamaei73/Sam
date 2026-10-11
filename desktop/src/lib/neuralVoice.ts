import workletUrl from "./voiceCapture.worklet.ts?worker&url";

export interface SpeechInput {
  pause(): void;
  resume(): void;
  stop(): void;
}

export type SpeechInputFactory = (
  context: AudioContext,
  stream: MediaStream,
  onFrame: (frame: Float32Array, probability: number) => void,
  onFailure: () => void,
) => Promise<SpeechInput>;

/** Bounded, generation-aware queue between AudioWorklet and neural worker. */
export class SpeechFrameQueue {
  private epoch = 0;
  private active = false;
  private busy = false;
  private frames: Float32Array[] = [];

  constructor(
    private readonly send: (frame: Float32Array, epoch: number) => void,
    private readonly deliver: (frame: Float32Array, probability: number) => void,
    private readonly control: (type: "pause" | "resume" | "ack", epoch: number) => void,
    private readonly reset: () => void,
    private readonly failure: () => void,
  ) {}

  pause(): void {
    this.active = false;
    this.epoch++;
    for (const frame of this.frames) frame.fill(0);
    this.frames = [];
    this.control("pause", this.epoch);
    this.reset();
  }

  resume(): void {
    if (this.active) return;
    this.active = true;
    this.control("resume", this.epoch);
  }

  capture(frame: Float32Array, epoch: number): void {
    if (!this.active || epoch !== this.epoch) { frame.fill(0); return; }
    if (frame.length !== 512 || this.frames.length >= 16) {
      frame.fill(0);
      this.pause();
      this.failure();
      return;
    }
    this.frames.push(frame);
    this.pump();
  }

  overrun(epoch: number): void {
    if (!this.active || epoch !== this.epoch) return;
    this.pause();
    this.failure();
  }

  complete(frame: Float32Array, probability: number, epoch: number): void {
    this.busy = false;
    try {
      if (this.active && epoch === this.epoch) {
        this.deliver(frame, probability);
        this.control("ack", epoch);
      }
    } finally {
      frame.fill(0);
      this.pump();
    }
  }

  private pump(): void {
    if (!this.active || this.busy) return;
    const frame = this.frames.shift();
    if (!frame) return;
    this.busy = true;
    this.send(frame, this.epoch);
  }
}

export const createNeuralSpeechInput: SpeechInputFactory = async (context, stream, onFrame, onFailure) => {
  if (!context.audioWorklet || context.sampleRate !== 16_000) throw new Error("Neural microphone unavailable");
  const worker = new Worker(new URL("./speech.worker.ts", import.meta.url), { type: "module", name: "sam-local-vad" });
  let node: AudioWorkletNode | undefined;
  let source: MediaStreamAudioSourceNode | undefined;
  let stopped = false;
  let ready = false;
  let rejectInitialization: (reason: Error) => void = () => undefined;
  let resolveReady: () => void = () => undefined;
  const modelReady = new Promise<void>((resolve) => { resolveReady = resolve; });
  const initializationFailed = new Promise<never>((_, reject) => { rejectInitialization = reject; });
  const queue = new SpeechFrameQueue(
    (frame, epoch) => worker.postMessage({ type: "frame", frame, epoch }, [frame.buffer]),
    onFrame,
    (type, epoch) => node?.port.postMessage({ type, epoch }),
    () => worker.postMessage({ type: "reset" }),
    () => fail(),
  );
  const stop = () => {
    if (stopped) return;
    stopped = true;
    queue.pause();
    source?.disconnect();
    node?.disconnect();
    node?.port.close();
    worker.terminate();
  };
  const fail = () => {
    if (stopped) return;
    rejectInitialization(new Error("Local speech detector failed"));
    stop();
    if (ready) onFailure();
  };
  worker.onerror = fail;
  worker.onmessageerror = fail;
  worker.onmessage = ({ data }: MessageEvent<{ type: string; frame?: Float32Array; epoch?: number; probability?: number }>) => {
    if (stopped) { data.frame?.fill(0); return; }
    if (data.type === "ready") resolveReady();
    else if (data.type === "failed") fail();
    else if (data.type === "frame" && data.frame && data.epoch !== undefined && data.probability !== undefined) {
      queue.complete(data.frame, data.probability, data.epoch);
    }
  };
  const timer = window.setTimeout(fail, 15_000);
  try {
    const basePath = new URL(`${import.meta.env.BASE_URL}voice/`, window.location.href).href;
    worker.postMessage({ type: "init", basePath });
    await Promise.race([(async () => {
      await Promise.all([modelReady, context.audioWorklet.addModule(workletUrl)]);
      if (stopped) throw new Error("Local speech detector stopped");
      node = new AudioWorkletNode(context, "sam-voice-capture", { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1 });
      node.onprocessorerror = fail;
      node.port.onmessage = ({ data }: MessageEvent<{ type: string; frame?: Float32Array; epoch: number }>) => {
        if (data.type === "overrun") queue.overrun(data.epoch);
        else if (data.type === "frame" && data.frame) queue.capture(data.frame, data.epoch);
      };
      source = context.createMediaStreamSource(stream);
      source.connect(node);
      node.connect(context.destination);
      await context.resume();
      if (stopped) throw new Error("Local speech detector stopped");
    })(), initializationFailed]);
    ready = true;
    queue.resume();
    return {
      pause: () => { if (!stopped) queue.pause(); },
      resume: () => { if (!stopped) queue.resume(); },
      stop,
    };
  } catch (error) {
    stop();
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
};
