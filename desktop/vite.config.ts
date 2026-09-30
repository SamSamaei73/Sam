import react from "@vitejs/plugin-react";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { defineConfig } from "vitest/config";

// Build identity for About (never on Home): lets the owner see at once which
// build is running. Version, short commit, whether the tree was modified,
// and the build time. Nothing else from the environment is embedded.
function git(args: string[]): string {
  try {
    return execFileSync("git", args, { encoding: "utf8", timeout: 15_000 }).trim();
  } catch {
    return "";
  }
}
const pkg = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf8")) as { version: string };
const commit = /^[0-9a-f]{7,12}$/.test(git(["rev-parse", "--short=10", "HEAD"])) ? git(["rev-parse", "--short=10", "HEAD"]) : "unknown";
const BUILD = {
  version: pkg.version,
  commit,
  modified: process.env.SAM_BUILD_MODIFIED === "1",
  builtAt: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
};

// Fixed dev port so the Tauri shell's devUrl is predictable; loopback only.
export default defineConfig({
  plugins: [react()],
  define: { __SAM_BUILD__: JSON.stringify(BUILD) },
  clearScreen: false,
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
    watch: { ignored: ["**/src-tauri/**"] },
  },
  build: { target: "es2022", sourcemap: false },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    include: ["src/**/*.test.{ts,tsx}"],
    exclude: ["**/node_modules/**", "**/src-tauri/**", "**/dist/**"],
  },
});
