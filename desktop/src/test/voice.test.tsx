import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { baseIdentity, baseStatus, fakeAudioEnvironment, mockBridge, ok, renderApp } from "./helpers";

const voiceStatus = { ...baseStatus, voice_input: "configured" as const };
const speechStatus = {
  ...baseStatus,
  speech_output: "configured" as const,
  speech_profiles: [{ profile_id: "sam_default", languages: ["en" as const] }],
};

describe("voice input", () => {
  it("is disabled and explained when not configured", async () => {
    const fake = fakeAudioEnvironment();
    renderApp(mockBridge(), fake.env);
    await screen.findByLabelText("Connection: Connected");
    expect(screen.getByRole("button", { name: /Voice input isn't configured/ })).toBeDisabled();
    expect(fake.getUserMedia).not.toHaveBeenCalled();
  });

  it("never starts the microphone on load", async () => {
    const fake = fakeAudioEnvironment();
    renderApp(mockBridge({ status: vi.fn(async () => voiceStatus) }), fake.env);
    await screen.findByRole("button", { name: "Talk to Sam" });
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
        speaker: "owner" as const,
        speaker_result: "owner_verified",
        language: "en" as const,
        direction: "ltr" as const,
      })),
    });
    renderApp(bridge, fake.env);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Talk to Sam" }));
    expect(fake.getUserMedia).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/Recording/)).toBeInTheDocument();
    act(() => fake.emit(new Float32Array(3200).fill(0.1)));
    await user.click(screen.getByRole("button", { name: "Stop recording and send" }));
    await waitFor(() => expect(bridge.voiceUtterance).toHaveBeenCalled());
    expect(fake.track.stop).toHaveBeenCalled();
    const audio = (bridge.voiceUtterance as ReturnType<typeof vi.fn>).mock.calls[0]?.[0] as string;
    expect(atob(audio).startsWith("RIFF")).toBe(true);
    const latest = screen.getByRole("region", { name: "Latest exchange" });
    expect(await within(latest).findByText("what's the weather")).toBeInTheDocument();
    expect(await within(latest).findByText("Sunny.")).toBeInTheDocument();
    expect(screen.queryByText(/Recording/)).toBeNull();
    await user.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("button", { name: "History" }));
    expect(within(screen.getByRole("log")).getByText("what's the weather")).toBeInTheDocument();
  });

  it("cancel discards the audio and releases the microphone", async () => {
    const fake = fakeAudioEnvironment();
    const bridge = mockBridge({ status: vi.fn(async () => voiceStatus) });
    renderApp(bridge, fake.env);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Talk to Sam" }));
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
    await user.click(await screen.findByRole("button", { name: "Talk to Sam" }));
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
        speaker: null,
        speaker_result: null,
        language: null,
        direction: null,
      })),
    });
    renderApp(bridge, fake.env);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Talk to Sam" }));
    act(() => fake.emit(new Float32Array(1600).fill(0.1)));
    await user.click(screen.getByRole("button", { name: "Stop recording and send" }));
    expect(await screen.findByText(/withheld/)).toBeInTheDocument();
    expect(bridge.chat).not.toHaveBeenCalled();
  });

  it("releases the microphone if the view is left mid-recording", async () => {
    const fake = fakeAudioEnvironment();
    renderApp(mockBridge({ status: vi.fn(async () => voiceStatus) }), fake.env);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Talk to Sam" }));
    await user.click(await screen.findByRole("button", { name: "Knowledge" }));
    expect(fake.track.stop).toHaveBeenCalled();
  });
});

