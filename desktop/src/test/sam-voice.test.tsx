import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { SamBridge } from "../bridge/bridge";
import { attentionItems } from "../home/useAttention";
import { baseIdentity, baseStatus, mockBridge, ok, readyVoice, renderApp } from "./helpers";

const identity = { ...baseIdentity, available: true, enrolled: true, setup_state: "enrolled" as const };
const healthy = { micDenied: false, needsSetup: false, identity, samVoice: readyVoice };

describe("live Action Required items", () => {
  it("does not turn a historical mismatch into an enrollment blocker", () => {
    expect(attentionItems(baseStatus, "connected", "not_verified", 0, 0, healthy)).toEqual([]);
  });
  it("counts the actual two issues and gives each a safe reason and resolution", () => {
    const items = attentionItems({ ...baseStatus, agent: "not_configured" }, "connected", null, 0, 0, {
      ...healthy, samVoice: { ...readyVoice, state: "SAM_VOICE_NOT_CONFIGURED", reason_code: "speech_language_unavailable" },
    });
    expect(items).toHaveLength(2);
    expect(items.map((i) => i.reason_code)).toEqual(["language_model_unavailable", "speech_language_unavailable"]);
    expect(items.every((i) => i.action && i.explanation && i.blocks.length && i.state)).toBe(true);
  });
  it("removes a resolved TTS issue on the next state update", () => {
    expect(attentionItems(baseStatus, "connected", null, 0, 0, {
      ...healthy, samVoice: { ...readyVoice, state: "SAM_VOICE_PERMISSION_REQUIRED", reason_code: "automatic_speech_off" },
    })).toHaveLength(1);
    expect(attentionItems(baseStatus, "connected", null, 0, 0, healthy)).toHaveLength(0);
  });
  it("deduplicates a current speech failure and provider status into one card", () => {
    const items = attentionItems(baseStatus, "connected", null, 0, 0, {
      ...healthy, speechIssue: "speech_failed",
      samVoice: { ...readyVoice, state: "SAM_VOICE_PROVIDER_UNAVAILABLE", reason_code: "local_voice_unavailable" },
    });
    expect(items).toHaveLength(1);
    expect(items[0]?.reason_code).toBe("speech_failed");
  });
  it("deduplicates missing profile and hands-free setup", () => {
    const items = attentionItems(baseStatus, "connected", null, 0, 0, {
      ...healthy, needsSetup: true, setupState: "not_enrolled",
      identity: { ...identity, enrolled: false, setup_state: "not_enrolled" },
    });
    expect(items).toHaveLength(1);
    expect(items[0]?.reason_code).toBe("voice_not_enrolled");
  });
  it("reports a current speaker failure as a retry, never a demand to enroll again", () => {
    const items = attentionItems(baseStatus, "connected", "not_verified", 0, 0, { ...healthy, identityIssue: "speaker_not_recognized" });
    expect(items[0]?.action).toBe("Try again");
    expect(items[0]?.retryVoice).toBe(true);
  });
  it("shows both actual blockers when the Home count is expanded", async () => {
    renderApp(mockBridge({ status: vi.fn(async () => ({ ...baseStatus, agent: "not_configured" as const })),
      samVoiceStatus: vi.fn(async () => ({ ...readyVoice, state: "SAM_VOICE_NOT_CONFIGURED" as const, reason_code: "speech_language_unavailable" })),
    }));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Action required: 2" }));
    expect(screen.getByRole("button", { name: "Review AI providers" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set up / preview voice" })).toBeInTheDocument();
    expect(screen.getByText("language_model_unavailable")).toBeInTheDocument();
    expect(screen.getByText("speech_language_unavailable")).toBeInTheDocument();
  });
});

describe("owner Sam Voice setup", () => {
  it("previews through production bridge tickets without chat or manual Read Aloud", async () => {
    let finish!: (value: Awaited<ReturnType<SamBridge["playSamSpeech"]>>) => void;
    const bridge = mockBridge({ playSamSpeech: vi.fn<SamBridge["playSamSpeech"]>(() => new Promise((resolve) => { finish = resolve; })) });
    renderApp(bridge);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Settings" }));
    await user.click(await screen.findByRole("button", { name: "Preview Sam's voice" }));
    await waitFor(() => expect(bridge.playSamSpeech).toHaveBeenCalledWith("preview-ticket"));
    expect(bridge.previewSamVoice).toHaveBeenCalledWith("en");
    expect(screen.getByRole("img", { name: "Sam is speaking" })).toBeInTheDocument();
    expect(bridge.chat).not.toHaveBeenCalled();
    expect(bridge.speak).not.toHaveBeenCalled();
    await act(async () => finish({ ...ok, kind: "local", audio_base64: null, audio_format: null, byte_length: null }));
    expect(screen.queryByText("Sam is speaking…")).toBeNull();
  });
  it("uses the selected Persian session language for preview", async () => {
    window.localStorage.setItem("sam.ui.prefs.v1", JSON.stringify({ responseLanguage: "fa" }));
    const bridge = mockBridge();
    renderApp(bridge);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Settings" }));
    await user.click(await screen.findByRole("button", { name: "Preview Sam's voice" }));
    expect(bridge.previewSamVoice).toHaveBeenCalledWith("fa");
  });
  it("owner disabling speech updates the trusted setting and stops the preview", async () => {
    const bridge = mockBridge();
    renderApp(bridge);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Settings" }));
    const toggle = await screen.findByRole("checkbox", { name: "Sam speaks responses" });
    await waitFor(() => expect(toggle).toBeChecked());
    await user.click(toggle);
    expect(bridge.setSamVoice).toHaveBeenCalledWith(false, false, "en");
    await waitFor(() => expect(toggle).not.toBeChecked());
    expect(bridge.careerSend).not.toHaveBeenCalled();
    expect(bridge.careerSubmit).not.toHaveBeenCalled();
  });
  it("preserves the exact safe playback failure instead of claiming success", async () => {
    const bridge = mockBridge({ playSamSpeech: vi.fn(async () => ({ ...ok, status: "failed" as const, reason_code: "speech_unavailable", kind: null, audio_base64: null, audio_format: null, byte_length: null })) });
    renderApp(bridge);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Settings" }));
    await user.click(await screen.findByRole("button", { name: "Preview Sam's voice" }));
    await waitFor(() => expect(bridge.playSamSpeech).toHaveBeenCalledOnce());
    expect(screen.queryByText("Sam is speaking…")).toBeNull();
    expect(screen.getAllByRole("alert").length).toBeGreaterThan(0);
  });
});
