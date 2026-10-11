import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { useEffect } from "react";
import type { SpeakResponse, VoiceResponse } from "../bridge/types";
import type { LocalSpeechOutput } from "../lib/localSpeech";
import { baseIdentity, baseStatus, fakeAudioEnvironment, mockBridge, ok, renderApp } from "./helpers";
import { SessionProvider, useSession } from "../session";
import { SamProvider } from "../state";

const play = vi.hoisted(() => vi.fn<(_audio: string, _format: string | null, end: (failed?: boolean) => void) => { stop: () => void }>());
vi.mock("../lib/audioPlayback", () => ({ playSynthesizedAudio: play }));

const response: VoiceResponse = {
  ...ok, transcript: "Please answer", reply: "Here is your answer.",
  speaker: "owner", speaker_result: "owner_verified", forwarded_to_agent: true,
  language: "en", direction: "ltr",
};

const permission: SpeakResponse = {
  ...ok, status: "confirmation_required", audio_base64: null, audio_format: null, byte_length: null,
  challenge: {
    confirmation_id: "confirm-speech", action: "speak", resource: "speech",
    scope: "default", risk: "medium", target: null, reason: null,
    expires_at: "2099-01-01T00:00:00Z",
  },
};

beforeEach(() => {
  play.mockReset();
  play.mockImplementation((_audio, _format, end) => ({ stop: () => end() }));
});

function ReplayControls() {
  const session = useSession();
  const { setHomeActive } = session;
  useEffect(() => { setHomeActive(true); return () => setHomeActive(false); }, [setHomeActive]);
  return <>
    <output data-testid="output-phase">{session.handsFree.phase}</output>
    <output data-testid="output-speaking">{session.speakingId}</output>
    <button onClick={() => session.exchange?.reply && void session.speak(session.exchange.reply)}>Replay</button>
  </>;
}

it("a delayed native stop callback cannot clear a replacement provider playback", async () => {
  const fake = fakeAudioEnvironment();
  let oldEnd: ((outcome?: "ended" | "stopped" | "unavailable" | "failed") => void) | undefined;
  const stopOld = vi.fn();
  const local: LocalSpeechOutput = { speak: (_text, _language, end) => { oldEnd = end; return { stop: stopOld }; } };
  const bridge = mockBridge({
    status: vi.fn(async () => ({ ...baseStatus, voice_input: "configured" as const, voice_activation: "on" as const,
      voice_identity: "configured" as const, speech_output: "configured" as const,
      speech_profiles: [{ profile_id: "sam_default", languages: ["en"] }] })),
    identityStatus: vi.fn(async () => ({ ...baseIdentity, available: true, enrolled: true, profile_compatible: true,
      setup_state: "enrolled" as const, step_up_configured: true, speaker_model: "configured" as const })),
    voiceWake: vi.fn(async () => ({ ...ok, wake: true, followed: true })),
    voiceUtterance: vi.fn(async () => response),
    speak: vi.fn(async () => ({ ...ok, audio_base64: "SUQz", audio_format: "mp3", byte_length: 3 })),
  });
  render(<SamProvider bridge={bridge} audioEnvironment={fake.env}><SessionProvider speech={local}><ReplayControls /></SessionProvider></SamProvider>);
  await waitFor(() => expect(screen.getByTestId("output-phase")).toHaveTextContent("sleeping"));
  act(() => {
    for (let i = 0; i < 6; i++) fake.emit(new Float32Array(1600).fill(0.15));
    for (let i = 0; i < 6; i++) fake.emit(new Float32Array(1600));
  });
  await waitFor(() => expect(screen.getByTestId("output-phase")).toHaveTextContent("speaking"));
  await userEvent.setup().click(screen.getByRole("button", { name: "Replay" }));
  await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
  expect(stopOld).toHaveBeenCalledTimes(1);
  const replacementId = screen.getByTestId("output-speaking").textContent;
  expect(replacementId).toBeTruthy();
  await act(async () => oldEnd?.("stopped"));
  expect(screen.getByTestId("output-speaking").textContent).toBe(replacementId);
  expect(screen.getByTestId("output-phase")).toHaveTextContent("speaking");
  act(() => {
    for (let i = 0; i < 16; i++) fake.emit(new Float32Array(1600).fill(0.4));
  });
  expect(bridge.voiceUtterance).toHaveBeenCalledTimes(1);
  await act(async () => play.mock.calls[0]?.[2]());
  expect(screen.getByTestId("output-phase")).toHaveTextContent("attending");
});

