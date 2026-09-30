import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { IdentityStatus, VoiceSetupState } from "../bridge/types";
import { baseIdentity, baseStatus, fakeAudioEnvironment, mockBridge, ok, renderApp } from "./helpers";

const SECRET = "a-long-private-owner-secret";
const MODELS_TOTAL = 569_529_058;

function identity(setup_state: VoiceSetupState, extra: Partial<IdentityStatus> = {}): IdentityStatus {
  const ready = setup_state === "setup_required" || setup_state === "not_enrolled" || setup_state === "enrolled";
  return {
    ...baseIdentity,
    available: ready,
    enrolled: setup_state === "enrolled" ? true : ready ? false : null,
    setup_state,
    step_up_configured: setup_state === "not_enrolled" || setup_state === "enrolled",
    models: {
      state: setup_state === "models_missing" ? "not_installed" : "installed",
      bytes_done: setup_state === "models_missing" ? 0 : MODELS_TOTAL,
      bytes_total: MODELS_TOTAL,
      reason_code: null,
    },
    ...extra,
  };
}

async function openSettings(bridge: ReturnType<typeof mockBridge>) {
  renderApp(bridge, fakeAudioEnvironment().env);
  const user = userEvent.setup();
  await screen.findByLabelText("Connection: Connected");
  await user.click(await screen.findByRole("button", { name: "Settings" }));
  return user;
}

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
  sessionStorage.clear();
});

describe("fresh install: voice components", () => {
  it("Home and Action Required name the real next step, and text still works", async () => {
    renderApp(mockBridge({ identityStatus: vi.fn(async () => identity("models_missing")) }), fakeAudioEnvironment().env);
    const user = userEvent.setup();
    expect(
      await screen.findByRole("button", { name: "Install voice components so Sam can hear you" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Owner verification required/)).toBeNull();
    await user.click(screen.getByRole("button", { name: /Action required/ }));
    expect(screen.getByText("Install voice components")).toBeInTheDocument();
    // Text is independent of voice: the keyboard fallback is still offered.
    expect(screen.getByRole("button", { name: /type|keyboard/i })).toBeEnabled();
  });

  it("installs only when the owner asks, shows its size and source, and reports progress", async () => {
    let state = identity("models_missing");
    const install = vi.fn(async () => {
      state = identity("models_missing", {
        models: { state: "installing", bytes_done: MODELS_TOTAL / 2, bytes_total: MODELS_TOTAL, reason_code: null },
      });
      return { ...ok, models: state.models };
    });
    const bridge = mockBridge({ identityStatus: vi.fn(async () => state), installVoiceComponents: install });
    const user = await openSettings(bridge);
    expect(await screen.findByText(/about 570 MB/)).toBeInTheDocument();
    expect(screen.getByText(/Hugging Face/)).toBeInTheDocument();
    expect(install).not.toHaveBeenCalled(); // never automatic
    await user.click(screen.getByRole("button", { name: "Install voice components" }));
    expect(install).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("progressbar")).toHaveAttribute("value", "50");
  });

  it("a failed install is clear and retryable, never a generic block", async () => {
    const failed = identity("models_missing", {
      models: { state: "failed", bytes_done: 0, bytes_total: MODELS_TOTAL, reason_code: "verification_failed" },
    });
    await openSettings(mockBridge({ identityStatus: vi.fn(async () => failed) }));
    expect(await screen.findByText(/couldn't be installed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Install voice components" })).toBeEnabled();
    expect(screen.queryByText(/Owner verification required/)).toBeNull();
  });

  it("after a verified install, restarts Sam's engine instead of hot-loading", async () => {
    const restart = vi.fn(async () => ({ status: "ok" }));
    const bridge = mockBridge({
      identityStatus: vi.fn(async () => identity("restart_required")),
      restartBackend: restart,
    });
    const user = await openSettings(bridge);
    await user.click(await screen.findByRole("button", { name: "Restart Sam's engine" }));
    expect(restart).toHaveBeenCalledTimes(1);
  });
});

describe("owner security + voice setup, without Terminal", () => {
  it("sets the secret once, clears it at once, keeps it out of storage, then records samples", async () => {
    const ownerSetup = vi.fn(async () => ({ ...ok, session_id: "enroll-1", samples_needed: 3 }));
    const bridge = mockBridge({ identityStatus: vi.fn(async () => identity("setup_required")), ownerSetup });
    const user = await openSettings(bridge);
    await user.click(await screen.findByRole("button", { name: "Set up owner verification" }));
    const first = screen.getByLabelText("Owner secret") as HTMLInputElement;
    const again = screen.getByLabelText("Type it again") as HTMLInputElement;
    expect(first.type).toBe("password");
    expect(again.type).toBe("password");
    expect(first.autocomplete).toBe("off");
    await user.type(first, SECRET);
    await user.type(again, SECRET);
    await user.click(screen.getByRole("button", { name: "Save and continue" }));
    expect(ownerSetup).toHaveBeenCalledTimes(1);
    expect(ownerSetup).toHaveBeenCalledWith({ stepUp: SECRET, confirm: SECRET });
    // The next step is recording; the secret is gone from the page and storage.
    expect(await screen.findByText(/Samples/)).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(SECRET);
    const stored = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage });
    expect(stored).not.toContain(SECRET);
  });

  it("shows the backend's content-free refusal and clears both fields", async () => {
    const ownerSetup = vi.fn(async () => ({
      ...ok,
      status: "rejected" as const,
      reason_code: "step_up_mismatch",
      message: "The two entries don't match.",
      session_id: null,
      samples_needed: 3,
    }));
    const bridge = mockBridge({ identityStatus: vi.fn(async () => identity("setup_required")), ownerSetup });
    const user = await openSettings(bridge);
    await user.click(await screen.findByRole("button", { name: "Set up owner verification" }));
    await user.type(screen.getByLabelText("Owner secret"), SECRET);
    await user.type(screen.getByLabelText("Type it again"), SECRET + "x");
    await user.click(screen.getByRole("button", { name: "Save and continue" }));
    expect(await screen.findByText("The two entries don't match.")).toBeInTheDocument();
    expect((screen.getByLabelText("Owner secret") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Type it again") as HTMLInputElement).value).toBe("");
  });

  it("each state has its own Home hint; none says 'Owner verification required'", async () => {
    for (const [state, hint] of [
      ["models_missing", "Install voice components so Sam can hear you"],
      ["restart_required", "Finish voice setup"],
      ["setup_required", "Set up your voice so Sam can recognize you"],
      ["not_enrolled", "Set up your voice so Sam can recognize you"],
    ] as const) {
      const view = renderApp(
        mockBridge({
          status: vi.fn(async () => ({ ...baseStatus, voice_activation: "on" as const })),
          identityStatus: vi.fn(async () => identity(state)),
        }),
        fakeAudioEnvironment().env,
      );
      expect(await screen.findByRole("button", { name: hint })).toBeInTheDocument();
      expect(screen.queryByText(/Owner verification required/)).toBeNull();
      view.unmount();
    }
  });

  it("Guest Mode never offers owner setup", async () => {
    const guest = identity("not_enrolled", { mode: "guest_mode", guest: { active: true, seconds_remaining: 600 } });
    renderApp(mockBridge({ identityStatus: vi.fn(async () => guest) }), fakeAudioEnvironment().env);
    await screen.findByLabelText("Connection: Connected");
    await waitFor(() => expect(screen.queryByRole("button", { name: /Set up your voice/ })).toBeNull());
  });
});
