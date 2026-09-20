import { useEffect, useRef, useState } from "react";
import { Recorder, RecorderError, type AudioEnvironment } from "../lib/recorder";
import { IconMic, IconStop } from "./Icons";
import { IconButton, Tooltip } from "./primitives";

const ERROR_COPY: Record<string, string> = {
  unsupported: "This device can't record audio.",
  denied: "Microphone access was declined.",
  failed: "The microphone couldn't be started.",
  empty: "Nothing was recorded.",
};

/**
 * Explicit push-to-record. Recording starts ONLY from the click/keypress on
 * this button, is visibly indicated, and ends on a second click, Cancel, the
 * length limit, or unmount (all of which release the microphone). There is no
 * always-on listening and no wake word. The recording stays in memory.
 */
export function VoiceRecorder({
  available,
  busy,
  onRecorded,
  onError,
  environment,
}: {
  available: boolean;
  busy: boolean;
  onRecorded: (audioBase64: string) => void;
  onError: (message: string) => void;
  environment?: AudioEnvironment | null;
}) {
  const [recording, setRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const recorder = useRef<Recorder | null>(null);
  const finishRef = useRef<() => void>(() => undefined);

  useEffect(() => {
    return () => recorder.current?.cancel();
  }, []);

  useEffect(() => {
    if (!recording) return;
    const started = Date.now();
    const timer = window.setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 250);
    return () => window.clearInterval(timer);
  }, [recording]);

  const finish = async () => {
    const active = recorder.current;
    if (!active) return;
    recorder.current = null;
    setRecording(false);
    setElapsed(0);
    try {
      const result = await active.stop();
      onRecorded(result.base64);
    } catch (error) {
      onError(ERROR_COPY[error instanceof RecorderError ? error.code : "failed"] ?? ERROR_COPY.failed!);
    }
  };
  useEffect(() => {
    finishRef.current = () => void finish();
  });

  const begin = async () => {
    const next = new Recorder(environment, () => finishRef.current());
    recorder.current = next;
    try {
      await next.start();
      setRecording(true);
    } catch (error) {
      recorder.current = null;
      onError(ERROR_COPY[error instanceof RecorderError ? error.code : "failed"] ?? ERROR_COPY.failed!);
    }
  };

  const cancel = () => {
    recorder.current?.cancel();
    recorder.current = null;
    setRecording(false);
    setElapsed(0);
  };

  if (!available) {
    return (
      <Tooltip text="Voice input isn't configured">
        <IconButton label="Voice input (not configured)" disabled>
          <IconMic />
        </IconButton>
      </Tooltip>
    );
  }

  return (
    <span className="row" style={{ gap: 6 }}>
      {recording ? (
        <>
          <span className="pill" data-tone="critical" role="status" aria-live="polite">
            <span className="rec-dot" aria-hidden="true" />
            <span className="pill-text">Recording {elapsed}s</span>
          </span>
          <IconButton label="Cancel recording" onClick={cancel}>
            ✕
          </IconButton>
        </>
      ) : null}
      <IconButton
        label={recording ? "Stop recording and send" : "Record a voice message"}
        active={recording}
        disabled={busy}
        onClick={() => (recording ? void finish() : void begin())}
      >
        {recording ? <IconStop /> : <IconMic />}
      </IconButton>
    </span>
  );
}
