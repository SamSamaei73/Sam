import { useEffect } from "react";
import { IconKeyboard, IconMic, IconStop } from "../components/Icons";
import { IconButton } from "../components/primitives";
import { useSession } from "../session";
import { useSam } from "../state";

/**
 * Quiet controls under Sam. With voice activation on, the owner just says
 * "Sam": there is no button to press, only a small manual fallback ("talk
 * now" / "stop listening") and the keyboard. Without it, the small
 * microphone is push-to-talk (a click starts, a second click sends).
 * Nothing here ever turns listening on by itself.
 */
export function VoiceControl({ onType, onSetup }: { onType: () => void; onSetup: () => void }) {
  const { t } = useSam();
  const session = useSession();
  const hf = session.handsFree;
  const { cancelListening, setHomeActive } = session;
  // Home is on screen: hands-free may run. Leaving Home always releases the
  // microphone (push-to-talk capture and the wake-word listener alike).
  useEffect(() => {
    setHomeActive(true);
    return () => {
      setHomeActive(false);
      cancelListening();
    };
  }, [cancelListening, setHomeActive]);

  if (hf.active) return <HandsFreeControls onType={onType} />;

  const listening = session.voice === "listening";
  const blocked = session.voice === "thinking" || session.voice === "permission";
  const label = !session.micAvailable
    ? `${t("home.talk")} (${t("mic.notConfigured")})`
    : listening
      ? t("mic.stop")
      : t("home.talk");

  return (
    <div className="voice-control" data-state={session.voice}>
      <div className="voice-side">
        {listening ? (
          <IconButton label={t("mic.cancel")} onClick={session.cancelListening}>
            ✕
          </IconButton>
        ) : session.voice === "speaking" ? (
          <IconButton label={t("home.stopSpeaking")} onClick={session.stopSpeaking}>
            <IconStop />
          </IconButton>
        ) : null}
      </div>
      <button
        type="button"
        className="mic-button"
        data-listening={listening ? "true" : "false"}
        aria-label={label}
        title={label}
        disabled={!session.micAvailable || (blocked && !listening)}
        onClick={() => void (listening ? session.stopListening() : session.startListening())}
      >
        <span className="mic-glow" aria-hidden="true" />
        {listening ? <IconStop /> : <IconMic />}
      </button>
      <div className="voice-side">
        <IconButton label={t("home.type")} onClick={onType}>
          <IconKeyboard />
        </IconButton>
      </div>
      <p className="voice-hint" aria-live="polite">
        {listening ? (
          <span className="recording" role="status">
            <span className="rec-dot" aria-hidden="true" />
            {t("mic.recording")} {session.recordingSeconds}s
          </span>
        ) : hf.micDenied ? (
          <span className="hint-problem">
            {t("home.micDenied")}{" "}
            <button type="button" className="link-button" onClick={session.wakeNow}>
              {t("home.tryAgain")}
            </button>
          </span>
        ) : hf.needsSetup ? (
          <button type="button" className="link-button setup-link" onClick={onSetup}>
            {hf.setupState === "models_missing"
              ? t("home.installVoice")
              : hf.setupState === "restart_required"
                ? t("home.finishVoice")
                : t("home.setupVoice")}
          </button>
        ) : session.micAvailable ? (
          hf.activation === "off" ? t("home.pushToTalk") : t("home.talk")
        ) : (
          t("mic.notConfigured")
        )}
      </p>
    </div>
  );
}

function HandsFreeControls({ onType }: { onType: () => void }) {
  const { t } = useSam();
  const session = useSession();
  const phase = session.handsFree.phase;
  const inConversation = phase === "attending" || phase === "thinking" || phase === "speaking";
  const hint =
    session.voice === "speaking"
      ? t("home.hint.speaking")
      : session.voice === "thinking"
        ? t("home.hint.thinking")
        : session.voice === "listening" || session.voice === "attending" || session.voice === "waking"
          ? t("home.hint.listening")
          : t("home.hint.sleeping");
  return (
    <div className="voice-control hands-free" data-state={session.voice}>
      <p className="voice-hint hands-free-hint" aria-live="polite">
        {hint}
      </p>
      <div className="hands-free-tools">
        {session.voice === "speaking" ? (
          <IconButton label={t("home.stopSpeaking")} onClick={session.stopSpeaking}>
            <IconStop />
          </IconButton>
        ) : inConversation ? (
          <IconButton label={t("home.stopListening")} onClick={session.sleepNow}>
            <IconStop />
          </IconButton>
        ) : (
          <IconButton label={t("home.talkNow")} onClick={session.wakeNow}>
            <IconMic />
          </IconButton>
        )}
        <IconButton label={t("home.type")} onClick={onType}>
          <IconKeyboard />
        </IconButton>
      </div>
    </div>
  );
}
