import { render } from "@testing-library/react";
import { vi } from "vitest";
import { App } from "../App";
import type { SamBridge } from "../bridge/bridge";
import type {
  IdentityStatus,
  OperationResult,
  CareerOverview,
  ProactiveOverview,
  ProfessionalIngestResult,
  ProfessionalProfile,
  ProvidersStatus,
  StatusResponse,
} from "../bridge/types";
import type { LocalSpeechOutput } from "../lib/localSpeech";
import type { AudioEnvironment } from "../lib/recorder";

export const ok: OperationResult = {
  status: "ok",
  reason_code: null,
  message: null,
  reference_id: null,
  challenge: null,
};

export const baseStatus: StatusResponse = {
  backend: { status: "ok", service: "Sam", environment: "test" },
  agent: "configured",
  knowledge: "available",
  memory: "available",
  tools: "foundation_ready",
  tool_count: 0,
  server_count: 0,
  voice_input: "not_configured",
  speech_output: "not_configured",
  speech_profiles: [],
  voice_identity: "not_configured",
  persian_tts: "not_configured",
  computer_control: "not_configured",
  coding_agent: "not_configured",
  conversation_history: "session_local",
  memory_storage: "in_process",
  principal_label: "local-user",
};

export const baseIdentity: IdentityStatus = {
  available: false,
  setup_state: "voice_unavailable",
  step_up_configured: false,
  models: { state: "unavailable", bytes_done: 0, bytes_total: 0, reason_code: null },
  enrolled: null,
  mode: "owner_only",
  guest: { active: false, seconds_remaining: 0 },
  last_verification: null,
  speaker_model: "not_configured",
  local_stt: "not_configured",
  persian_tts: "not_configured",
  samples_needed: 3,
  samples_max: 5,
};

export const baseProviders: ProvidersStatus = {
  available: true,
  providers: [
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
      state: "available",
      enabled: true,
      cost_class: "free_tier",
      external: true,
      free_tier_data_use: true,
      note: "External cloud provider (Google). Data leaves this device.",
      models: ["gemini-3.8-flash", "gemini-3.5-flash-lite"],
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
  ],
  preferences: {
    preferred_provider: null,
    allow_free_fallback: true,
    personal_to_free_tier: false,
    private_to_free_tier: false,
    claude_improvement_state: "unknown",
    private_to_claude_when_improvement_enabled: false,
    gemini_attestation: "unknown",
  },
  content: { mode: "permissive", topic_blocklist: [], follow_user_tone: true, private_content_auto_memory: false },
  routing_mode: "auto",
  paid_fallback: "off",
  max_provider_attempts: 2,
};

export const emptyProfessional: ProfessionalProfile = {
  ...ok,
  claims: [],
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
  sources: [],
  counts: {},
};

export const emptyProactive: ProactiveOverview = {
  ...ok,
  tasks: [],
  notifications: [],
  history: [],
  conditions: [
    {
      condition_id: "deadline_approaching",
      label: "deadline",
      required_params: ["date"],
      optional_params: ["lead_hours", "time", "timezone"],
    },
  ],
  limits: {
    min_interval_hours: 1,
    max_tasks: 100,
    max_enabled_tasks: 50,
    max_title_chars: 120,
    max_instruction_chars: 1000,
  },
  live_runs: 0,
  scheduler_enabled: false,
};

export const emptyCareer: CareerOverview = {
  ...ok,
  opportunities: [],
  applications: [],
  contacts: [],
  outreach: [],
  follow_ups: [],
  review_queue: [],
  preferences: null,
  submission_available: false,
  sending_available: false,
};

export const emptyIngest: ProfessionalIngestResult = {
  ...ok,
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
};

export type MockBridge = { [K in keyof SamBridge]: ReturnType<typeof vi.fn> } & SamBridge;