it("a push-to-talk answer is both written and spoken automatically", async () => {
  const fake = fakeAudioEnvironment();
  const spoken = vi.fn<LocalSpeechOutput["speak"]>((_text, _language, end) => ({ stop: () => end("stopped") }));
  const bridge = mockBridge({
    status: vi.fn(async () => ({ ...baseStatus, voice_input: "configured" as const })),
    voiceUtterance: vi.fn(async () => response),
  });
  renderApp(bridge, fake.env, { speak: spoken });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Talk to Sam" }));
  act(() => fake.emit(new Float32Array(3_200).fill(0.15)));
  await user.click(screen.getByRole("button", { name: "Stop recording and send" }));
  expect(await screen.findByText(response.reply!)).toBeInTheDocument();
  await waitFor(() => expect(spoken).toHaveBeenCalledWith(response.reply, "en", expect.any(Function)));
  expect(bridge.speak).not.toHaveBeenCalled();
  act(() => spoken.mock.calls[0]?.[2]("ended"));
});

async function startPersianTurn(speech: NonNullable<Parameters<typeof mockBridge>[0]>["speak"]) {
  const fake = fakeAudioEnvironment();
  const bridge = mockBridge({
    status: vi.fn(async () => ({
      ...baseStatus, voice_input: "configured" as const, voice_activation: "on" as const,
      voice_identity: "configured" as const, speech_output: "configured" as const,
      speech_profiles: [{ profile_id: "sam_persian", languages: ["fa" as const] }],
    })),
    identityStatus: vi.fn(async () => ({
      ...baseIdentity, available: true, enrolled: true, profile_compatible: true,
      setup_state: "enrolled" as const, step_up_configured: true, speaker_model: "configured" as const,
    })),
    voiceWake: vi.fn(async () => ({ ...ok, wake: true, followed: true })),
    voiceUtterance: vi.fn(async () => ({ ...response, reply: "سلام، پاسخ شما آماده است.", language: "fa" as const, direction: "rtl" as const })),
    speak: speech,
  });
  renderApp(bridge, fake.env, { speak: (_text, _language, end) => { end("unavailable"); return null; } });
  await waitFor(() => expect(document.querySelector(".presence")?.getAttribute("data-state")).toBe("sleeping"));
  act(() => {
    for (let i = 0; i < 6; i++) fake.emit(new Float32Array(1_600).fill(0.15));
    for (let i = 0; i < 6; i++) fake.emit(new Float32Array(1_600));
  });
  return bridge;
}

it("a missing Persian voice asks through the existing speech permission flow", async () => {
  const bridge = await startPersianTurn(vi.fn(async () => permission));
  expect(await screen.findByText("سلام، پاسخ شما آماده است.")).toBeInTheDocument();
  const dialog = await screen.findByRole("alertdialog");
  expect(play).not.toHaveBeenCalled();
  expect(bridge.decideConfirmation).not.toHaveBeenCalled();
  await userEvent.setup().click(within(dialog).getByRole("button", { name: "Deny" }));
  await waitFor(() => expect(document.querySelector(".presence")?.getAttribute("data-state")).toBe("attending"));
  expect(bridge.speak).toHaveBeenCalledTimes(1);
  expect(play).not.toHaveBeenCalled();
});

it("approved Persian output keeps the listener paused until playback finishes", async () => {
  const bridge = await startPersianTurn(vi.fn(async (input) => input.confirmationId
    ? { ...ok, audio_base64: "SUQz", audio_format: "mp3", byte_length: 3 }
    : permission));
  const dialog = await screen.findByRole("alertdialog");
  await userEvent.setup().click(within(dialog).getByRole("button", { name: "Approve" }));
  await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
  expect(bridge.speak).toHaveBeenLastCalledWith({
    text: "سلام، پاسخ شما آماده است.", voiceProfile: "sam_persian", language: "fa", confirmationId: "confirm-speech",
  });
  expect(document.querySelector(".presence")?.getAttribute("data-state")).toBe("speaking");
  act(() => play.mock.calls[0]?.[2]());
  await waitFor(() => expect(document.querySelector(".presence")?.getAttribute("data-state")).toBe("attending"));
});

it("a playback failure is visible while the written answer remains", async () => {
  await startPersianTurn(vi.fn(async () => ({ ...ok, audio_base64: "SUQz", audio_format: "mp3", byte_length: 3 })));
  await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
  act(() => play.mock.calls[0]?.[2](true));
  expect(await screen.findByText("Sam couldn't play this reply. Check your sound output.")).toBeInTheDocument();
  expect(screen.getByText("سلام، پاسخ شما آماده است.")).toBeInTheDocument();
});
