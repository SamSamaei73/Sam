import { render } from "@testing-library/react";
import { vi } from "vitest";
import { App } from "../App";
import type { SamBridge } from "../bridge/bridge";
import type { OperationResult, StatusResponse } from "../bridge/types";
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
  computer_control: "not_configured",
  coding_agent: "not_configured",
  conversation_history: "session_local",
  memory_storage: "in_process",
  principal_label: "local-user",
};

export type MockBridge = { [K in keyof SamBridge]: ReturnType<typeof vi.fn> } & SamBridge;

export function mockBridge(overrides: Partial<SamBridge> = {}): MockBridge {
  const base: SamBridge = {
    status: vi.fn(async () => baseStatus),
    chat: vi.fn(async () => ({ ...ok, reply: "Hello from Sam" })),
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
    })),
    speak: vi.fn(async () => ({
      ...ok,
      audio_base64: null,
      audio_format: null,
      byte_length: null,
    })),
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
