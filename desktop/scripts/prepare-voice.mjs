import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, readFileSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";

const root = new URL("../", import.meta.url);
const file = (path) => fileURLToPath(new URL(path, root));
const model = file("node_modules/@ricky0123/vad-web/dist/silero_vad_v6.onnx");
const digest = createHash("sha256").update(readFileSync(model)).digest("hex");
if (digest !== "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3") {
  throw new Error("Pinned local speech model checksum does not match");
}
const runtime = JSON.parse(readFileSync(file("node_modules/onnxruntime-web/package.json"), "utf8"));
if (runtime.version !== "1.22.0") throw new Error("Unexpected speech runtime version");
mkdirSync(file("public/voice"), { recursive: true });
copyFileSync(model, file("public/voice/silero_vad_v6.onnx"));
for (const name of ["ort-wasm-simd-threaded.wasm", "ort-wasm-simd-threaded.mjs"]) {
  copyFileSync(file(`node_modules/onnxruntime-web/dist/${name}`), file(`public/voice/${name}`));
}
