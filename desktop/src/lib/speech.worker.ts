import { SileroModel } from "./sileroModel";

type Request =
  | { type: "init"; basePath: string }
  | { type: "frame"; epoch: number; frame: Float32Array }
  | { type: "reset" };

let model: SileroModel | undefined;
let serial: Promise<void> = Promise.resolve();

// There is only one inference in flight. Reset is ordered after that inference
// and before the next turn, so its recurrent state cannot leak across turns.
self.onmessage = (event: MessageEvent<Request>) => {
  const request = event.data;
  serial = serial.then(async () => {
    if (request.type === "init") {
      const base = new URL(request.basePath);
      if (base.origin !== self.location.origin || !base.pathname.endsWith("/voice/")) {
        throw new Error("Invalid voice asset location");
      }
      model = await SileroModel.create(new URL("silero_vad_v6.onnx", base).href, base.href);
      self.postMessage({ type: "ready" });
    } else if (request.type === "reset") {
      model?.reset();
    } else {
      if (!model) throw new Error("Speech detector is not ready");
      try {
        const probability = await model.probability(request.frame);
        self.postMessage({ type: "frame", epoch: request.epoch, frame: request.frame, probability }, { transfer: [request.frame.buffer] });
      } finally {
        if (request.frame.byteLength) request.frame.fill(0);
      }
    }
  }).catch(() => {
    if (request.type === "frame" && request.frame.byteLength) request.frame.fill(0);
    self.postMessage({ type: "failed" });
  });
};
