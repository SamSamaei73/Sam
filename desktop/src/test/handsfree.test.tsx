import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { IdentityStatus, VoiceResponse } from "../bridge/types";
import type { LocalSpeechOutput } from "../lib/localSpeech";
import { baseIdentity, baseStatus, fakeAudioEnvironment, mockBridge, ok, renderApp } from "./helpers";

const FRAME = 1_600; // 100 ms at 16 kHz
const enrolled: IdentityStatus = {
  ...baseIdentity,
  available: true,
  enrolled: true,
  setup_state: "enrolled",
  step_up_configured: true,
  speaker_model: "configured",
};
const handsFreeStatus = {
  ...baseStatus,
  voice_input: "configured" as const,
  voice_identity: "configured" as const,
  voice_activation: "on" as const,
};

const reply = (overrides: Partial<VoiceResponse> = {}): VoiceResponse => ({
  ...ok,
  transcript: "how is the project going",
  forwarded_to_agent: true,
  reply: "It's on track.",
  speaker: "owner",
  speaker_result: "owner_verified",
  language: "en",
  direction: "ltr",
  ...overrides,
});

function setup(overrides: Parameters<typeof mockBridge>[0] = {}, identity: IdentityStatus = enrolled) {
  const fake = fakeAudioEnvironment();
  const bridge = mockBridge({
    status: vi.fn(async () => handsFreeStatus),
    identityStatus: vi.fn(async () => identity),
    ...overrides,
  });
  const spoken: string[] = [];
  const endings: (() => void)[] = [];
  const speech: LocalSpeechOutput = {
    speak: (text, _language, onEnd) => {
      spoken.push(text);
      endings.push(onEnd);
      return { stop: () => onEnd() };
    },
  };
  renderApp(bridge, fake.env, speech);
  const say = (frames = 6, turn = false) => {
    act(() => {
      for (let i = 0; i < frames; i++) fake.emit(new Float32Array(FRAME).fill(0.2));
      for (let i = 0; i < (turn ? 11 : 6); i++) fake.emit(new Float32Array(FRAME));
    });
  };
  const silence = (frames = 20) =>
    act(() => {
      for (let i = 0; i < frames; i++) fake.emit(new Float32Array(FRAME));
    });
  const finishSpeaking = () => act(() => endings.shift()?.());
  return { fake, bridge, say, silence, spoken, finishSpeaking };
}

const presence = () => document.querySelector(".presence") as HTMLElement;
const state = () => presence()?.dataset.state;

afterEach(() => {
  vi.useRealTimers();
});

