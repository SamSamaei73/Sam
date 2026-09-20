import { useEffect, useRef, useState } from "react";
import type { SamBridge } from "../bridge/bridge";
import type { SpeakResponse } from "../bridge/types";
import { playSynthesizedAudio, type Playback } from "../lib/audioPlayback";
import { IconSpeaker, IconStop } from "./Icons";
import { IconButton } from "./primitives";

export const READ_ALOUD_MAX_CHARS = 20_000;

/**
 * Explicit read-aloud. Only fires from a click; sends the message text plus a
 * *trusted profile id* the backend advertised. Never automatic.
 */
export function ReadAloud({
  bridge,
  text,
  profile,
  language,
  confirm,
  onError,
}: {
  bridge: SamBridge;
  text: string;
  profile: string;
  language: "fa" | "en";
  confirm: (run: (confirmationId?: string) => Promise<SpeakResponse>) => Promise<SpeakResponse>;
  onError: (message: string) => void;
}) {
  const [state, setState] = useState<"idle" | "loading" | "playing">("idle");
  const playback = useRef<Playback | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      playback.current?.stop();
    };
  }, []);

  const stop = () => {
    playback.current?.stop();
    playback.current = null;
    setState("idle");
  };

  const start = async () => {
    setState("loading");
    try {
      const result = await confirm((confirmationId) =>
        bridge.speak({ text, voiceProfile: profile, language, confirmationId }),
      );
      if (!mounted.current) return;
      if (result.status !== "ok" || !result.audio_base64) {
        setState("idle");
        onError(result.message ?? "Sam couldn't read that aloud.");
        return;
      }
      setState("playing");
      playback.current = playSynthesizedAudio(result.audio_base64, result.audio_format, () => {
        playback.current = null;
        if (mounted.current) setState("idle");
      });
    } catch {
      if (mounted.current) setState("idle");
      onError("Sam couldn't read that aloud.");
    }
  };

  if (text.length > READ_ALOUD_MAX_CHARS) return null;
  return state === "playing" ? (
    <IconButton label="Stop reading aloud" onClick={stop}>
      <IconStop />
    </IconButton>
  ) : (
    <IconButton label="Read aloud" disabled={state === "loading"} onClick={() => void start()}>
      <IconSpeaker />
    </IconButton>
  );
}
