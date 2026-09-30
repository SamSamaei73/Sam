/**
 * DEV-ONLY demo bridge, used when the app runs in a plain browser under
 * `vite dev` (no Tauri runtime) so the interface can be exercised visually.
 * It is dynamically imported behind `import.meta.env.DEV` and is absent from
 * production bundles. It simulates responses only; it is not a policy engine
 * and never claims to enforce permissions.
 */
import { BridgeError, type SamBridge } from "./bridge";
import { detectLanguage, directionFor } from "../lib/language";
import { bytesToBase64, encodeWav } from "../lib/wav";
import type {
  ActivityItem,
  IdentityStatus,
  VoiceModelsInfo,
  VoiceSetupState,
  ProviderPreferences,
  ProvidersStatus,
  ProviderStatus,
  Challenge,
  GrantInfo,
  KnowledgeHit,
  OperationResult,
  CareerOpportunity,
  ProClaim,
  ProfessionalProfile,
  ProNotification,
  ProSource,
  ProTask,
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

/**
 * DEV-only visual QA fixtures, chosen by `?fixture=` in the dev server URL:
 * voice (voice input on; a slow reply; audible synthesized speech), guest,
 * attention (items waiting for the owner) and degraded (no language model).
 */
type Fixture =
  | "voice"
  | "handsfree"
  | "stranger"
  | "guest"
  | "attention"
  | "degraded"
  | "setup"
  | "firstrun"
  | null;
const FIXTURES = ["voice", "handsfree", "stranger", "guest", "attention", "degraded", "setup", "firstrun"] as const;
function fixtureFromUrl(): Fixture {
  const value = new URLSearchParams(globalThis.location?.search ?? "").get("fixture");
  return (FIXTURES as readonly string[]).includes(value ?? "") ? (value as Fixture) : null;
}

/** A soft 3-second tone, so "speaking" is real audio playback in the demo. */
function demoTone(): string {
  const rate = 16_000;
  const samples = new Float32Array(rate * 3);
  for (let i = 0; i < samples.length; i++) {
    const t = i / rate;
    samples[i] = 0.12 * Math.sin(2 * Math.PI * 220 * t) * (0.6 + 0.4 * Math.sin(2 * Math.PI * 3 * t));
  }
  return bytesToBase64(encodeWav(samples, rate));
}

export function demoBridge(): SamBridge {
  const fixture = fixtureFromUrl();
  // Hands-free demo: the demo has no recognizer, so ANY speech segment counts
  // as the wake word (visual QA only; the real backend checks for "Sam").
  const handsFree = fixture === "handsfree" || fixture === "stranger";
  let activation: "on" | "off" = handsFree ? "on" : "off";
  const models = {
    prefs: {
      preferred_provider: null,
      allow_free_fallback: true,
      personal_to_free_tier: false,
      private_to_free_tier: false,
      claude_improvement_state: "unknown",
      private_to_claude_when_improvement_enabled: false,
      gemini_attestation: "unknown",
    } as ProviderPreferences,
    blocklist: [] as string[],
  };
  const providerRows = (): ProviderStatus[] => [
    {
      provider_id: "claude_subscription",
      display_name: "Claude (subscription)",
      state: "available",
      enabled: true,
      cost_class: "subscription_included",
      external: true,
      free_tier_data_use: false,
      note: "Uses your Claude subscription through the local Claude Code app, not Anthropic API billing.",
      models: ["subscription_default"],
    },
    {
      provider_id: "gemini_free",
      display_name: "Gemini (Free Tier)",
      state: "not_configured",
      enabled: false,
      cost_class: "free_tier",
      external: true,
      free_tier_data_use: true,
      note: "External cloud provider (Google). On the Free Tier Google may use submitted content to improve its products and human reviewers may read it. Data leaves this device.",
      models: [],
    },
    {
      provider_id: "openai_api",
      display_name: "OpenAI (API)",
      state: "disabled",
      enabled: false,
      cost_class: "paid_api",
      external: true,
      free_tier_data_use: false,
      note: "Disabled. The OpenAI API is separate paid billing.",
      models: [],
    },
    {
      provider_id: "grok_api",
      display_name: "Grok (xAI API)",
      state: "disabled",
      enabled: false,
      cost_class: "paid_api",
      external: true,
      free_tier_data_use: false,
      note: "Disabled. The xAI API is separate prepaid billing.",
      models: [],
    },
  ];
  const modelsSnapshot = (): ProvidersStatus => ({
    available: true,
    providers: providerRows(),
    preferences: { ...models.prefs },
    content: {
      mode: "permissive",
      topic_blocklist: [...models.blocklist],
      follow_user_tone: true,
      private_content_auto_memory: false,
    },
    routing_mode: "auto",
    paid_fallback: "off",
    max_provider_attempts: 2,
  });
  // First run: no local voice models, no owner security, nobody enrolled.
  const firstRun = fixture === "firstrun";
  const MODEL_BYTES = 569_529_058; // the pinned speaker + small Whisper files
  const identity = {
    enrolled: fixture === "guest" || fixture === "attention" || handsFree,
    samples: 0,
    session: null as string | null,
    guestUntil: fixture === "guest" ? Date.now() + 15 * 60_000 : 0,
    modelsReady: !firstRun,
    modelState: (firstRun ? "not_installed" : "installed") as VoiceModelsInfo["state"],
    modelBytes: firstRun ? 0 : MODEL_BYTES,
    stepUp: !firstRun,
  };
  const setupState = (): VoiceSetupState => {
    if (!identity.modelsReady) return identity.modelState === "installed" ? "restart_required" : "models_missing";
    if (identity.enrolled) return "enrolled";
    return identity.stepUp ? "not_enrolled" : "setup_required";
  };
  const snapshot = (): IdentityStatus => {
    const remaining = Math.max(0, Math.round((identity.guestUntil - Date.now()) / 1000));
    return {
      available: identity.modelsReady,
      setup_state: setupState(),
      step_up_configured: identity.stepUp,
      models: {
        state: identity.modelState,
        bytes_done: identity.modelBytes,
        bytes_total: MODEL_BYTES,
        reason_code: null,
      },
      enrolled: identity.enrolled,
      mode: remaining > 0 ? "guest_mode" : "owner_only",
      guest: { active: remaining > 0, seconds_remaining: remaining },
      last_verification: fixture === "attention" ? "not_verified" : identity.enrolled ? "verified" : null,
      speaker_model: "configured",
      local_stt: "configured",
      persian_tts: "not_configured",
      samples_needed: 3,
      samples_max: 5,
    };
  };
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
        agent: fixture === "degraded" ? "not_configured" : "configured",
        knowledge: "available",
        memory: "available",
        tools: "foundation_ready",
        tool_count: 0,
        server_count: 0,
        voice_input: fixture === "voice" || handsFree || fixture === "setup" ? "configured" : "not_configured",
        voice_activation: fixture === "voice" ? "unavailable" : activation,
        speech_output: "configured",
        speech_profiles: [{ profile_id: "sam_default", languages: ["en"] }],
        voice_identity: "configured",
        persian_tts: "not_configured",
        computer_control: "not_configured",
        coding_agent: "not_configured",
        conversation_history: "session_local",
        memory_storage: "in_process",
        principal_label: "local-user",
      };
    },
    async chat(message, language = "auto") {
      await wait(500);
      log("chat", "Sam request", "completed");
      const detected = detectLanguage(message);
      const lang = language === "auto" ? (detected ?? "en") : language;
      return {
        ...empty,
        status: "ok",
        reference_id: "demo-exec",
        language: lang,
        direction: directionFor(lang),
        reply:
          lang === "fa"
            ? `این یک پاسخ نمایشی است: «${message.slice(0, 60)}». در برنامهٔ واقعی، پاسخ از هستهٔ عامل سام می‌آید. برای نمونه: API و Docker به همان شکل لاتین می‌مانند. https://example.com/docs`
            : `This is a demo reply to: "${message.slice(0, 80)}". In the real app the answer comes from Sam's agent core through the local bridge.`,
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
    async voiceWake() {
      await wait(250);
      return { ...empty, status: "ok", wake: activation === "on", followed: false };
    },
    async setVoiceActivation(enabled) {
      await wait(100);
      activation = enabled ? "on" : "off";
      return { ...empty, status: "ok", voice_activation: activation };
    },
    async voiceUtterance() {
      if (fixture === "stranger") {
        await wait(1200);
        return {
          ...empty,
          status: "denied",
          reason_code: "owner_verification_required",
          message: "Sam didn't recognize your voice.",
          transcript: null,
          forwarded_to_agent: false,
          reply: null,
          speaker: null,
          speaker_result: "owner_not_verified",
          language: null,
          direction: null,
        };
      }
      if (fixture === "voice" || handsFree) {
        await wait(handsFree ? 2500 : 4000);
        return {
          ...empty,
          status: "ok",
          transcript: "What should I focus on this afternoon?",
          forwarded_to_agent: true,
          reply: "The grant proposal is the priority this afternoon; your review call is at 4 pm, and nothing else is urgent.",
          speaker: "owner",
          speaker_result: "owner_verified",
          language: "en",
          direction: "ltr",
        };
      }
      await wait();
      return {
        ...empty,
        status: "not_configured",
        reason_code: "not_configured",
        message: "That capability isn't configured.",
        transcript: null,
        forwarded_to_agent: false,
        reply: null,
        speaker: null,
        speaker_result: null,
        language: null,
        direction: null,
      };
    },
    async speak() {
      if (fixture === "voice" || handsFree) {
        await wait(300);
        const audio = demoTone();
        return { ...empty, status: "ok", audio_base64: audio, audio_format: "wav", byte_length: audio.length };
      }
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

    // ---- owner voice identity & Guest Mode (demo state only) ----
    async identityStatus() {
      await wait(60);
      return snapshot();
    },
    async identityEnrollBegin({ stepUp }) {
      await wait(150);
      if (stepUp !== "demo-step-up") throw new BridgeError("step_up_failed", "That step-up secret wasn't accepted.");
      identity.session = `enroll-${Date.now()}`;
      identity.samples = 0;
      return { ...empty, status: "ok", session_id: identity.session, samples_needed: 3 };
    },
    async installVoiceComponents() {
      await wait(80);
      if (identity.modelState !== "installed" && identity.modelState !== "installing") {
        identity.modelState = "installing";
        identity.modelBytes = 0;
        const step = () => {
          identity.modelBytes = Math.min(MODEL_BYTES, identity.modelBytes + MODEL_BYTES / 8);
          if (identity.modelBytes >= MODEL_BYTES) identity.modelState = "installed";
          else window.setTimeout(step, 250);
        };
        window.setTimeout(step, 250);
      }
      return { ...empty, status: "ok", models: snapshot().models };
    },
    async ownerSetup({ stepUp, confirm }) {
      await wait(150);
      if (stepUp !== confirm) return { ...empty, status: "rejected", reason_code: "step_up_mismatch", message: "The two entries don't match.", session_id: null, samples_needed: 3 };
      if (stepUp.trim().length < 16) return { ...empty, status: "rejected", reason_code: "step_up_too_short", message: "Use at least 16 characters.", session_id: null, samples_needed: 3 };
      if (identity.stepUp) return { ...empty, status: "rejected", reason_code: "step_up_already_set", message: "Owner security is already set up.", session_id: null, samples_needed: 3 };
      identity.stepUp = true;
      identity.session = `enroll-${Date.now()}`;
      identity.samples = 0;
      return { ...empty, status: "ok", session_id: identity.session, samples_needed: 3 };
    },
    async restartBackend() {
      await wait(300);
      if (identity.modelState === "installed") identity.modelsReady = true;
      return { status: "ok" };
    },
    async identityEnrollSample() {
      await wait(250);
      identity.samples += 1;
      return { ...empty, status: "ok", accepted: true, sample_count: identity.samples, samples_needed: 3 };
    },
    async identityEnrollComplete() {
      await wait(200);
      identity.enrolled = true;
      identity.session = null;
      log("voice", "Owner voice enrolled", "completed");
      return { ...empty, status: "ok" };
    },
    async identityEnrollCancel() {
      identity.session = null;
      return { ...empty, status: "ok" };
    },
    async identityDelete(stepUp) {
      await wait(150);
      if (stepUp !== "demo-step-up") throw new BridgeError("step_up_failed", "That step-up secret wasn't accepted.");
      identity.enrolled = false;
      identity.guestUntil = 0;
      return { ...empty, status: "ok" };
    },
    // ---- AI providers, routing and privacy (demo state only) ----
    async providersStatus() {
      await wait(60);
      return modelsSnapshot();
    },
    async setProviderPreferences(input) {
      await wait(120);
      const loosens =
        (input.personal_to_free_tier && !models.prefs.personal_to_free_tier) ||
        (input.private_to_free_tier && !models.prefs.private_to_free_tier) ||
        (input.private_to_claude_when_improvement_enabled &&
          !models.prefs.private_to_claude_when_improvement_enabled) ||
        (input.gemini_attestation === "owner_attested_unbilled" &&
          models.prefs.gemini_attestation !== "owner_attested_unbilled");
      if (loosens && input.stepUp !== "demo-step-up") {
        throw new BridgeError("step_up_failed", "That step-up secret wasn't accepted.");
      }
      if (input.preferred_provider === "openai_api" || input.preferred_provider === "grok_api") {
        throw new BridgeError("invalid", "That request was not valid.");
      }
      models.prefs = {
        preferred_provider: input.preferred_provider,
        allow_free_fallback: input.allow_free_fallback,
        personal_to_free_tier: input.personal_to_free_tier,
        private_to_free_tier: input.private_to_free_tier,
        claude_improvement_state: input.claude_improvement_state,
        private_to_claude_when_improvement_enabled: input.private_to_claude_when_improvement_enabled,
        gemini_attestation: input.gemini_attestation,
      };
      models.blocklist = [...input.topic_blocklist];
      log("settings", "Model preferences changed", "updated");
      return modelsSnapshot();
    },
    async guestChallenge() {
      await wait(100);
      return {
        ...empty,
        status: "ok",
        challenge_id: `ch-${Date.now()}`,
        text_en: "Please say: 7 4 9 2 blue",
        text_fa: "بگویید: 7 4 9 2 آبی",
        expires_in_seconds: 60,
      };
    },
    async guestStart({ minutes }) {
      await wait(300);
      identity.guestUntil = Date.now() + minutes * 60_000;
      log("voice", "Guest mode started", "active");
      return { ...empty, status: "ok" };
    },
    async guestEnd() {
      identity.guestUntil = 0;
      log("voice", "Guest mode revoked", "ok");
      return { ...empty, status: "ok" };
    },
    // ---- Professional Intelligence (demo state only; synthetic content) ----
    async professionalProfile() {
      await wait(80);
      return demoProfile(pro.sources);
    },
    async professionalIngest({ name, sourceType, privacyClass, resourceType }) {
      await wait(200);
      if (pro.sources.some((s) => s.label === name)) {
        return { ...ingestEmpty, status: "rejected", reason_code: "duplicate_source", message: "That content is already in your profile." };
      }
      pro.sources.push({
        source_id: `s_demo_${pro.sources.length + 1}`,
        label: name,
        source_type: sourceType,
        source_family_id: `f_demo_${pro.sources.length + 1}`,
        version: 1,
        privacy_class: privacyClass,
        freshness: "fresh",
        ingested_at: iso(),
        refreshed_at: iso(),
        resource_type: resourceType,
        accepted_claims: 3,
        pending_candidates: 0,
        conflicts: 0,
        candidate_extraction: "off",
      });
      log("professional", "Professional source ingest", "ingested");
      return { ...ingestEmpty, status: "ok", ingest_status: "ingested", claims_created: 3, evidence_added: 3 };
    },
    async professionalReview() {
      await wait(80);
      log("professional", "Professional review", "ok");
      return { ...empty, status: "ok" };
    },
    async professionalRemove(sourceId, confirmationId) {
      await wait(120);
      if (!confirmationId) {
        return {
          ...empty,
          status: "confirmation_required",
          reason_code: "confirmation_required",
          message: "This needs your confirmation.",
        };
      }
      pro.sources = pro.sources.filter((s) => s.source_id !== sourceId);
      log("professional", "Professional source removal", "allow");
      return { ...empty, status: "ok" };
    },
    async professionalQuery(mode, text) {
      await wait(100);
      const hits = demoClaims.filter((c) => c.statement.toLowerCase().includes(text.toLowerCase()));
      return {
        ...empty,
        status: "ok",
        mode,
        claims: mode === "search" ? hits : [],
        requirement:
          mode === "evidence_for"
            ? {
                status: hits.length ? "partially_supported" : "unknown",
                normalized_requirement: hits.length ? text : "",
                skills: hits,
                inferred_skills: [],
                projects: [],
                employment: [],
                research: [],
                education: [],
                unsupported_aspects: hits.length ? ["production use: demo data has no employment evidence"] : [],
                notes: hits.length ? [] : ["no recognised skill or qualification in the requirement"],
              }
            : null,
      };
    },
    // ---- Proactive Agent (demo state only; nothing is ever scheduled or sent) ----
    async proactiveOverview() {
      await wait(60);
      return {
        ...empty,
        status: "ok",
        tasks: auto.tasks,
        notifications: auto.notifications,
        history: [],
        conditions: [
          { condition_id: "deadline_approaching", label: "deadline", required_params: ["date"], optional_params: ["lead_hours", "time", "timezone"] },
          { condition_id: "professional_open_conflicts", label: "professional profile", required_params: [], optional_params: [] },
        ],
        limits: { min_interval_hours: 1, max_tasks: 100, max_enabled_tasks: 50, max_title_chars: 120, max_instruction_chars: 1000 },
        live_runs: 0,
        scheduler_enabled: auto.schedulerEnabled,
      };
    },
    async proactiveScheduler(enabled) {
      await wait(60);
      auto.schedulerEnabled = enabled;
      log("proactive", enabled ? "Automations scheduling on" : "Automations scheduling off", "ok");
      return { ...empty, status: "ok", scheduler_enabled: enabled };
    },
    // ---- Career & PhD (demo only; nothing is ever submitted or sent) ----
    async careerOverview() {
      await wait(60);
      return {
        ...empty,
        status: "ok",
        opportunities: careerDemo.opportunities,
        applications: [],
        contacts: [],
        outreach: [],
        follow_ups: [],
        review_queue:
          fixture === "attention"
            ? [
                { kind: "application", label: "Application draft ready for review", opportunity_id: null, item_id: "ap_demo" },
                { kind: "outreach", label: "Outreach draft ready for review", opportunity_id: null, item_id: "or_demo" },
              ]
            : [],
        preferences: null,
        submission_available: false,
        sending_available: false,
      };
    },
    async careerOpportunity(input) {
      await wait(80);
      if (input.action === "import" && input.text) {
        const title = /Title:\s*(.+)/.exec(input.text)?.[1] ?? "Untitled";
        const organization = /(?:Company|University):\s*(.+)/.exec(input.text)?.[1] ?? "Unknown";
        careerDemo.opportunities.push({
          opportunity_id: `op_demo_${careerDemo.opportunities.length + 1}`,
          type: input.opportunityType ?? "job",
          title,
          organization,
          location: null,
          work_mode: "unknown",
          source: input.sourceKind ?? "other",
          canonical_url: input.url ?? "",
          source_count: 1,
          deadline: null,
          status: "active",
          compensation: null,
          funding: null,
          sponsorship: "unknown",
          requirements: [],
          research_topics: [],
          tracked: false,
          has_official_application_url: false,
          last_verified: iso(),
        });
      }
      log("career", `Opportunity ${input.action}`, "ok");
      return { ...empty, status: "ok", item_id: null, state: null };
    },
    async careerFit() {
      await wait(60);
      return { ...empty, status: "ok", requirements: [], alignment_status: null, alignment: [] };
    },
    async careerDraft(input) {
      await wait(60);
      log("career", `Application ${input.action}`, "ok");
      return { ...empty, status: "ok", item_id: null, state: null };
    },
    async careerSubmit() {
      await wait(40);
      return { ...empty, status: "denied", reason_code: "submission_unavailable", message: "Sam can't submit applications yet.", item_id: null, state: null };
    },
    async careerContact() {
      await wait(40);
      return { ...empty, status: "ok", item_id: null, state: null };
    },
    async careerOutreach() {
      await wait(40);
      return { ...empty, status: "ok", item_id: null, state: null };
    },
    async careerSend() {
      await wait(40);
      return { ...empty, status: "denied", reason_code: "email_unavailable", message: "Sam can't send e-mail yet.", item_id: null, state: null };
    },
    async careerPreferences() {
      await wait(40);
      return { ...empty, status: "ok", item_id: null, state: null };
    },
    async proactiveCreate(input) {
      await wait(100);
      const s = input.schedule;
      const task: ProTask = {
        task_id: `t_demo_${auto.tasks.length + 1}`,
        title: input.title,
        task_type: input.taskType,
        timing_mode: input.timingMode,
        action: input.action,
        schedule: {
          timezone: s.timezone,
          start_date: s.startDate,
          time_of_day: s.timeOfDay,
          daypart: s.daypart,
          frequency: s.frequency,
          interval: s.interval,
          weekdays: s.weekdays,
          until: s.until,
          max_runs: s.maxRuns,
        },
        condition_id: input.conditionId,
        condition_params: input.conditionParams,
        semantics: input.conditionId ? input.semantics : null,
        instruction: input.instruction,
        privacy_class: input.privacyClass,
        notification_level: input.notificationLevel,
        proposed_action: input.proposedAction,
        cooldown_hours: input.cooldownHours,
        enabled: input.enabled,
        status: "active",
        expires_at: null,
        created_at: iso(),
        next_run_at: input.enabled ? `${s.startDate}T${s.timeOfDay ?? "08:00"}:00Z` : null,
        last_run_at: null,
        last_result: null,
        last_failure: null,
        last_reason: null,
        running: false,
        version: 1,
      };
      auto.tasks.push(task);
      log("proactive", "Automation create", "ok");
      return { ...empty, status: "ok", task };
    },
    async proactiveUpdate(input) {
      await wait(80);
      const task = auto.tasks.find((t) => t.task_id === input.taskId);
      if (!task) return { ...empty, status: "rejected", reason_code: "task_not_found", message: "That automation no longer exists.", task: null };
      if (input.enabled !== undefined) {
        task.enabled = input.enabled;
        task.next_run_at = input.enabled ? iso() : null;
      }
      if (input.title) task.title = input.title;
      if (input.cooldownHours) task.cooldown_hours = input.cooldownHours;
      task.version += 1;
      log("proactive", "Automation update", "ok");
      return { ...empty, status: "ok", task };
    },
    async proactiveDelete(taskId, confirmationId) {
      await wait(80);
      if (!confirmationId) {
        return { ...empty, status: "confirmation_required", reason_code: "confirmation_required", message: "This needs your confirmation." };
      }
      auto.tasks = auto.tasks.filter((t) => t.task_id !== taskId);
      log("proactive", "Automation delete", "allow");
      return { ...empty, status: "ok" };
    },
    async proactiveRun(taskId) {
      await wait(80);
      const task = auto.tasks.find((t) => t.task_id === taskId);
      if (!task) return { ...empty, status: "rejected", reason_code: "task_not_found", message: "That automation no longer exists." };
      const note: ProNotification = {
        notification_id: `n_demo_${auto.notifications.length + 1}`,
        task_id: taskId,
        title: task.title,
        summary: task.instruction || "Reminder.",
        created_at: iso(),
        reason_code: task.action === "watch" ? "condition_met" : "reminder_due",
        importance: task.notification_level === "requires_attention" ? "attention" : "info",
        source_label: task.action === "watch" ? "demo condition" : "reminder",
        proposed_action: task.proposed_action,
        privacy_class: task.privacy_class,
        read: false,
      };
      auto.notifications.unshift(note);
      task.last_run_at = iso();
      task.last_result = "notification_created";
      log("proactive", "Automation run now", "started");
      return { ...empty, status: "ok" };
    },
    async proactiveNotification(notificationId, action) {
      await wait(40);
      if (action === "dismiss") auto.notifications = auto.notifications.filter((n) => n.notification_id !== notificationId);
      else auto.notifications = auto.notifications.map((n) => (n.notification_id === notificationId ? { ...n, read: true } : n));
      return { ...empty, status: "ok" };
    },
  };
}

const ingestEmpty = {
  ...empty,
  source_id: null,
  ingest_status: null,
  claims_created: 0,
  claims_updated: 0,
  claims_skipped_rejected: 0,
  evidence_added: 0,
  conflicts_open: 0,
  candidate_extraction: "off",
  candidates_proposed: 0,
  candidates_accepted: 0,
  candidates_rejected: 0,
  unmapped_skills: 0,
} as const;

const careerDemo: { opportunities: CareerOpportunity[] } = { opportunities: [] };

const auto: { tasks: ProTask[]; notifications: ProNotification[]; schedulerEnabled: boolean } = {
  tasks: [],
  notifications: [],
  schedulerEnabled: false,
};

const pro: { sources: ProSource[] } = {
  sources: [
    {
      source_id: "s_demo_1",
      label: "demo-cv.txt",
      source_type: "master_cv",
      source_family_id: "f_demo_cv",
      version: 1,
      privacy_class: "personal",
      freshness: "fresh",
      ingested_at: new Date().toISOString(),
      refreshed_at: new Date().toISOString(),
      resource_type: "txt",
      accepted_claims: 4,
      pending_candidates: 0,
      conflicts: 0,
      candidate_extraction: "off",
    },
  ],
};

const demoClaims: ProClaim[] = [
  {
    claim_id: "c_demo_python",
    category: "technology",
    statement: "Technology: Python",
    strength: "single_source",
    review: "unreviewed",
    accepted: true,
    inferred: false,
    candidate: false,
    sensitivity: "personal",
    attributes: { skill_id: "python", display: "Python" },
    conflicted_attributes: [],
    source_count: 1,
    source_family_count: 1,
    canonical: true,
    evidence: [
      {
        evidence_id: "e_demo_1",
        source_id: "s_demo_1",
        source_label: "demo-cv.txt",
        source_type: "master_cv",
        source_family_id: "f_demo_cv",
        nature: "explicit_source",
        page_number: null,
        section_title: "Skills",
        paragraph_index: null,
        character_start: 120,
        character_end: 160,
        reference: "Python, TypeScript, React (demo)",
        context_claim_id: null,
        context_statement: null,
        basis_evidence_id: null,
      },
    ],
  },
];

function demoProfile(sources: ProSource[]): ProfessionalProfile {
  return {
    ...empty,
    status: "ok",
    claims: demoClaims,
    publications: [],
    education: [],
    experience: {
      entries: [],
      total_months: 0,
      total_years: 0,
      remainder_months: 0,
      conservative_months: 0,
      excluded_conflicted: 0,
      excluded_incomplete: 0,
      gaps: [],
    },
    conflicts: [],
    gaps: [],
    sources,
    counts: { technology: 1 },
  };
}