describe("hands-free voice activation", () => {
  it("waits for its name without any button and without calling anything", async () => {
    const { fake, bridge, silence } = setup();
    await waitFor(() => expect(state()).toBe("sleeping"));
    expect(fake.getUserMedia).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Say "Sam" to start')).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Talk to Sam/ })).toBeNull();
    silence(50);
    expect(bridge.voiceWake).not.toHaveBeenCalled();
    expect(bridge.voiceUtterance).not.toHaveBeenCalled();
    expect(bridge.chat).not.toHaveBeenCalled();
    expect(bridge.speak).not.toHaveBeenCalled();
    expect(window.localStorage.getItem("sam.ui.prefs.v1") ?? "").not.toMatch(/audio|wav/i);
  });

  it("speech that is not its name does not wake Sam or reach the agent", async () => {
    const { bridge, say } = setup();
    await waitFor(() => expect(state()).toBe("sleeping"));
    say();
    await waitFor(() => expect(bridge.voiceWake).toHaveBeenCalledTimes(1));
    expect(state()).toBe("sleeping");
    expect(bridge.voiceUtterance).not.toHaveBeenCalled();
    expect(bridge.chat).not.toHaveBeenCalled();
  });

  it("wakes on its name, then talks hands-free turn after turn", async () => {
    const utterance = vi.fn(async () => reply());
    const { bridge, say, spoken, finishSpeaking } = setup({
      voiceWake: vi.fn(async () => ({ ...ok, wake: true, followed: false })),
      voiceUtterance: utterance,
    });
    await waitFor(() => expect(state()).toBe("sleeping"));
    say(); // "Sam"
    await waitFor(() => expect(["waking", "attending"]).toContain(state()));
    say(8, true); // the first request: no button, the turn closes on silence
    await waitFor(() => expect(utterance).toHaveBeenCalledTimes(1));
    expect((utterance.mock.calls[0] as unknown[] | undefined)?.[3]).toBe(true); // a hands-free turn
    await waitFor(() => expect(spoken).toEqual(["It's on track."]));
    expect(state()).toBe("speaking");
    expect(within(screen.getByRole("region", { name: "Latest exchange" })).getByText("It's on track.")).toBeInTheDocument();
    finishSpeaking();
    await waitFor(() => expect(state()).toBe("attending"));
    say(8, true); // the second turn: no "Sam" needed
    await waitFor(() => expect(utterance).toHaveBeenCalledTimes(2));
    expect(bridge.voiceWake).toHaveBeenCalledTimes(1);
  });

  it("a stop phrase puts Sam back to sleep", async () => {
    const { say } = setup({
      voiceWake: vi.fn(async () => ({ ...ok, wake: true, followed: false })),
      voiceUtterance: vi.fn(async () =>
        reply({ reason_code: "conversation_ended", message: "Okay.", transcript: null, reply: null }),
      ),
    });
    await waitFor(() => expect(state()).toBe("sleeping"));
    say();
    await waitFor(() => expect(state()).toBe("attending"));
    say(8, true);
    await waitFor(() => expect(state()).toBe("sleeping"));
  });

  it("an unrecognized speaker ends the conversation (fail closed)", async () => {
    const { say, bridge } = setup({
      voiceWake: vi.fn(async () => ({ ...ok, wake: true, followed: false })),
      voiceUtterance: vi.fn(async () =>
        reply({
          status: "denied",
          reason_code: "owner_verification_required",
          message: "Sam didn't recognize your voice.",
          transcript: null,
          reply: null,
          forwarded_to_agent: false,
        }),
      ),
    });
    await waitFor(() => expect(state()).toBe("sleeping"));
    say();
    await waitFor(() => expect(state()).toBe("attending"));
    say(8, true);
    await waitFor(() => expect(state()).toBe("sleeping"));
    expect(await screen.findByText("Sam didn't recognize your voice.")).toBeInTheDocument();
    expect(bridge.chat).not.toHaveBeenCalled();
  });

  it("a conversation ends by itself after a quiet while", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { say } = setup({ voiceWake: vi.fn(async () => ({ ...ok, wake: true, followed: false })) });
    await waitFor(() => expect(state()).toBe("sleeping"));
    say();
    await waitFor(() => expect(state()).toBe("attending"));
    act(() => vi.advanceTimersByTime(21_000));
    await waitFor(() => expect(state()).toBe("sleeping"));
  });

  it("ignores speech right after a false wake, and backs off when rate limited", async () => {
    const wake = vi.fn(async () => ({ ...ok, status: "rejected" as const, reason_code: "rate_limited", wake: false, followed: false }));
    const { say } = setup({ voiceWake: wake });
    await waitFor(() => expect(state()).toBe("sleeping"));
    say();
    await waitFor(() => expect(wake).toHaveBeenCalledTimes(1));
    say();
    say();
    expect(wake).toHaveBeenCalledTimes(1);
  });

  it("the name followed by a request is answered as the first turn", async () => {
    const utterance = vi.fn(async () => reply());
    const { say } = setup({
      voiceWake: vi.fn(async () => ({ ...ok, wake: true, followed: true })),
      voiceUtterance: utterance,
    });
    await waitFor(() => expect(state()).toBe("sleeping"));
    say(); // "Sam, how is the project going?"
    await waitFor(() => expect(utterance).toHaveBeenCalledTimes(1));
  });
});

