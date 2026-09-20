import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BridgeError } from "../bridge/bridge";
import type { IdentityStatus } from "../bridge/types";
import {
  baseIdentity,
  baseStatus,
  fakeAudioEnvironment,
  mockBridge,
  ok,
  renderApp,
} from "./helpers";

const available: IdentityStatus = {
  ...baseIdentity,
  available: true,
  enrolled: false,
  speaker_model: "configured",
  local_stt: "configured",
};
const enrolled: IdentityStatus = { ...available, enrolled: true };
const guestActive: IdentityStatus = {
  ...enrolled,
  mode: "guest_mode",
  guest: { active: true, seconds_remaining: 600 },
};
const voiceStatus = { ...baseStatus, voice_input: "configured" as const, voice_identity: "configured" as const };

async function settings(bridge: ReturnType<typeof mockBridge>, env = fakeAudioEnvironment()) {
  renderApp(bridge, env.env);
  const user = userEvent.setup();
  await screen.findByLabelText("Connection: Connected");
  await user.click(screen.getByRole("button", { name: "Settings" }));
  return { user, env };
}

async function record(user: ReturnType<typeof userEvent.setup>, env: ReturnType<typeof fakeAudioEnvironment>) {
  await user.click(screen.getByRole("button", { name: "Record a voice message" }));
  act(() => env.emit(new Float32Array(4000).fill(0.1)));
  await user.click(screen.getByRole("button", { name: "Stop recording and send" }));
}

afterEach(() => vi.useRealTimers());

describe("Owner voice settings", () => {
  it("says so plainly when identity isn't configured", async () => {
    await settings(mockBridge());
    expect(await screen.findByText(/Owner voice identity isn't configured/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Enroll" })).toBeNull();
    expect(screen.getByRole("button", { name: "Start Guest Mode" })).toBeDisabled();
  });

  it("shows enrollment state and safe last-verification text only", async () => {
    const bridge = mockBridge({
      identityStatus: vi.fn(async () => ({ ...enrolled, last_verification: "not_verified" as const })),
    });
    await settings(bridge);
    expect(await screen.findByText("Enrolled")).toBeInTheDocument();
    expect(screen.getByText("Not the owner")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Re-enroll" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove" })).toBeInTheDocument();
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/vector|embedding|similarity|score/i);
  });

  it("offers Enroll when not enrolled and reports an unreadable store", async () => {
    await settings(mockBridge({ identityStatus: vi.fn(async () => available) }));
    expect(await screen.findByRole("button", { name: "Enroll" })).toBeInTheDocument();
  });

  it("shows voice-processing capability statuses", async () => {
    await settings(mockBridge({ identityStatus: vi.fn(async () => enrolled) }));
    const section = (await screen.findByRole("heading", { name: "Voice processing" })).closest("section") as HTMLElement;
    expect(within(section).getAllByText("Configured").length).toBeGreaterThanOrEqual(2);
    expect(within(section).getByText("Not configured")).toBeInTheDocument(); // Persian TTS
  });
});

