import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CaptureMeter } from "../lib/voiceDiagnostics";
import type { SpeechPlaybackResponse } from "../bridge/types";
import { BridgeError } from "../bridge/bridge";
import { baseIdentity, baseStatus, fakeAudioEnvironment, mockBridge, ok, renderApp } from "./helpers";

describe("safe microphone diagnostics", () => {
  it("distinguishes digital silence from real non-zero samples without retaining audio", () => {
    const meter = new CaptureMeter();
    expect(meter.push(new Float32Array(16_000), 16_000)).toMatchObject({ microphone: "SILENT", rmsDbfs: -120, peakDbfs: -120, nonZeroRatio: 0, reason: "microphone_digital_silence" });
    const samples = Float32Array.from({ length: 16_000 }, (_, i) => 0.1 * Math.sin(i));
    const result = meter.push(samples, 16_000)!;
    expect(result.microphone).toBe("READY");
    expect(result.rmsDbfs).toBeGreaterThan(-30);
    expect(result.peakDbfs).toBeGreaterThan(-21);
    expect(result.nonZeroRatio).toBeGreaterThan(0.99);
    expect(Object.values(meter).every((value) => typeof value === "number")).toBe(true);
  });
});

describe("live model readiness", () => {
  it("retries a precise provider blocker and removes it immediately", async () => {
    let ready = false;
    const bridge = mockBridge({
      status: vi.fn(async () => ({ ...baseStatus, agent: ready ? "configured" as const : "not_configured" as const,
        model_issue: ready ? null : { provider: "claude_subscription", reason_code: "not_logged_in", message: "Claude needs authentication", resolution: "Open Claude Code and run /login, then retry providers." },
      })),
    });
    bridge.providersStatus.mockImplementation(async () => { ready = true; return { available: true } as Awaited<ReturnType<typeof bridge.providersStatus>>; });
    renderApp(bridge);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Action required: 1" }));
    expect(screen.getAllByText("Claude needs authentication").length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Retry providers" }));
    await waitFor(() => expect(screen.queryAllByText("Claude needs authentication")).toHaveLength(0));
    expect(bridge.chat).not.toHaveBeenCalled();
  });
});

describe("wake without a language model", () => {
  it("retries early identity checks as soon as the backend starts, without waiting for a poll", async () => {
    const audio = fakeAudioEnvironment();
    const identityStatus = vi.fn()
      .mockRejectedValueOnce(new BridgeError("starting", "Starting"))
      .mockResolvedValue({ ...baseIdentity, available: true, enrolled: true, setup_state: "enrolled" });
    const bridge = mockBridge({
      status: vi.fn(async () => ({ ...baseStatus, voice_input: "configured" as const, voice_activation: "on" as const })),
      identityStatus,
    });
    renderApp(bridge, audio.env);
    await waitFor(() => expect(document.querySelector(".presence")?.getAttribute("data-state")).toBe("sleeping"));
    expect(identityStatus).toHaveBeenCalledTimes(2);
    expect(audio.getUserMedia).toHaveBeenCalledTimes(1);
  });

  it("keeps the microphone open across provider recovery and closes it on quit", async () => {
    const audio = fakeAudioEnvironment();
    let ready = false;
    const bridge = mockBridge({
      status: vi.fn(async () => ({ ...baseStatus, agent: ready ? "configured" as const : "not_configured" as const, voice_input: "configured" as const, voice_activation: "on" as const })),
      identityStatus: vi.fn(async () => ({ ...baseIdentity, available: true, enrolled: true, setup_state: "enrolled" as const })),
    });
    bridge.providersStatus.mockImplementation(async () => { ready = true; return { available: true } as Awaited<ReturnType<typeof bridge.providersStatus>>; });
    const view = renderApp(bridge, audio.env);
    await waitFor(() => expect(document.querySelector(".presence")?.getAttribute("data-state")).toBe("sleeping"));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Action required: 1" }));
    await user.click(screen.getByRole("button", { name: "Retry providers" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Retry providers" })).toBeNull());
    expect(audio.getUserMedia).toHaveBeenCalledTimes(1);
    expect(audio.track.stop).not.toHaveBeenCalled();
    expect(bridge.chat).not.toHaveBeenCalled();
    view.unmount();
    expect(audio.track.stop).toHaveBeenCalled();
  });

  it("starts on launch, greets only a verified owner, and waits for real playback completion", async () => {
    const audio = fakeAudioEnvironment();
    let finish!: (result: SpeechPlaybackResponse) => void;
    const bridge = mockBridge({
      status: vi.fn(async () => ({ ...baseStatus, agent: "not_configured" as const, voice_input: "configured" as const, voice_activation: "on" as const })),
      identityStatus: vi.fn(async () => ({ ...baseIdentity, available: true, enrolled: true, setup_state: "enrolled" as const })),
      voiceWake: vi.fn(async () => ({ ...ok, wake: true, owner_verified: true, followed: false, speech_id: "greeting-ticket", speech_reason: null, language: "en" as const })),
      playSamSpeech: vi.fn(() => new Promise<SpeechPlaybackResponse>((resolve) => { finish = resolve; })),
    });
    renderApp(bridge, audio.env);
    const state = () => document.querySelector(".presence")?.getAttribute("data-state");
    await waitFor(() => expect(state()).toBe("sleeping"));
    act(() => {
      for (let i = 0; i < 5; i++) audio.emit(new Float32Array(2048).fill(0.2));
      for (let i = 0; i < 5; i++) audio.emit(new Float32Array(2048));
    });
    await waitFor(() => expect(bridge.playSamSpeech).toHaveBeenCalledWith("greeting-ticket"));
    expect(state()).toBe("greeting");
    expect(bridge.voiceUtterance).not.toHaveBeenCalled();
    expect(bridge.chat).not.toHaveBeenCalled();
    await act(async () => finish({ ...ok, kind: "local", audio_base64: null, audio_format: null, byte_length: null }));
    await waitFor(() => expect(state()).toBe("attending"));
  });
});
