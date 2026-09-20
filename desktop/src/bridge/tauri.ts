import { invoke } from "@tauri-apps/api/core";
import { toBridgeError, type SamBridge } from "./bridge";

/**
 * Production bridge. Each call is one *named* Tauri command implemented in
 * Rust with a fixed loopback destination. The webview never sees the backend
 * URL or the bridge token, and cannot choose either.
 */
async function call<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  try {
    return (await invoke<T>(command, args)) as T;
  } catch (error) {
    throw toBridgeError(error);
  }
}

export const tauriBridge: SamBridge = {
  status: () => call("sam_status"),
  chat: (message) => call("sam_chat", { message }),
  knowledgeList: () => call("sam_knowledge_list"),
  knowledgeQuery: (query) => call("sam_knowledge_query", { query }),
  knowledgeIngest: ({ name, resourceType, contentBase64, confirmationId }) =>
    call("sam_knowledge_ingest", {
      name,
      resourceType,
      contentBase64,
      confirmationId: confirmationId ?? null,
    }),
  knowledgeRemove: (resourceId, confirmationId) =>
    call("sam_knowledge_remove", {
      resourceId,
      confirmationId: confirmationId ?? null,
    }),
  memorySearch: (text) => call("sam_memory_search", { text: text ?? null }),
  tools: () => call("sam_tools"),
  permissions: () => call("sam_permissions"),
  revokeGrant: (grantId) => call("sam_revoke_grant", { grantId }),
  activity: () => call("sam_activity"),
  decideConfirmation: (confirmationId, approved, stepUp) =>
    call("sam_decide_confirmation", { confirmationId, approved, stepUp: stepUp ?? null }),
  voiceUtterance: (audioBase64, confirmationId) =>
    call("sam_voice_utterance", { audioBase64, confirmationId: confirmationId ?? null }),
  speak: ({ text, voiceProfile, confirmationId }) =>
    call("sam_speak", { text, voiceProfile, confirmationId: confirmationId ?? null }),
};