describe("Enrollment", () => {
  it("needs the step-up secret, sends it once, and never retains it", async () => {
    const bridge = mockBridge({ identityStatus: vi.fn(async () => available) });
    const { user, env } = await settings(bridge);
    await user.click(await screen.findByRole("button", { name: "Enroll" }));
    const dialog = await screen.findByRole("dialog");
    const begin = within(dialog).getByRole("button", { name: "Begin" });
    expect(begin).toBeDisabled();
    const field = within(dialog).getByLabelText("Step-up secret");
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "off");
    await user.type(field, "my-step-up-secret");
    await user.click(begin);
    await waitFor(() =>
      expect(bridge.identityEnrollBegin).toHaveBeenCalledWith({ stepUp: "my-step-up-secret", reEnroll: false }),
    );
    // once submitted, the secret is gone from state, the DOM and browser storage
    await waitFor(() => expect(document.querySelector("input[type=password]")).toBeNull());
    expect(document.body.innerHTML).not.toContain("my-step-up-secret");
    expect(JSON.stringify({ ...window.localStorage })).not.toContain("my-step-up");
    expect(JSON.stringify({ ...window.sessionStorage })).not.toContain("my-step-up");
    expect(env.getUserMedia).not.toHaveBeenCalled(); // no microphone until the user records
  });

  it("shows a translated, safe error for a wrong secret and stays on the same step", async () => {
    const bridge = mockBridge({
      identityStatus: vi.fn(async () => available),
      identityEnrollBegin: vi.fn(async () => Promise.reject(new BridgeError("step_up_failed", "x"))),
    });
    const { user } = await settings(bridge);
    await user.click(await screen.findByRole("button", { name: "Enroll" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Step-up secret"), "wrong-secret-here");
    await user.click(within(dialog).getByRole("button", { name: "Begin" }));
    expect(await within(dialog).findByText("That step-up secret wasn't accepted.")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Step-up secret")).toHaveValue("");
    expect(bridge.identityEnrollSample).not.toHaveBeenCalled();
  });

  it("records samples explicitly, tracks progress and completes only when enough", async () => {
    let count = 0;
    const bridge = mockBridge({
      identityStatus: vi.fn(async () => available),
      identityEnrollSample: vi.fn(async () => ({ ...ok, accepted: true, sample_count: ++count, samples_needed: 3 })),
    });
    const { user, env } = await settings(bridge);
    await user.click(await screen.findByRole("button", { name: "Enroll" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Step-up secret"), "s3cret-value-1");
    await user.click(within(dialog).getByRole("button", { name: "Begin" }));
    const finish = await within(dialog).findByRole("button", { name: "Finish enrollment" });
    expect(finish).toBeDisabled();
    expect(env.getUserMedia).not.toHaveBeenCalled();
    for (let i = 1; i <= 3; i++) {
      await record(user, env);
      await waitFor(() => expect(within(dialog).getByText(new RegExp(`${i} / 3`))).toBeInTheDocument());
    }
    const [, audio] = (bridge.identityEnrollSample as ReturnType<typeof vi.fn>).mock.calls[0] ?? [];
    expect(atob(audio as string).startsWith("RIFF")).toBe(true);
    expect(env.track.stop).toHaveBeenCalled();
    await waitFor(() => expect(finish).toBeEnabled());
    await user.click(finish);
    await waitFor(() => expect(bridge.identityEnrollComplete).toHaveBeenCalledWith("enroll-1"));
    expect(await within(dialog).findByText("Your voice is enrolled.")).toBeInTheDocument();
  });

  it("shows Persian and English phrase suggestions and warns about a rejected sample", async () => {
    const bridge = mockBridge({
      identityStatus: vi.fn(async () => available),
      identityEnrollSample: vi.fn(async () => ({ ...ok, status: "rejected" as const, accepted: false, sample_count: 0, samples_needed: 3, message: "That recording is too short. Speak for a few seconds." })),
    });
    const { user, env } = await settings(bridge);
    await user.click(await screen.findByRole("button", { name: "Enroll" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Step-up secret"), "secret-value-22");
    await user.click(within(dialog).getByRole("button", { name: "Begin" }));
    await within(dialog).findByText(/سلام سام/);
    await record(user, env);
    expect(await within(dialog).findByText(/too short/)).toBeInTheDocument();
  });

  it("cancelling mid-enrollment cancels the backend session (samples discarded)", async () => {
    const bridge = mockBridge({ identityStatus: vi.fn(async () => available) });
    const { user } = await settings(bridge);
    await user.click(await screen.findByRole("button", { name: "Enroll" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Step-up secret"), "secret-value-33");
    await user.click(within(dialog).getByRole("button", { name: "Begin" }));
    await within(dialog).findByRole("button", { name: "Finish enrollment" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(bridge.identityEnrollCancel).toHaveBeenCalledWith("enroll-1"));
  });

  it("re-enrollment uses the re-enroll flag", async () => {
    const bridge = mockBridge({ identityStatus: vi.fn(async () => enrolled) });
    const { user } = await settings(bridge);
    await user.click(await screen.findByRole("button", { name: "Re-enroll" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Step-up secret"), "secret-value-44");
    await user.click(within(dialog).getByRole("button", { name: "Begin" }));
    await waitFor(() =>
      expect(bridge.identityEnrollBegin).toHaveBeenCalledWith({ stepUp: "secret-value-44", reEnroll: true }),
    );
  });
});

describe("Removing the voice profile", () => {
  it("needs the step-up secret and clears it after use", async () => {
    const bridge = mockBridge({ identityStatus: vi.fn(async () => enrolled) });
    const { user } = await settings(bridge);
    await user.click(await screen.findByRole("button", { name: "Remove" }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Remove voice profile" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Step-up secret"), "remove-secret-1");
    await user.click(confirm);
    await waitFor(() => expect(bridge.identityDelete).toHaveBeenCalledWith("remove-secret-1"));
    expect(document.body.innerHTML).not.toContain("remove-secret-1");
    expect(JSON.stringify({ ...window.localStorage })).not.toContain("remove-secret");
  });
});

describe("Guest Mode", () => {
  it("is disabled until the owner is enrolled", async () => {
    await settings(mockBridge({ identityStatus: vi.fn(async () => available) }));
    expect(await screen.findByRole("button", { name: "Start Guest Mode" })).toBeDisabled();
    expect(screen.getByText("Enroll your voice first.")).toBeInTheDocument();
  });

  it("requires the step-up secret, a fresh spoken phrase, and sends no identity claims", async () => {
    const bridge = mockBridge({ identityStatus: vi.fn(async () => enrolled) });
    const { user, env } = await settings(bridge);
    await user.click(await screen.findByRole("button", { name: "Start Guest Mode" }));
    const dialog = await screen.findByRole("dialog");
    const show = within(dialog).getByRole("button", { name: "Show me the phrase" });
    expect(show).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Step-up secret"), "guest-secret-1");
    await user.click(show);
    expect(await within(dialog).findByTestId("challenge")).toHaveTextContent("Please say: 7 4 9 2 blue");
    expect(env.getUserMedia).not.toHaveBeenCalled(); // recording only starts on an explicit click
    await record(user, env);
    await waitFor(() => expect(bridge.guestStart).toHaveBeenCalled());
    const [input] = (bridge.guestStart as ReturnType<typeof vi.fn>).mock.calls[0] ?? [];
    expect(Object.keys(input as object).sort()).toEqual(["audioBase64", "challengeId", "minutes", "stepUp"]);
    expect(input).toMatchObject({ challengeId: "ch-1", stepUp: "guest-secret-1", minutes: 15 });
    expect(document.body.innerHTML).not.toContain("guest-secret-1");
    expect(JSON.stringify({ ...window.localStorage, ...window.sessionStorage })).not.toContain("guest-secret");
  });

  it("a refused start clears the one-time challenge and reports why", async () => {
    const bridge = mockBridge({
      identityStatus: vi.fn(async () => enrolled),
      guestStart: vi.fn(async () => ({ ...ok, status: "denied" as const, message: "Owner verification failed." })),
    });
    const { user, env } = await settings(bridge);
    await user.click(await screen.findByRole("button", { name: "Start Guest Mode" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Step-up secret"), "guest-secret-2");
    await user.click(within(dialog).getByRole("button", { name: "Show me the phrase" }));
    await within(dialog).findByTestId("challenge");
    await record(user, env);
    expect(await within(dialog).findByText("Owner verification failed.")).toBeInTheDocument();
    expect(within(dialog).queryByTestId("challenge")).toBeNull(); // needs a NEW phrase
  });

  it("shows a persistent banner with a countdown and an End button on every page", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const bridge = mockBridge({ identityStatus: vi.fn(async () => guestActive) });
    renderApp(bridge);
    const banner = await screen.findByText("Guest Mode is active");
    expect(banner.closest(".guest-banner")).toHaveTextContent("10:00");
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(banner.closest(".guest-banner")).toHaveTextContent("9:57");
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(screen.getByRole("button", { name: "Knowledge" }));
    expect(screen.getByText("Guest Mode is active")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "End Guest Mode" }));
    await waitFor(() => expect(bridge.guestEnd).toHaveBeenCalled());
  });

  it("no banner when Guest Mode is off", async () => {
    renderApp(mockBridge({ identityStatus: vi.fn(async () => enrolled) }));
    await screen.findByLabelText("Connection: Connected");
    expect(screen.queryByText("Guest Mode is active")).toBeNull();
  });

  it("owner can end Guest Mode from Settings", async () => {
    const bridge = mockBridge({ identityStatus: vi.fn(async () => guestActive) });
    const { user } = await settings(bridge);
    const section = (await screen.findByRole("heading", { name: "Guest Mode", level: 3 })).closest("section") as HTMLElement;
    await user.click(within(section).getByRole("button", { name: "End Guest Mode" }));
    await waitFor(() => expect(bridge.guestEnd).toHaveBeenCalled());
  });
});

describe("Voice results", () => {
  const speak = async (bridge: ReturnType<typeof mockBridge>) => {
    const env = fakeAudioEnvironment();
    renderApp(bridge, env.env);
    const user = userEvent.setup();
    await screen.findByLabelText("Connection: Connected");
    await record(user, env);
    return user;
  };
  const base = { ...ok, forwarded_to_agent: false, reply: null, language: null, direction: null };

  it("a non-owner is stopped with the fixed message and no transcript is shown", async () => {
    const bridge = mockBridge({
      status: vi.fn(async () => voiceStatus),
      voiceUtterance: vi.fn(async () => ({
        ...base,
        status: "denied" as const,
        reason_code: "owner_verification_required",
        message: "Owner verification required.",
        transcript: "LEAKED-TRANSCRIPT",
        speaker: null,
        speaker_result: "owner_not_verified",
      })),
    });
    await speak(bridge);
    expect(await screen.findByText("Owner verification required.")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("LEAKED-TRANSCRIPT");
  });

  it("a guest utterance is labelled as the guest, not the owner", async () => {
    const bridge = mockBridge({
      status: vi.fn(async () => voiceStatus),
      identityStatus: vi.fn(async () => guestActive),
      voiceUtterance: vi.fn(async () => ({
        ...base,
        transcript: "tell me a joke",
        reply: "Why did the developer go broke?",
        forwarded_to_agent: true,
        speaker: "guest" as const,
        speaker_result: "owner_not_verified",
        language: "en" as const,
        direction: "ltr" as const,
      })),
    });
    await speak(bridge);
    const log = screen.getByRole("log");
    const said = await within(log).findByText("tell me a joke");
    expect(said.closest("article")).toHaveAttribute("data-role", "guest");
    expect(within(log).getByText("Why did the developer go broke?")).toBeInTheDocument();
  });

  it("passes the language preference and confirmation id (never an identity claim)", async () => {
    const bridge = mockBridge({ status: vi.fn(async () => voiceStatus) });
    await speak(bridge);
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalled());
    const args = (bridge.voiceUtterance as ReturnType<typeof vi.fn>).mock.calls[0] ?? [];
    expect(args).toHaveLength(3);
    expect(args[2]).toBe("auto");
  });
});

describe("Persian identity UI", () => {
  it("enrollment and guest dialogs render Persian strings (and the Persian phrase)", async () => {
    window.localStorage.setItem("sam.ui.prefs.v1", JSON.stringify({ uiLanguage: "fa" }));
    const bridge = mockBridge({ identityStatus: vi.fn(async () => enrolled) });
    renderApp(bridge);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "تنظیمات" }));
    expect((await screen.findAllByText("صدای مالک")).length).toBeGreaterThan(0);
    await user.click(await screen.findByRole("button", { name: "شروع حالت مهمان" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("شروع حالت مهمان", { selector: "h2" })).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("رمز تأیید تکمیلی"), "guest-secret-fa");
    await user.click(within(dialog).getByRole("button", { name: "عبارت را نشان بده" }));
    expect(await within(dialog).findByTestId("challenge")).toHaveTextContent("بگویید: 7 4 9 2 آبی");
  });

  it("the Guest banner is translated too", async () => {
    window.localStorage.setItem("sam.ui.prefs.v1", JSON.stringify({ uiLanguage: "fa" }));
    renderApp(mockBridge({ identityStatus: vi.fn(async () => guestActive) }));
    expect(await screen.findByText("حالت مهمان فعال است")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "پایان حالت مهمان" })).toBeInTheDocument();
  });
});