describe("Sam presence follows real state", () => {
  const presence = () => document.querySelector(".presence") as HTMLElement;

  it("is idle, then listening with the real mic level, then thinking, then idle", async () => {
    const fake = fakeAudioEnvironment();
    let release: (value: unknown) => void = () => undefined;
    const bridge = mockBridge({
      status: vi.fn(async () => voiceStatus),
      voiceUtterance: vi.fn(() => new Promise((resolve) => (release = resolve))) as never,
    });
    renderApp(bridge, fake.env);
    const user = userEvent.setup();
    await screen.findByRole("img", { name: "Sam is ready" });
    expect(presence().dataset.state).toBe("idle");
    expect(presence().style.getPropertyValue("--level")).toBe("0.000");

    await user.click(await screen.findByRole("button", { name: "Talk to Sam" }));
    await waitFor(() => expect(presence().dataset.state).toBe("listening"));
    expect(screen.getByText("Listening")).toBeInTheDocument();
    act(() => fake.emit(new Float32Array(1600).fill(0.1)));
    await waitFor(() => expect(Number(presence().style.getPropertyValue("--level"))).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: "Stop recording and send" }));
    await waitFor(() => expect(presence().dataset.state).toBe("thinking"));
    expect(screen.getByRole("img", { name: "Sam is thinking" })).toBeInTheDocument();
    expect(presence().style.getPropertyValue("--level")).toBe("0.000");
    // Nothing can start a new capture while Sam is thinking.
    expect(screen.getByRole("button", { name: "Talk to Sam" })).toBeDisabled();

    act(() =>
      release({
        ...ok,
        transcript: "hello",
        forwarded_to_agent: true,
        reply: "Hi.",
        speaker: "owner",
        speaker_result: "owner_verified",
        language: "en",
        direction: "ltr",
      }),
    );
    await waitFor(() => expect(presence().dataset.state).toBe("idle"));
  });

  it("shows the error state briefly when the microphone is refused", async () => {
    const fake = fakeAudioEnvironment();
    fake.env.getUserMedia = vi.fn(async () => {
      throw Object.assign(new Error("x"), { name: "NotAllowedError" });
    });
    renderApp(mockBridge({ status: vi.fn(async () => voiceStatus) }), fake.env);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Talk to Sam" }));
    await waitFor(() => expect(presence().dataset.state).toBe("error"));
  });

  it("marks Guest Mode on the presence without implying the owner", async () => {
    renderApp(
      mockBridge({
        identityStatus: vi.fn(async () => ({
          ...baseIdentity,
          available: true,
          enrolled: true,
          guest: { active: true, seconds_remaining: 120 },
        })),
      }),
    );
    await waitFor(() => expect(presence().dataset.guest).toBe("true"));
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
    await user.click(screen.getByRole("button", { name: "Type to Sam" }));
    await user.type(screen.getByLabelText("Message Sam"), "hi");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText("Hello from Sam");
    expect(screen.queryByRole("button", { name: "Speak reply" })).toBeNull();
  });

  it("never auto-plays: nothing is synthesized until the button is clicked", async () => {
    const bridge = mockBridge({ status: vi.fn(async () => speechStatus) });
    renderApp(bridge);
    const user = userEvent.setup();
    await screen.findByLabelText("Connection: Connected");
    await user.click(screen.getByRole("button", { name: "Type to Sam" }));
    await user.type(screen.getByLabelText("Message Sam"), "hi");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("button", { name: "Speak reply" });
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
    await user.click(screen.getByRole("button", { name: "Type to Sam" }));
    await user.type(screen.getByLabelText("Message Sam"), "hi");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await user.click(await screen.findByRole("button", { name: "Speak reply" }));
    await waitFor(() => expect(bridge.speak).toHaveBeenCalled());
    expect(bridge.speak).toHaveBeenCalledWith({
      text: "Hello from Sam",
      voiceProfile: "sam_default",
      language: "en",
      confirmationId: undefined,
    });
    expect(created).toHaveLength(1);
    await waitFor(() => expect(instances).toHaveLength(1));
    await screen.findByRole("img", { name: "Sam is speaking" });
    act(() => instances[0]?.onended?.());
    await screen.findByRole("img", { name: "Sam is ready" });
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
    await user.click(screen.getByRole("button", { name: "Type to Sam" }));
    await user.type(screen.getByLabelText("Message Sam"), "hi");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await user.click(await screen.findByRole("button", { name: "Speak reply" }));
    expect(await screen.findByText("The voice service timed out.")).toBeInTheDocument();
  });
});
