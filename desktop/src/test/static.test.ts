import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "..");
const ROOT = join(SRC, "..");

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, out);
    else out.push(full);
  }
  return out;
}

const productionSources = walk(SRC).filter(
  (f) => /\.(ts|tsx|css)$/.test(f) && !f.includes(`${join("src", "test")}`) && !f.endsWith("demo.ts"),
);
const read = (f: string) => readFileSync(f, "utf8");
/** Source with comments removed, so prose about what is forbidden isn't flagged. */
const code = (f: string) => read(f).replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

describe("static security checks (production sources)", () => {
  it("has production sources to scan", () => {
    expect(productionSources.length).toBeGreaterThan(20);
  });

  it.each([
    ["dangerouslySetInnerHTML", /dangerouslySetInnerHTML/],
    ["innerHTML assignment", /\.(innerHTML|outerHTML)\s*=/],
    ["insertAdjacentHTML", /insertAdjacentHTML/],
    ["eval", /\beval\s*\(/],
    ["new Function", /new\s+Function\s*\(/],
    ["document.write", /document\.write/],
    ["fetch", /\bfetch\s*\(/],
    ["XMLHttpRequest", /XMLHttpRequest/],
    ["WebSocket / EventSource", /\b(WebSocket|EventSource)\b/],
    ["sendBeacon", /sendBeacon/],
    ["remote URLs", /https?:\/\/(?!www\.w3\.org)/],
    ["shell/process/fs plugins", /@tauri-apps\/plugin-(shell|fs|process|clipboard|global-shortcut|http)/],
    ["generic Tauri invoke names", /invoke\(\s*[a-z]/],
  ])("has no %s", (_name, pattern) => {
    // tauri.ts is the one module allowed to call `invoke(` (with a literal sam_* command).
    const exempt = (f: string) => _name === "generic Tauri invoke names" && f.endsWith("tauri.ts");
    const offenders = productionSources.filter((f) => pattern.test(code(f)) && !exempt(f));
    expect(offenders.map((f) => relative(ROOT, f))).toEqual([]);
  });

  it("only the Tauri bridge calls invoke, and only with literal sam_* commands", () => {
    const users = productionSources.filter((f) => /@tauri-apps\/api/.test(read(f)));
    expect(users.map((f) => relative(ROOT, f))).toEqual(["src/bridge/tauri.ts"]);
    const text = read(join(SRC, "bridge", "tauri.ts"));
    const commands = [...text.matchAll(/call(?:<[^>]*>)?\(\s*"([^"]+)"/g)].map((m) => m[1]);
    expect(commands.length).toBe(25);
    for (const command of commands) expect(command).toMatch(/^sam_[a-z_]+$/);
  });

  it("browser storage is used only by the preference allowlist module", () => {
    const users = productionSources.filter((f) => /(localStorage|sessionStorage|indexedDB)/.test(code(f)));
    expect(users.map((f) => relative(ROOT, f))).toEqual(["src/lib/prefs.ts"]);
  });

  it("the prefs module can only store the three allowlisted keys", () => {
    const text = code(join(SRC, "lib", "prefs.ts"));
    expect(text).toMatch(/sidebarCollapsed/);
    expect(text).not.toMatch(/transcript|message|token|confirmation/i);
  });

  it("request-shaped bridge methods carry no authority parameters", () => {
    const text = code(join(SRC, "bridge", "bridge.ts"));
    const iface = text.slice(text.indexOf("export interface SamBridge"), text.indexOf("export class BridgeError"));
    expect(iface).not.toMatch(/principal|scope|risk|allow|endpoint|url|token|apiKey|model|voiceReference/i);
  });

  it("the demo bridge is only reachable behind import.meta.env.DEV", () => {
    const importers = productionSources.filter((f) => /["']\.\/demo["']|bridge\/demo/.test(read(f)));
    expect(importers.map((f) => relative(ROOT, f))).toEqual(["src/bridge/index.ts"]);
    expect(read(join(SRC, "bridge", "index.ts"))).toMatch(/if \(import\.meta\.env\.DEV/);
  });

  it("index.html and CSS load nothing remotely", () => {
    expect(read(join(ROOT, "index.html"))).not.toMatch(/https?:\/\//);
    for (const css of productionSources.filter((f) => f.endsWith(".css"))) {
      expect(read(css)).not.toMatch(/@import|url\(\s*["']?https?:/);
    }
  });

  it("no production code logs to the console (no accidental leakage of secrets or transcripts)", () => {
    const offenders = productionSources
      .filter((f) => /\.(ts|tsx)$/.test(f))
      .filter((f) => /\bconsole\.(log|info|warn|error|debug|trace)\(/.test(code(f)));
    expect(offenders.map((f) => relative(ROOT, f))).toEqual([]);
  });

  it("password fields exist only where a step-up secret is legitimately typed", () => {
    const users = productionSources
      .filter((f) => f.endsWith(".tsx") && /type="password"/.test(code(f)))
      .map((f) => relative(ROOT, f))
      .sort();
    expect(users).toEqual([
      "src/components/ConfirmationDialog.tsx",
      "src/components/IdentityDialogs.tsx",
      // Phase 13: the step-up secret that must accompany LOOSENING a privacy setting.
      "src/components/ModelSettings.tsx",
    ]);
  });

  it("the UI never asserts an owner identity or a principal in a request", () => {
    for (const file of productionSources.filter((f) => /components|views|bridge/.test(f))) {
      const text = code(file);
      expect(text, relative(ROOT, file)).not.toMatch(/\bisOwner\b|\bowner\s*:\s*true\b|\bprincipal\s*:|speaker\s*:\s*["']owner["']\s*[,}]/);
    }
  });

  it("recordings are never persisted or turned into files/URLs by the recorder or dialogs", () => {
    for (const name of ["lib/recorder.ts", "components/IdentityDialogs.tsx", "components/VoiceRecorder.tsx"]) {
      const text = code(join(SRC, name));
      expect(text, name).not.toMatch(/MediaRecorder|createObjectURL|indexedDB|FileSystem|showSaveFilePicker|\.download\s*=/);
    }
  });

  it("the interface language strings are static (no runtime translation calls)", () => {
    const text = code(join(SRC, "i18n", "strings.ts"));
    expect(text).not.toMatch(/fetch|import\(|Intl\.Translator|translate\(.*await/);
  });
});
