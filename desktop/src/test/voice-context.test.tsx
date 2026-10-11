import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SamBridge } from "../bridge/bridge";
import type { VoiceResponse } from "../bridge/types";
import type { LocalSpeechOutput } from "../lib/localSpeech";
import { SessionProvider, useSession } from "../session";
import { SamProvider } from "../state";
import { baseIdentity, baseStatus, fakeAudioEnvironment, mockBridge, ok } from "./helpers";

const CONTEXT_ID = "a".repeat(32);
const FRAME = 1_600;
const response: VoiceResponse = {
  ...ok,
  transcript: "Please remember the blue example for this conversation.",
  reply: "I can use that example for your next question.",
  forwarded_to_agent: true,
  speaker: "owner",
  speaker_result: "owner_verified",
  language: "en",
  direction: "ltr",
  conversation_id: CONTEXT_ID,
};

/** Exercise the session lifecycle directly; the App tests cover its controls. */
function SessionControls() {
  const session = useSession();
  const { setHomeActive } = session;
  useEffect(() => {
    setHomeActive(true);
    return () => setHomeActive(false);
  }, [setHomeActive]);
  return <>
    <output data-testid="phase">{session.handsFree.phase}</output>
    <output data-testid="reply">{session.exchange?.reply?.text}</output>
    <button onClick={session.sleepNow}>Sleep</button>
    <button onClick={session.wakeNow}>Wake</button>
    <button onClick={session.clearActive}>Clear</button>
    <button onClick={() => setHomeActive(false)}>Leave Home</button>
  </>;
}

function setup(utterance = vi.fn<SamBridge["voiceUtterance"]>().mockResolvedValue(response)) {
  const fake = fakeAudioEnvironment();
  const bridge = mockBridge({
    status: vi.fn(async () => ({
      ...baseStatus,
      voice_input: "configured" as const,
      voice_identity: "configured" as const,
      voice_activation: "on" as const,
    })),
    identityStatus: vi.fn(async () => ({
      ...baseIdentity,
      available: true,
      enrolled: true,
      profile_compatible: true,
      setup_state: "enrolled" as const,
      step_up_configured: true,
      speaker_model: "configured" as const,
    })),
    voiceWake: vi.fn(async () => ({ ...ok, wake: true, followed: false })),
    voiceUtterance: utterance,
  });
  const endings: (() => void)[] = [];
  const spoken = vi.fn<LocalSpeechOutput["speak"]>((_text, _language, end) => {
    endings.push(() => end("ended"));
    return { stop: () => end("stopped") };
  });
  render(
    <SamProvider bridge={bridge} audioEnvironment={fake.env}>
      <SessionProvider speech={{ speak: spoken }}><SessionControls /></SessionProvider>
    </SamProvider>,
  );
  const say = (level = 0.2) => act(() => {
    for (let i = 0; i < 16; i++) fake.emit(new Float32Array(FRAME).fill(level));
    for (let i = 0; i < 12; i++) fake.emit(new Float32Array(FRAME));
  });
  const phase = () => screen.getByTestId("phase").textContent;
  const wake = async () => {
    await waitFor(() => expect(phase()).toBe("sleeping"));
    say();
    await waitFor(() => expect(phase()).toBe("attending"));
  };
  const finishReply = async () => {
    await waitFor(() => expect(phase()).toBe("speaking"));
    await act(async () => endings.shift()?.());
    await waitFor(() => expect(phase()).toBe("attending"));
  };
  return { bridge, utterance, spoken, say, phase, wake, finishReply };
}

afterEach(() => vi.restoreAllMocks());

