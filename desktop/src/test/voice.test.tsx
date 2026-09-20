import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { baseStatus, fakeAudioEnvironment, mockBridge, ok, renderApp } from "./helpers";

const voiceStatus = { ...baseStatus, voice_input: "configured" as const };
const speechStatus = {
  ...baseStatus,
  speech_output: "configured" as const,
  speech_profiles: [{ profile_id: "sam_default" }],
};

describe("voice input", () => {
  it("is disabled and explained when not configured", async () => {
    const fake = fakeAudioEnvironment();
    renderApp(mockBridge(), fake.env);
    await screen.findByLabelText("Connection: Connected");
    expect(screen.getByRole("button", { name: "Voice input (not configured)" })).toBeDisabled();
    expect(fake.getUserMedia).not.toHaveBeenCalled();
  });

  it("never starts the microphone on load", async () => {
    const fake = fakeAudioEnvironment();
    renderApp(mockBridge({ status: vi.fn(async () => voiceStatus) }), fake.env);
    await screen.findByRole("button", { name: "Record a voice message" });
    expect(fake.getUserMedia).not.toHaveBeenCalled();
  });

  it("records only after a click, shows an indicator, and stops tracks on stop", async () => {
    const fake = fakeAudioEnvironment();
    const bridge = mockBridge({
      status: vi.fn(async () => voiceStatus),
      voiceUtterance: vi.fn(async () => ({
        ...ok,
        transcript: "what's the weather",
        forwarded_to_agent: true,
        reply: "Sunny.",
      })),
    });
    renderApp(bridge, fake.env);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Record a voice message" }));
    expect(fake.getUserMedia).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/Recording/)).toBeInTheDocument();
    act(() => fake.emit(new Float32Array(3200).fill(0.1)));
    await user.click(screen.getByRole("button", { name: "Stop recording and send" }));
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalled());
    expect(fake.track.stop).toHaveBeenCalled();
    const audio = (bridge.voiceUtterance as ReturnType<typeof vi.fn>).mock.calls[0]?.[0] as string;
    expect(atob(audio).startsWith("RIFF")).toBe(true);
    const log = screen.getByRole("log");
    expect(await within(log).findByText("what's the weather")).toBeInTheDocument();
    expect(await screen.findByText("Sunny.")).toBeInTheDocument();
    expect(screen.queryByText(/Recording/)).toBeNull();
  });

  it("cancel discards the audio and releases the microphone", async () => {
    const fake = fakeAudioEnvironment();
    const bridge = mockBridge({ status: vi.fn(async () => voiceStatus) });
    renderApp(bridge, fake.env);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Record a voice message" }));
    await user.click(await screen.findByRole("button", { name: "Cancel recording" }));
    expect(fake.track.stop).toHaveBeenCalled();
    expect(bridge.voiceUtterance).not.toHaveBeenCalled();
  });

  it("shows a safe message when microphone access is denied", async () => {
    const fake = fakeAudioEnvironment();
    fake.env.getUserMedia = vi.fn(async () => {
      throw Object.assign(new Error("x"), { name: "NotAllowedError" });
    });
    renderApp(mockBridge({ status: vi.fn(async () => voiceStatus) }), fake.env);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Record a voice message" }));
    expect(await screen.findByText("Microphone access was declined.")).toBeInTheDocument();
  });

  it("does not display or forward a withheld (secret) transcript", async () => {
    const fake = fakeAudioEnvironment();
    const bridge = mockBridge({
      status: vi.fn(async () => voiceStatus),
      voiceUtterance: vi.fn(async () => ({
        ...ok,
        status: "rejected" as const,
        reason_code: "secret_detected",
        message: "That looked like it contained a secret, so it was withheld.",
        transcript: null,
        forwarded_to_agent: false,
        reply: null,
      })),
    });
    renderApp(bridge, fake.env);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Record a voice message" }));
    act(() => fake.emit(new Float32Array(1600).fill(0.1)));
    await user.click(screen.getByRole("button", { name: "Stop recording and send" }));
    expect(await screen.findByText(/withheld/)).toBeInTheDocument();
    expect(bridge.chat).not.toHaveBeenCalled();
  });

  it("releases the microphone if the view is left mid-recording", async () => {
    const fake = fakeAudioEnvironment();
    renderApp(mockBridge({ status: vi.fn(async () => voiceStatus) }), fake.env);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Record a voice message" }));
    await user.click(screen.getByRole("button", { name: "Knowledge" }));
    expect(fake.track.stop).toHaveBeenCalled();
  });
});

