import { render } from "@testing-library/react";
import { vi } from "vitest";
import { App } from "../App";
import type { SamBridge } from "../bridge/bridge";
import type { IdentityStatus, ProvidersStatus, OperationResult, StatusResponse } from "../bridge/types";
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
    speak: vi.fn(async () => ({
      ...ok,
      audio_base64: null,
      audio_format: null,
      byte_length: null,
    })),
    identityStatus: vi.fn(async () => baseIdentity),
    identityEnrollBegin: vi.fn(async () => ({ ...ok, session_id: "enroll-1", samples_needed: 3 })),
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
  };
  return { ...base, ...overrides } as MockBridge;
}

export function renderApp(bridge: SamBridge, audioEnvironment?: AudioEnvironment | null) {
  return render(<App bridge={bridge} audioEnvironment={audioEnvironment} />);
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
