import { useEffect, useRef, useState } from "react";
import { useSam } from "../state";
import { Recorder, RecorderError, type AudioEnvironment } from "../lib/recorder";
import { IconMic, IconStop } from "./Icons";
import { IconButton, Tooltip } from "./primitives";



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
  const { t, audioEnvironment } = useSam();
  const env = environment !== undefined ? environment : audioEnvironment;
  const errorText = (code: string): string =>
    t(`mic.${code === "denied" || code === "unsupported" || code === "empty" ? code : "failed"}` as "mic.failed");
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
      onError(errorText(error instanceof RecorderError ? error.code : "failed"));
    }
  };
  useEffect(() => {
    finishRef.current = () => void finish();
  });

  const begin = async () => {
    const next = new Recorder(env, () => finishRef.current());
    recorder.current = next;
    try {
      await next.start();
      setRecording(true);
    } catch (error) {
      recorder.current = null;
      onError(errorText(error instanceof RecorderError ? error.code : "failed"));
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
      <Tooltip text={t("mic.notConfigured")}>
        <IconButton label={`${t("mic.start")} (${t("mic.notConfigured")})`} disabled>
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
            <span className="pill-text">
              {t("mic.recording")} {elapsed}s
            </span>
          </span>
          <IconButton label={t("mic.cancel")} onClick={cancel}>
            ✕
          </IconButton>
        </>
      ) : null}
      <IconButton
        label={recording ? t("mic.stop") : t("mic.start")}
        active={recording}
        disabled={busy}
        onClick={() => (recording ? void finish() : void begin())}
      >
        {recording ? <IconStop /> : <IconMic />}
      </IconButton>
    </span>
  );
}