export function mockBridge(overrides: Partial<SamBridge> = {}): MockBridge {
  const base: SamBridge = {
    status: vi.fn(async () => baseStatus),
    chat: vi.fn(async () => ({ ...ok, reply: "Hello from Sam", language: "en" as const, direction: "ltr" as const })),
    knowledgeList: vi.fn(async () => ({ ...ok, resources: [] })),
    knowledgeQuery: vi.fn(async () => ({ ...ok, hits: [] })),
    knowledgeIngest: vi.fn(async () => ({ ...ok, resource: null, duplicate_of: null })),
    knowledgeRemove: vi.fn(async () => ok),
    memorySearch: vi.fn(async () => ({ ...ok, items: [], working: [] })),
    tools: vi.fn(async () => ({ state: "foundation_ready" as const, servers: [], tools: [] })),
    permissions: vi.fn(async () => ({ grants: [] })),
    revokeGrant: vi.fn(async () => ok),
    activity: vi.fn(async () => ({ scope: "current_session" as const, items: [] })),
    decideConfirmation: vi.fn(async (id: string, approved: boolean) => ({
      status: approved ? ("approved" as const) : ("denied" as const),
      confirmation_id: id,
    })),
    voiceUtterance: vi.fn(async () => ({
      ...ok,
      transcript: null,
      forwarded_to_agent: false,
      reply: null,
      speaker: null,
      speaker_result: null,
      language: null,
      direction: null,
    })),
    voiceWake: vi.fn(async () => ({ ...ok, wake: false, followed: false })),
    setVoiceActivation: vi.fn(async (enabled: boolean) => ({
      ...ok,
      voice_activation: enabled ? ("on" as const) : ("off" as const),
    })),
    speak: vi.fn(async () => ({
      ...ok,
      audio_base64: null,
      audio_format: null,
      byte_length: null,
    })),
    identityStatus: vi.fn(async () => baseIdentity),
    identityEnrollBegin: vi.fn(async () => ({ ...ok, session_id: "enroll-1", samples_needed: 3 })),
    installVoiceComponents: vi.fn(async () => ({
      ...ok,
      models: { state: "installing" as const, bytes_done: 0, bytes_total: 100, reason_code: null },
    })),
    ownerSetup: vi.fn(async () => ({ ...ok, session_id: "enroll-1", samples_needed: 3 })),
    restartBackend: vi.fn(async () => ({ status: "ok" })),
    identityEnrollSample: vi.fn(async () => ({ ...ok, accepted: true, sample_count: 1, samples_needed: 3 })),
    identityEnrollComplete: vi.fn(async () => ok),
    identityEnrollCancel: vi.fn(async () => ok),
    identityDelete: vi.fn(async () => ok),
    guestChallenge: vi.fn(async () => ({
      ...ok,
      challenge_id: "ch-1",
      text_en: "Please say: 7 4 9 2 blue",
      text_fa: "بگویید: 7 4 9 2 آبی",
      expires_in_seconds: 60,
    })),
    guestStart: vi.fn(async () => ok),
    guestEnd: vi.fn(async () => ok),
    providersStatus: vi.fn(async () => baseProviders),
    setProviderPreferences: vi.fn(async () => baseProviders),
    professionalProfile: vi.fn(async () => emptyProfessional),
    professionalIngest: vi.fn(async () => ({ ...emptyIngest, ingest_status: "ingested" })),
    proactiveOverview: vi.fn(async () => emptyProactive),
    careerOverview: vi.fn(async () => emptyCareer),
    careerOpportunity: vi.fn(async () => ({ ...ok, item_id: "op_1", state: null })),
    careerFit: vi.fn(async () => ({ ...ok, requirements: [], alignment_status: null, alignment: [] })),
    careerDraft: vi.fn(async () => ({ ...ok, item_id: "ap_1", state: "drafting" })),
    careerSubmit: vi.fn(async () => ({ ...ok, item_id: "ap_1", state: null })),
    careerContact: vi.fn(async () => ({ ...ok, item_id: "ct_1", state: null })),
    careerOutreach: vi.fn(async () => ({ ...ok, item_id: "or_1", state: "draft" })),
    careerSend: vi.fn(async () => ({ ...ok, item_id: "or_1", state: null })),
    careerPreferences: vi.fn(async () => ({ ...ok, item_id: null, state: null })),
    proactiveCreate: vi.fn(async () => ({ ...ok, task: null })),
    proactiveUpdate: vi.fn(async () => ({ ...ok, task: null })),
    proactiveDelete: vi.fn(async () => ok),
    proactiveRun: vi.fn(async () => ok),
    proactiveNotification: vi.fn(async () => ok),
    proactiveScheduler: vi.fn(async (enabled: boolean) => ({ ...ok, scheduler_enabled: enabled })),
    professionalReview: vi.fn(async () => ok),
    professionalRemove: vi.fn(async () => ok),
    professionalQuery: vi.fn(async (mode: "search" | "evidence_for") => ({
      ...ok,
      mode,
      claims: [],
      requirement: null,
    })),
  };
  return { ...base, ...overrides } as MockBridge;
}

export function renderApp(
  bridge: SamBridge,
  audioEnvironment?: AudioEnvironment | null,
  speech?: LocalSpeechOutput | null,
) {
  return render(<App bridge={bridge} audioEnvironment={audioEnvironment} speech={speech} />);
}

export function fakeAudioEnvironment() {
  const track = { stop: vi.fn() };
  const stream = { getTracks: () => [track] } as unknown as MediaStream;
  let handler: ((event: { inputBuffer: { getChannelData: () => Float32Array } }) => void) | null = null;
  const processor = {
    connect: vi.fn(),
    disconnect: vi.fn(),
    set onaudioprocess(value: typeof handler) {
      handler = value;
    },
    get onaudioprocess() {
      return handler;
    },
  };
  const source = { connect: vi.fn(), disconnect: vi.fn() };
  const context = {
    sampleRate: 16_000,
    destination: {},
    createMediaStreamSource: vi.fn(() => source),
    createScriptProcessor: vi.fn(() => processor),
    close: vi.fn(async () => undefined),
  } as unknown as AudioContext;
  const getUserMedia = vi.fn(async () => stream);
  const env: AudioEnvironment = { getUserMedia, createContext: () => context };
  return {
    env,
    track,
    getUserMedia,
    processor,
    context,
    emit(samples: Float32Array) {
      handler?.({ inputBuffer: { getChannelData: () => samples } });
    },
  };
}