describe("hands-free setup and safety", () => {
  it("without a voice profile it asks for setup and does not open the microphone", async () => {
    const { fake } = setup({}, { ...enrolled, enrolled: false, setup_state: "not_enrolled" });
    expect(await screen.findByRole("button", { name: "Set up your voice so Sam can recognize you" })).toBeInTheDocument();
    expect(fake.getUserMedia).not.toHaveBeenCalled();
    await userEvent.setup().click(screen.getByRole("button", { name: /Action required/ }));
    expect(screen.getByText("Set up your voice for hands-free Sam")).toBeInTheDocument();
  });

  it("a refused microphone fails closed with a clear action, and does not retry by itself", async () => {
    const fake = fakeAudioEnvironment();
    const refuse = vi.fn(async () => {
      throw Object.assign(new Error("x"), { name: "NotAllowedError" });
    });
    fake.env.getUserMedia = refuse;
    renderApp(
      mockBridge({ status: vi.fn(async () => handsFreeStatus), identityStatus: vi.fn(async () => enrolled) }),
      fake.env,
      null,
    );
    expect(await screen.findByText("Microphone access is off for Sam.")).toBeInTheDocument();
    expect(refuse).toHaveBeenCalledTimes(1);
    await new Promise((r) => setTimeout(r, 300));
    expect(refuse).toHaveBeenCalledTimes(1); // no silent retry loop
    await userEvent.setup().click(screen.getByRole("button", { name: /Action required/ }));
    expect(screen.getByText(/Microphone access is off \(System Settings/)).toBeInTheDocument();
  });

  it("leaving Home closes the microphone", async () => {
    const { fake } = setup();
    await waitFor(() => expect(state()).toBe("sleeping"));
    await userEvent.setup().click(screen.getByRole("button", { name: "Knowledge" }));
    expect(fake.track.stop).toHaveBeenCalled();
  });

  it("a hidden window closes the microphone", async () => {
    const { fake } = setup();
    await waitFor(() => expect(state()).toBe("sleeping"));
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(fake.track.stop).toHaveBeenCalled();
    visibility.mockRestore();
  });

  it("stays push-to-talk (mic closed) while voice activation is off", async () => {
    const { fake } = setup({ status: vi.fn(async () => ({ ...handsFreeStatus, voice_activation: "off" as const })) });
    expect(await screen.findByRole("button", { name: "Talk to Sam" })).toBeInTheDocument();
    expect(fake.getUserMedia).not.toHaveBeenCalled();
  });

  it("only the owner sees the switch, and it calls only the owner route", async () => {
    const { bridge } = setup({ status: vi.fn(async () => ({ ...handsFreeStatus, voice_activation: "off" as const })) });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Settings" }));
    await user.click(screen.getByRole("checkbox", { name: 'Listen for "Sam"' }));
    expect(bridge.setVoiceActivation).toHaveBeenCalledWith(true);
    expect(bridge.decideConfirmation).not.toHaveBeenCalled();
    expect(bridge.proactiveScheduler).not.toHaveBeenCalled();
  });

  it("a guest never sees the voice activation switch", async () => {
    setup({}, { ...enrolled, guest: { active: true, seconds_remaining: 300 } });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Settings" }));
    expect(screen.queryByRole("checkbox", { name: 'Listen for "Sam"' })).toBeNull();
  });
});

describe("a text draft never blocks voice", () => {
  const DRAFT = "half-written note: ask about the 3 PM review  (keep this exactly)";

  async function writeDraft(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: "Type to Sam" }));
    await user.type(screen.getByLabelText("Message Sam"), DRAFT);
    await user.keyboard("{Escape}"); // close the overlay without sending
    expect(screen.queryByRole("dialog", { name: "Type to Sam" })).toBeNull();
  }

  async function draftAfter(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole("button", { name: "Type to Sam" }));
    return (screen.getByLabelText("Message Sam") as HTMLTextAreaElement).value;
  }

  it("the wake word starts a conversation while a draft exists, and the draft is untouched", async () => {
    const utterance = vi.fn(async () => reply());
    const { bridge, say, finishSpeaking } = setup({
      voiceWake: vi.fn(async () => ({ ...ok, wake: true, followed: false })),
      voiceUtterance: utterance,
    });
    const user = userEvent.setup();
    await waitFor(() => expect(state()).toBe("sleeping"));
    await writeDraft(user);
    say(); // "Sam"
    await waitFor(() => expect(state()).toBe("attending"));
    say(8, true);
    await waitFor(() => expect(utterance).toHaveBeenCalledTimes(1));
    finishSpeaking();
    // The draft never reached the model or the voice request...
    expect(bridge.chat).not.toHaveBeenCalled();
    expect(JSON.stringify(utterance.mock.calls)).not.toContain("half-written");
    // ...nor the transcript...
    expect(screen.getByRole("region", { name: "Latest exchange" }).textContent).not.toContain("half-written");
    // ...and it is exactly as the owner left it.
    expect(await draftAfter(user)).toBe(DRAFT);
  });

  it("the manual voice fallback works while a draft exists", async () => {
    const { bridge } = setup();
    const user = userEvent.setup();
    await waitFor(() => expect(state()).toBe("sleeping"));
    await writeDraft(user);
    await user.click(screen.getByRole("button", { name: "Talk now" }));
    await waitFor(() => expect(state()).toBe("attending"));
    expect(bridge.chat).not.toHaveBeenCalled();
    expect(await draftAfter(user)).toBe(DRAFT);
  });

  it("ending the voice conversation never submits the draft", async () => {
    const { bridge, say } = setup({
      voiceWake: vi.fn(async () => ({ ...ok, wake: true, followed: false })),
      voiceUtterance: vi.fn(async () =>
        reply({ reason_code: "conversation_ended", message: "Okay.", transcript: null, reply: null }),
      ),
    });
    const user = userEvent.setup();
    await waitFor(() => expect(state()).toBe("sleeping"));
    await writeDraft(user);
    say();
    await waitFor(() => expect(state()).toBe("attending"));
    say(8, true); // "Sam, go to sleep"
    await waitFor(() => expect(state()).toBe("sleeping"));
    expect(bridge.chat).not.toHaveBeenCalled();
    expect(await draftAfter(user)).toBe(DRAFT);
  });

  it("an open text box does not stop Sam from hearing its name", async () => {
    const { say } = setup({ voiceWake: vi.fn(async () => ({ ...ok, wake: true, followed: false })) });
    const user = userEvent.setup();
    await waitFor(() => expect(state()).toBe("sleeping"));
    await user.click(screen.getByRole("button", { name: "Type to Sam" }));
    await user.type(screen.getByLabelText("Message Sam"), DRAFT);
    say();
    await waitFor(() => expect(state()).toBe("attending"));
    expect((screen.getByLabelText("Message Sam") as HTMLTextAreaElement).value).toBe(DRAFT);
  });

  it("security still applies: an unrecognized voice ends the conversation, draft or not", async () => {
    const { bridge, say } = setup({
      voiceWake: vi.fn(async () => ({ ...ok, wake: true, followed: false })),
      voiceUtterance: vi.fn(async () =>
        reply({
          status: "denied",
          reason_code: "owner_verification_required",
          message: "Sam didn't recognize your voice.",
          transcript: null,
          reply: null,
          forwarded_to_agent: false,
        }),
      ),
    });
    const user = userEvent.setup();
    await waitFor(() => expect(state()).toBe("sleeping"));
    await writeDraft(user);
    say();
    await waitFor(() => expect(state()).toBe("attending"));
    say(8, true);
    await waitFor(() => expect(state()).toBe("sleeping"));
    expect(bridge.chat).not.toHaveBeenCalled();
    expect(bridge.decideConfirmation).not.toHaveBeenCalled();
    expect(await draftAfter(user)).toBe(DRAFT);
  });

  it("the draft is never written to browser storage", async () => {
    setup();
    const user = userEvent.setup();
    await waitFor(() => expect(state()).toBe("sleeping"));
    await writeDraft(user);
    const dump = JSON.stringify({ ...window.localStorage }) + JSON.stringify({ ...window.sessionStorage });
    expect(dump).not.toContain("half-written");
  });
});
