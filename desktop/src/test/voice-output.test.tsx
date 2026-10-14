import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { useEffect } from "react";
import type { SpeechPlaybackResponse, VoiceResponse } from "../bridge/types";
import type { SamBridge } from "../bridge/bridge";
import { baseIdentity, baseStatus, fakeAudioEnvironment, mockBridge, ok, renderApp } from "./helpers";
import { SessionProvider, useSession } from "../session";
import { SamProvider } from "../state";

const play = vi.hoisted(() => vi.fn<(_audio: string, _format: string | null, end: (failed?: boolean) => void) => { stop: () => void }>());
vi.mock("../lib/audioPlayback", () => ({ playSynthesizedAudio: play }));

const response: VoiceResponse = {
  ...ok, transcript: "Please answer", reply: "Here is your answer.",
  speaker: "owner", speaker_result: "owner_verified", forwarded_to_agent: true,
  language: "en", direction: "ltr",
  speech_id: "reply-ticket",
};

const localDone: SpeechPlaybackResponse = { ...ok, kind: "local", audio_base64: null, audio_format: null, byte_length: null };
const audioReady: SpeechPlaybackResponse = { ...ok, kind: "audio", audio_base64: "SUQz", audio_format: "mp3", byte_length: 3 };

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
  let oldEnd: ((response: SpeechPlaybackResponse) => void) | undefined;
  const stopOld = vi.fn();
  const bridge = mockBridge({
    status: vi.fn(async () => ({ ...baseStatus, voice_input: "configured" as const, voice_activation: "on" as const,
      voice_identity: "configured" as const, speech_output: "configured" as const,
      speech_profiles: [{ profile_id: "sam_default", languages: ["en"] }] })),
    identityStatus: vi.fn(async () => ({ ...baseIdentity, available: true, enrolled: true, profile_compatible: true,
      setup_state: "enrolled" as const, step_up_configured: true, speaker_model: "configured" as const })),
    voiceWake: vi.fn(async () => ({ ...ok, wake: true, owner_verified: true, followed: true })),
    voiceUtterance: vi.fn(async () => response),
    playSamSpeech: vi.fn(() => new Promise<SpeechPlaybackResponse>((resolve) => { oldEnd = resolve; })),
    stopSamSpeech: vi.fn(async () => { stopOld(); }),
    speak: vi.fn(async () => ({ ...ok, audio_base64: "SUQz", audio_format: "mp3", byte_length: 3 })),
  });
  render(<SamProvider bridge={bridge} audioEnvironment={fake.env}><SessionProvider speech={null}><ReplayControls /></SessionProvider></SamProvider>);
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
  await act(async () => oldEnd?.(localDone));
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
  let finish!: (response: SpeechPlaybackResponse) => void;
  const bridge = mockBridge({
    status: vi.fn(async () => ({ ...baseStatus, voice_input: "configured" as const })),
    voiceUtterance: vi.fn(async () => response),
    playSamSpeech: vi.fn(() => new Promise<SpeechPlaybackResponse>((resolve) => { finish = resolve; })),
  });
  renderApp(bridge, fake.env, null);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Talk to Sam" }));
  act(() => fake.emit(new Float32Array(3_200).fill(0.15)));
  await user.click(screen.getByRole("button", { name: "Stop recording and send" }));
  expect(await screen.findByText(response.reply!)).toBeInTheDocument();
  await waitFor(() => expect(bridge.playSamSpeech).toHaveBeenCalledWith("reply-ticket"));
  expect(document.querySelector(".presence")?.getAttribute("data-state")).toBe("speaking");
  expect(bridge.speak).not.toHaveBeenCalled();
  await act(async () => finish(localDone));
  await waitFor(() => expect(document.querySelector(".presence")?.getAttribute("data-state")).toBe("idle"));
});

async function startPersianTurn(speech: SamBridge["playSamSpeech"], unavailable = false) {
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
    voiceWake: vi.fn(async () => ({ ...ok, wake: true, owner_verified: true, followed: true })),
    voiceUtterance: vi.fn(async () => ({ ...response, reply: "سلام، پاسخ شما آماده است.", language: "fa" as const, direction: "rtl" as const,
      speech_id: unavailable ? null : "reply-ticket", speech_reason: unavailable ? "speech_language_unavailable" : null })),
    playSamSpeech: speech,
  });
  renderApp(bridge, fake.env, { speak: (_text, _language, end) => { end("unavailable"); return null; } });
  await waitFor(() => expect(document.querySelector(".presence")?.getAttribute("data-state")).toBe("sleeping"));
  act(() => {
    for (let i = 0; i < 6; i++) fake.emit(new Float32Array(1_600).fill(0.15));
    for (let i = 0; i < 6; i++) fake.emit(new Float32Array(1_600));
  });
  return bridge;
}

it("a missing Persian voice preserves the answer and never silently enables an external provider", async () => {
  const bridge = await startPersianTurn(vi.fn(async () => audioReady), true);
  expect(await screen.findByText("سلام، پاسخ شما آماده است.")).toBeInTheDocument();
  expect(play).not.toHaveBeenCalled();
  expect(bridge.decideConfirmation).not.toHaveBeenCalled();
  await waitFor(() => expect(document.querySelector(".presence")?.getAttribute("data-state")).toBe("attending"));
  expect(bridge.speak).not.toHaveBeenCalled();
  expect(bridge.playSamSpeech).not.toHaveBeenCalled();
  expect(bridge.setSamVoice).not.toHaveBeenCalled();
});

it("approved Persian output keeps the listener paused until playback finishes", async () => {
  const bridge = await startPersianTurn(vi.fn(async () => audioReady));
  await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
  expect(bridge.playSamSpeech).toHaveBeenLastCalledWith("reply-ticket");
  expect(bridge.speak).not.toHaveBeenCalled();
  expect(document.querySelector(".presence")?.getAttribute("data-state")).toBe("speaking");
  act(() => play.mock.calls[0]?.[2]());
  await waitFor(() => expect(document.querySelector(".presence")?.getAttribute("data-state")).toBe("attending"));
});

it("a playback failure is visible while the written answer remains", async () => {
  await startPersianTurn(vi.fn(async () => audioReady));
  await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
  act(() => play.mock.calls[0]?.[2](true));
  expect((await screen.findAllByText("Sam couldn't play speech. Check the sound output and try Preview Sam's voice.")).length).toBeGreaterThan(0);
  expect(screen.getByText("سلام، پاسخ شما آماده است.")).toBeInTheDocument();
});