describe("temporary voice conversation context", () => {
  it("reuses context after playback while sending fresh audio for every verification", async () => {
    const { bridge, utterance, spoken, say, wake, finishReply } = setup();
    await wake();
    say(0.2);
    await waitFor(() => expect(spoken).toHaveBeenCalledTimes(1));
    expect(utterance.mock.calls[0]?.[4]).toBeUndefined();
    say(0.4); // Sam's reply must not become the next utterance.
    expect(utterance).toHaveBeenCalledTimes(1);
    await finishReply();
    say(-0.25);
    await waitFor(() => expect(utterance).toHaveBeenCalledTimes(2));
    const first = utterance.mock.calls[0]!;
    const followup = utterance.mock.calls[1]!;
    expect(followup[0]).not.toBe(first[0]);
    expect(followup.slice(1)).toEqual([undefined, "auto", true, CONTEXT_ID]);
    expect(bridge.voiceWake).toHaveBeenCalledTimes(1);
    expect(bridge.chat).not.toHaveBeenCalled();
    expect(bridge.decideConfirmation).not.toHaveBeenCalled();
    expect(JSON.stringify(window.localStorage)).not.toContain(CONTEXT_ID);
    expect(JSON.stringify(window.sessionStorage)).not.toContain(CONTEXT_ID);
  });

  it.each(["hide", "sleep", "navigation", "clear"] as const)("erases the context on %s", async (action) => {
    const { bridge, say, wake, finishReply } = setup();
    await wake();
    say();
    await finishReply();
    bridge.endVoiceConversation.mockClear();
    if (action === "hide") {
      vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
      act(() => document.dispatchEvent(new Event("visibilitychange")));
    } else {
      await userEvent.setup().click(screen.getByRole("button", {
        name: { sleep: "Sleep", navigation: "Leave Home", clear: "Clear" }[action],
      }));
    }
    await waitFor(() => expect(bridge.endVoiceConversation).toHaveBeenCalledWith(CONTEXT_ID));
  });

  it("a rejected follow-up clears context and the next wake starts without it", async () => {
    const utterance = vi.fn<SamBridge["voiceUtterance"]>()
      .mockResolvedValueOnce(response)
      .mockResolvedValueOnce({
        ...response, status: "denied", reason_code: "owner_verification_required",
        message: "Sam didn't recognize your voice.", transcript: null, reply: null,
        forwarded_to_agent: false, speaker: null, speaker_result: "owner_rejected", conversation_id: null,
      })
      .mockResolvedValue(response);
    const { bridge, say, wake, finishReply, phase } = setup(utterance);
    await wake();
    say();
    await finishReply();
    bridge.endVoiceConversation.mockClear();
    say(-0.2);
    await waitFor(() => expect(phase()).toBe("sleeping"));
    expect(utterance.mock.calls[1]?.[4]).toBe(CONTEXT_ID);
    await waitFor(() => expect(bridge.endVoiceConversation).toHaveBeenCalled());
    await userEvent.setup().click(screen.getByRole("button", { name: "Wake" }));
    say(0.3);
    await waitFor(() => expect(utterance).toHaveBeenCalledTimes(3));
    expect(utterance.mock.calls[2]?.[4]).toBeUndefined();
    expect(bridge.chat).not.toHaveBeenCalled();
  });

  it("a late response after hiding cannot revive its context or start speech", async () => {
    let release: ((result: VoiceResponse) => void) | undefined;
    const utterance = vi.fn<SamBridge["voiceUtterance"]>(() => new Promise((resolve) => { release = resolve; }));
    const { bridge, spoken, say, wake, phase } = setup(utterance);
    await wake();
    say();
    await waitFor(() => expect(utterance).toHaveBeenCalledTimes(1));
    bridge.endVoiceConversation.mockClear();
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    // With no ID yet, cancel all pending context for this principal. The
    // backend generation check prevents the in-flight turn committing later.
    await waitFor(() => expect(bridge.endVoiceConversation).toHaveBeenCalledWith(undefined));
    await act(async () => release?.(response));
    expect(spoken).not.toHaveBeenCalled();
    expect(screen.getByTestId("reply")).toBeEmptyDOMElement();
    visibility.mockReturnValue("visible");
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() => expect(phase()).toBe("sleeping"));
    await userEvent.setup().click(screen.getByRole("button", { name: "Wake" }));
    say(-0.3);
    await waitFor(() => expect(utterance).toHaveBeenCalledTimes(2));
    expect(utterance.mock.calls[1]?.[4]).toBeUndefined();
  });
});
