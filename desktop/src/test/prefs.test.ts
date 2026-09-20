import { describe, expect, it } from "vitest";
import { DEFAULT_PREFS, loadPrefs, sanitizePrefs, savePrefs } from "../lib/prefs";

describe("preferences allowlist", () => {
  it("drops unknown keys and invalid values", () => {
    const clean = sanitizePrefs({
      sidebarCollapsed: true,
      transcript: "my secret words",
      token: "abc",
      readAloudVoice: "https://evil.example/voice",
      reducedMotion: "yes",
    });
    expect(clean).toEqual({ ...DEFAULT_PREFS, sidebarCollapsed: true });
  });

  it("only ever writes allowlisted keys to storage", () => {
    savePrefs({ ...DEFAULT_PREFS, readAloudVoice: "sam_default" });
    const all = JSON.stringify({ ...window.localStorage });
    expect(Object.keys(window.localStorage)).toEqual(["sam.ui.prefs.v1"]);
    expect(Object.keys(JSON.parse(window.localStorage.getItem("sam.ui.prefs.v1") ?? "{}")).sort()).toEqual([
      "readAloudVoice",
      "reducedMotion",
      "sidebarCollapsed",
    ]);
    expect(all).not.toMatch(/transcript|message|token|confirmation/i);
  });

  it("survives corrupt or unavailable storage", () => {
    window.localStorage.setItem("sam.ui.prefs.v1", "{not json");
    expect(loadPrefs()).toEqual(DEFAULT_PREFS);
  });
});
