// @vitest-environment node
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { expect, it } from "vitest";
import { SileroModel } from "../lib/sileroModel";

it("runs the pinned local model, rejects loud non-speech, and resets its recurrent state", async () => {
  const model = await SileroModel.create(
    new Uint8Array(await readFile(resolve("node_modules/@ricky0123/vad-web/dist/silero_vad_v6.onnx"))),
    `${resolve("node_modules/onnxruntime-web/dist")}/`,
  );
  try {
    const first = await model.probability(new Float32Array(512));
    expect(first).toBeLessThan(0.1);
    const noise = Float32Array.from({ length: 512 }, (_, i) => 0.2 * Math.sin(i * 2 * Math.PI * 1000 / 16000));
    for (let i = 0; i < 40; i++) expect(await model.probability(noise)).toBeLessThan(0.5);
    model.reset();
    expect(await model.probability(new Float32Array(512))).toBeCloseTo(first, 6);
    await expect(model.probability(new Float32Array(2048))).rejects.toThrow("Invalid speech frame");
  } finally {
    await model.close();
  }
}, 20_000);
