import { base64ToBytes } from "./wav";

const ALLOWED_TYPES: Record<string, string> = {
  mp3: "audio/mpeg",
  wav: "audio/wav",
  opus: "audio/ogg",
  pcm: "audio/wav",
};

/**
 * In-memory playback of a synthesized clip. The clip is a Blob behind an
 * object URL that is revoked as soon as playback ends, fails, or is stopped.
 * Nothing is saved to disk or storage.
 */
export interface Playback {
  stop(): void;
}

export function playSynthesizedAudio(
  audioBase64: string,
  format: string | null,
  onEnd: (failed?: boolean) => void,
): Playback {
  const type = ALLOWED_TYPES[(format ?? "").toLowerCase()] ?? "audio/mpeg";
  const url = URL.createObjectURL(new Blob([base64ToBytes(audioBase64) as Uint8Array<ArrayBuffer>], { type }));
  const audio = new Audio(url);
  let done = false;
  const finish = (failed = false) => {
    if (done) return;
    done = true;
    audio.onended = null;
    audio.onerror = null;
    audio.pause();
    audio.removeAttribute("src");
    URL.revokeObjectURL(url);
    onEnd(failed);
  };
  audio.onended = () => finish();
  audio.onerror = () => finish(true);
  void audio.play().catch(() => finish(true));
  return { stop: () => finish() };
}
