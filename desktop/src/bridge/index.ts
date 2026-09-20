import type { SamBridge } from "./bridge";
import { tauriBridge } from "./tauri";

/**
 * Chooses the bridge. In a production build `import.meta.env.DEV` is the
 * literal `false`, so the demo bridge import below is dead code and is not
 * part of the bundle (asserted by a static test).
 */
export async function resolveBridge(): Promise<SamBridge> {
  if (import.meta.env.DEV && !isTauriRuntime()) {
    const { demoBridge } = await import("./demo");
    return demoBridge();
  }
  return tauriBridge;
}

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}
