import * as ort from "onnxruntime-web/wasm";

/** Silero v6's published streaming ONNX interface. One session, one CPU thread.
 * Its 64-sample context and recurrent state contain no persisted recordings. */
export class SileroModel {
  private state: ort.Tensor = new ort.Tensor("float32", new Float32Array(256), [2, 1, 128]);
  private readonly rate = new ort.Tensor("int64", new BigInt64Array([16000n]));
  private readonly context = new Float32Array(64);

  private constructor(private readonly session: ort.InferenceSession) {}

  static async create(model: string | Uint8Array, wasmPath?: string): Promise<SileroModel> {
    ort.env.wasm.numThreads = 1;
    ort.env.wasm.proxy = false;
    ort.env.logLevel = "error";
    if (wasmPath) ort.env.wasm.wasmPaths = wasmPath;
    const options: ort.InferenceSession.SessionOptions = { executionProviders: ["wasm"] };
    const session = typeof model === "string"
      ? await ort.InferenceSession.create(model, options)
      : await ort.InferenceSession.create(model, options);
    return new SileroModel(session);
  }

  async probability(frame: Float32Array): Promise<number> {
    if (frame.length !== 512 || !frame.every(Number.isFinite)) throw new Error("Invalid speech frame");
    const samples = new Float32Array(576);
    samples.set(this.context);
    samples.set(frame, 64);
    this.context.set(frame.subarray(448));
    const input = new ort.Tensor("float32", samples, [1, 576]);
    try {
      const result = await this.session.run({ input, state: this.state, sr: this.rate });
      const probability = Number(result.output?.data[0]);
      const next = result.stateN;
      result.output?.dispose();
      if (!next || !Number.isFinite(probability) || probability < 0 || probability > 1) {
        next?.dispose();
        throw new Error("Invalid speech prediction");
      }
      this.state.dispose();
      this.state = next;
      return probability;
    } finally {
      input.dispose();
      samples.fill(0);
    }
  }

  reset(): void {
    this.context.fill(0);
    this.state.dispose();
    this.state = new ort.Tensor("float32", new Float32Array(256), [2, 1, 128]);
  }

  async close(): Promise<void> {
    this.context.fill(0);
    this.state.dispose();
    this.rate.dispose();
    await this.session.release();
  }
}
