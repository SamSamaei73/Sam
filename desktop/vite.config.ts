import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Fixed dev port so the Tauri shell's devUrl is predictable; loopback only.
export default defineConfig({
  plugins: [react()],
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
