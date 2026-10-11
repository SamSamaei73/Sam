import { afterEach, expect, it, vi } from "vitest";
import { playSynthesizedAudio } from "../lib/audioPlayback";

afterEach(() => vi.unstubAllGlobals());

it("reports rejected audio playback once and releases the in-memory clip", async () => {
  const revoke = vi.fn();
  vi.stubGlobal("URL", { createObjectURL: () => "blob:speech", revokeObjectURL: revoke });
  const pause = vi.fn();
  vi.stubGlobal("Audio", class {
    onended: (() => void) | null = null;
    onerror: (() => void) | null = null;
    play = () => Promise.reject(new Error("output unavailable"));
    pause = pause;
    removeAttribute = vi.fn();
  });
  const end = vi.fn();
  const playback = playSynthesizedAudio("SUQz", "mp3", end);
  await vi.waitFor(() => expect(end).toHaveBeenCalledExactlyOnceWith(true));
  playback.stop();
  expect(end).toHaveBeenCalledTimes(1);
  expect(revoke).toHaveBeenCalledExactlyOnceWith("blob:speech");
  expect(pause).toHaveBeenCalledTimes(1);
});
