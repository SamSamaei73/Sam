import { beforeEach, describe, expect, it, vi } from "vitest";
import { BridgeError, SAFE_ERROR_MESSAGES, toBridgeError } from "../bridge/bridge";

const invoke = vi.fn();
// A plain (non-vi.fn) rejection path: vitest's call tracking would otherwise
// report the intentionally-rejected promise as unhandled.
const failure: { value: unknown } = { value: undefined };
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) =>
    failure.value === undefined ? invoke(...args) : Promise.reject(failure.value),
}));

import { tauriBridge } from "../bridge/tauri";

const METHODS = [
  "status",
  "chat",
  "knowledgeList",
  "knowledgeQuery",
  "knowledgeIngest",
  "knowledgeRemove",
  "memorySearch",
  "tools",
  "permissions",
  "revokeGrant",
  "activity",
  "decideConfirmation",
  "voiceUtterance",
  "speak",
  "identityStatus",
  "identityEnrollBegin",
  "identityEnrollSample",
  "identityEnrollComplete",
  "identityEnrollCancel",
  "identityDelete",
  "guestChallenge",
  "guestStart",
  "guestEnd",
  "providersStatus",
  "setProviderPreferences",
].sort();

describe("tauri bridge", () => {
  beforeEach(() => {
    invoke.mockReset();
    failure.value = undefined;
  });

  it("exposes only the fixed, named operations (no generic bridge)", () => {
    expect(Object.keys(tauriBridge).sort()).toEqual(METHODS);
    for (const name of Object.keys(tauriBridge)) {
      expect(name).not.toMatch(/fetch|request|invoke|shell|execute|proxy|readFile|writeFile|url/i);
    }
  });

  it("maps each method to one explicit sam_* command", async () => {
    invoke.mockResolvedValue({});
    await tauriBridge.status();
    await tauriBridge.chat("hi");
    await tauriBridge.knowledgeQuery("q");
    await tauriBridge.decideConfirmation("c1", true);
    const commands = invoke.mock.calls.map((call) => call[0]);
    expect(commands).toEqual(["sam_status", "sam_chat", "sam_knowledge_query", "sam_decide_confirmation"]);
    for (const command of commands) expect(command).toMatch(/^sam_[a-z_]+$/);
  });

  it("never sends principal, scope, risk, permission, endpoint or credentials", async () => {
    invoke.mockResolvedValue({});
    await tauriBridge.chat("hi");
    await tauriBridge.knowledgeIngest({ name: "a.txt", resourceType: "txt", contentBase64: "aGk=" });
    await tauriBridge.knowledgeRemove("r1", "c1");
    await tauriBridge.speak({ text: "hi", voiceProfile: "sam_default" });
    await tauriBridge.voiceUtterance("AAAA", "c1");
    await tauriBridge.decideConfirmation("c1", true);
    await tauriBridge.revokeGrant("g1");
    await tauriBridge.memorySearch("x");
    await tauriBridge.knowledgeQuery("q");
    await tauriBridge.identityEnrollBegin({ stepUp: "s", reEnroll: false });
    await tauriBridge.identityEnrollSample("sess-1", "AAAA");
    await tauriBridge.identityEnrollComplete("sess-1");
    await tauriBridge.identityDelete("s");
    await tauriBridge.guestStart({ challengeId: "c1", audioBase64: "AAAA", stepUp: "s", minutes: 15 });
    await tauriBridge.guestChallenge();
    await tauriBridge.guestEnd();
    await tauriBridge.identityStatus();
    const forbidden = /principal|scope|risk|permission|allow|endpoint|url|token|api_?key|model|voice_?reference|user_?id|owner|speaker/i;
    for (const [, args] of invoke.mock.calls) {
      for (const key of Object.keys((args ?? {}) as object)) expect(key).not.toMatch(forbidden);
    }
  });

  it("provider commands send only trusted preference fields, never an endpoint, key, model or cost class", async () => {
    invoke.mockResolvedValue({});
    await tauriBridge.providersStatus();
    await tauriBridge.setProviderPreferences({
      preferred_provider: "gemini_free",
      allow_free_fallback: true,
      personal_to_free_tier: false,
      private_to_free_tier: true,
      claude_improvement_state: "unknown",
      private_to_claude_when_improvement_enabled: false,
      gemini_attestation: "unknown",
      topic_blocklist: ["x"],
      stepUp: "s",
    });
    await tauriBridge.chat("hi", "auto", "private");
    const providerCalls = invoke.mock.calls.filter((call) => /^sam_models_/.test(String(call[0])));
    expect(providerCalls.map((call) => call[0])).toEqual(["sam_models_status", "sam_models_preferences"]);
    const keys = Object.keys((providerCalls[1]?.[1] ?? {}) as object).sort();
    expect(keys).toEqual([
      "allowFreeFallback",
      "claudeImprovementState",
      "geminiAttestation",
      "personalToFreeTier",
      "preferredProvider",
      "privateToClaudeWhenImprovementEnabled",
      "privateToFreeTier",
      "stepUp",
      "topicBlocklist",
    ]);
    for (const key of keys) expect(key).not.toMatch(/endpoint|url|token|api_?key|model|cost|billing|paid|principal|scope|risk/i);
    expect(invoke.mock.calls.at(-1)).toEqual(["sam_chat", { message: "hi", language: "auto", privacy: "private" }]);
  });

  it("turns any backend failure into a safe, generic error", async () => {
    failure.value = "secret-token abc123 at /Users/me/file.py: traceback";
    const error = await tauriBridge.status().catch((e) => e);
    expect(error).toBeInstanceOf(BridgeError);
    expect(error.message).toBe(SAFE_ERROR_MESSAGES.failed);
    expect(error.message).not.toMatch(/secret|token|Users|traceback/);
  });

  it("keeps known error codes and drops unknown ones", () => {
    expect(toBridgeError("timeout").code).toBe("timeout");
    expect(toBridgeError("guest_mode_active").code).toBe("guest_mode_active");
    expect(toBridgeError("guest_mode_active").message).toMatch(/End Guest Mode/);
    expect(toBridgeError({ any: "thing" }).code).toBe("failed");
  });
});
