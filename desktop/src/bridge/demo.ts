/**
 * DEV-ONLY demo bridge, used when the app runs in a plain browser under
 * `vite dev` (no Tauri runtime) so the interface can be exercised visually.
 * It is dynamically imported behind `import.meta.env.DEV` and is absent from
 * production bundles. It simulates responses only; it is not a policy engine
 * and never claims to enforce permissions.
 */
import type { SamBridge } from "./bridge";
import type {
  ActivityItem,
  Challenge,
  GrantInfo,
  KnowledgeHit,
  OperationResult,
  ResourceInfo,
} from "./types";

const empty = {
  reason_code: null,
  message: null,
  reference_id: null,
  challenge: null,
} as const;

const wait = (ms = 120) => new Promise((resolve) => setTimeout(resolve, ms));
const iso = () => new Date().toISOString();

export function demoBridge(): SamBridge {
  const resources: ResourceInfo[] = [
    {
      resource_id: "res-demo-1",
      name: "Quarterly planning notes.md",
      resource_type: "markdown",
      size_bytes: 4820,
      chunk_count: 6,
      created_at: iso(),
    },
    {
      resource_id: "res-demo-2",
      name: "Research summary.pdf",
      resource_type: "pdf",
      size_bytes: 184_320,
      chunk_count: 21,
      created_at: iso(),
    },
  ];
  const grants: GrantInfo[] = [
    ["knowledge", "write", "default:ingest"],
    ["knowledge", "read", "default:list"],
    ["knowledge", "read", "default:retrieve"],
    ["knowledge", "delete", "default"],
    ["speech_synthesis", "send", "fish-audio/sam_default"],
  ].map(([resource, action, scope], index) => ({
    grant_id: `bootstrap-0${index + 1}`,
    resource: resource ?? "",
    action: action ?? "",
    scope: scope ?? "",
    status: "active" as const,
    expires_at: null,
    requires_confirmation: action === "delete",
    origin: "desktop_bootstrap",
  }));
  const activity: ActivityItem[] = [];
  const pending = new Map<string, Challenge>();
  const approved = new Set<string>();

  const log = (kind: string, label: string, outcome: string, risk: string | null = null) =>
    activity.unshift({ timestamp: iso(), kind, label, outcome, risk });

  return {
    async status() {
      await wait(60);
      return {
        backend: { status: "ok", service: "Sam", environment: "demo" },
        agent: "configured",
        knowledge: "available",
        memory: "available",
        tools: "foundation_ready",
        tool_count: 0,
        server_count: 0,
        voice_input: "not_configured",
        speech_output: "configured",
        speech_profiles: [{ profile_id: "sam_default" }],
        computer_control: "not_configured",
        coding_agent: "not_configured",
        conversation_history: "session_local",
        memory_storage: "in_process",
        principal_label: "local-user",
      };
    },
    async chat(message) {
      await wait(500);
      log("chat", "Sam request", "completed");
      return {
        ...empty,
        status: "ok",
        reference_id: "demo-exec",
        reply: `This is a demo reply to: "${message.slice(0, 80)}". In the real app the answer comes from Sam's agent core through the local bridge.`,
      };
    },
    async knowledgeList() {
      await wait();
      return { ...empty, status: "ok", resources: [...resources] };
    },
    async knowledgeQuery(query) {
      await wait(250);
      log("knowledge", "Knowledge searched", "allowed", "low");
      const hits: KnowledgeHit[] = resources.slice(0, 2).map((r, index) => ({
        resource_id: r.resource_id,
        resource_name: r.name,
        resource_type: r.resource_type,
        chunk_id: `chunk-${index}`,
        score: 0.82 - index * 0.17,
        snippet: `…matching passage about "${query}" from ${r.name}…`,
        location: {
          page_number: r.resource_type === "pdf" ? 4 : null,
          section_title: index === 0 ? "Q3 priorities" : null,
          paragraph_index: 2,
          character_start: 120,
          character_end: 388,
        },
      }));
      return { ...empty, status: "ok", hits };
    },
    async knowledgeIngest({ name, resourceType }) {
      await wait(400);
      const resource: ResourceInfo = {
        resource_id: `res-${Date.now()}`,
        name,
        resource_type: resourceType,
        size_bytes: 2048,
        chunk_count: 3,
        created_at: iso(),
      };
      resources.unshift(resource);
      log("knowledge", "Knowledge ingest", "success");
      return { ...empty, status: "ok", resource, duplicate_of: null };
    },
    async knowledgeRemove(resourceId, confirmationId) {
      await wait(200);
      if (confirmationId && approved.has(confirmationId)) {
        approved.delete(confirmationId);
        const index = resources.findIndex((r) => r.resource_id === resourceId);
        if (index >= 0) resources.splice(index, 1);
        log("knowledge", "Knowledge removal", "allowed", "high");
        return { ...empty, status: "ok" } satisfies OperationResult;
      }
      const target = resources.find((r) => r.resource_id === resourceId);
      const challenge: Challenge = {
        confirmation_id: `conf-${Date.now()}`,
        action: "delete",
        resource: "knowledge",
        scope: "default",
        risk: "high",
        target: target?.name ?? resourceId,
        reason: "Removing a document from Knowledge can't be undone.",
        expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
      };
      pending.set(challenge.confirmation_id, challenge);
      return {
        ...empty,
        status: "confirmation_required",
        reason_code: "confirmation_required",
        message: "This needs your confirmation.",
        challenge,
      };
    },
    async memorySearch() {
      await wait();
      return { ...empty, status: "ok", items: [], working: [] };
    },
    async tools() {
      await wait();
      return { state: "foundation_ready", servers: [], tools: [] };
    },
    async permissions() {
      await wait();
      return { grants: grants.filter((g) => g.status === "active").concat(grants.filter((g) => g.status !== "active")) };
    },
    async revokeGrant(grantId) {
      await wait();
      const grant = grants.find((g) => g.grant_id === grantId);
      if (!grant) return { ...empty, status: "rejected", reason_code: "resource_not_found", message: "That item no longer exists." };
      grant.status = "revoked";
      log("permission", "Grant revoked", "revoked");
      return { ...empty, status: "ok" };
    },
    async activity() {
      await wait(60);
      return { scope: "current_session", items: [...activity] };
    },
    async decideConfirmation(confirmationId, isApproved) {
      await wait(100);
      if (!pending.delete(confirmationId)) throw new Error("failed");
      if (isApproved) approved.add(confirmationId);
      log("permission", "Confirmation answered", isApproved ? "approved" : "denied");
      return { status: isApproved ? "approved" : "denied", confirmation_id: confirmationId };
    },
    async voiceUtterance() {
      await wait();
      return {
        ...empty,
        status: "not_configured",
        reason_code: "not_configured",
        message: "That capability isn't configured.",
        transcript: null,
        forwarded_to_agent: false,
        reply: null,
      };
    },
    async speak() {
      await wait();
      return {
        ...empty,
        status: "not_configured",
        reason_code: "not_configured",
        message: "The demo has no speech provider.",
        audio_base64: null,
        audio_format: null,
        byte_length: null,
      };
    },
  };
}