describe("read aloud", () => {
  const created: string[] = [];
  const revoked: string[] = [];
  afterEach(() => {
    created.length = 0;
    revoked.length = 0;
    vi.unstubAllGlobals();
  });

  function stubAudio() {
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => {
        const url = `blob:test-${created.length}`;
        created.push(url);
        return url;
      }),
      revokeObjectURL: vi.fn((url: string) => revoked.push(url)),
    });
    const instances: { onended: null | (() => void) }[] = [];
    class FakeAudio {
      onended: null | (() => void) = null;
      onerror: null | (() => void) = null;
      constructor(public src: string) {
        instances.push(this);
      }
      play = vi.fn(async () => undefined);
      pause = vi.fn();
      removeAttribute = vi.fn();
    }
    vi.stubGlobal("Audio", FakeAudio);
    return instances;
  }

  it("is absent when speech output isn't configured", async () => {
    renderApp(mockBridge());
    const user = userEvent.setup();
    await screen.findByLabelText("Connection: Connected");
    await user.type(screen.getByLabelText("Message Sam"), "hi");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText("Hello from Sam");
    expect(screen.queryByRole("button", { name: "Read aloud" })).toBeNull();
  });

  it("never auto-plays: nothing is synthesized until the button is clicked", async () => {
    const bridge = mockBridge({ status: vi.fn(async () => speechStatus) });
    renderApp(bridge);
    const user = userEvent.setup();
    await screen.findByLabelText("Connection: Connected");
    await user.type(screen.getByLabelText("Message Sam"), "hi");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("button", { name: "Read aloud" });
    expect(bridge.speak).not.toHaveBeenCalled();
  });

  it("sends only text and a trusted profile id, plays from memory and revokes the URL", async () => {
    const instances = stubAudio();
    const bridge = mockBridge({
      status: vi.fn(async () => speechStatus),
      speak: vi.fn(async () => ({ ...ok, audio_base64: btoa("ID3fake"), audio_format: "mp3", byte_length: 7 })),
    });
    renderApp(bridge);
    const user = userEvent.setup();
    await screen.findByLabelText("Connection: Connected");
    await user.type(screen.getByLabelText("Message Sam"), "hi");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await user.click(await screen.findByRole("button", { name: "Read aloud" }));
    await waitFor(() => expect(bridge.speak).toHaveBeenCalled());
    expect(bridge.speak).toHaveBeenCalledWith({
      text: "Hello from Sam",
      voiceProfile: "sam_default",
      confirmationId: undefined,
    });
    expect(created).toHaveLength(1);
    await waitFor(() => expect(instances).toHaveLength(1));
    act(() => instances[0]?.onended?.());
    expect(revoked).toEqual(created);
    expect(window.localStorage.length).toBe(0);
  });

  it("shows a safe message when synthesis fails", async () => {
    const bridge = mockBridge({
      status: vi.fn(async () => speechStatus),
      speak: vi.fn(async () => ({
        ...ok,
        status: "failed" as const,
        message: "The voice service timed out.",
        audio_base64: null,
        audio_format: null,
        byte_length: null,
      })),
    });
    renderApp(bridge);
    const user = userEvent.setup();
    await screen.findByLabelText("Connection: Connected");
    await user.type(screen.getByLabelText("Message Sam"), "hi");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await user.click(await screen.findByRole("button", { name: "Read aloud" }));
    expect(await screen.findByText("The voice service timed out.")).toBeInTheDocument();
  });
});
