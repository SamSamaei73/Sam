import { IconSpeaker, IconStop } from "../components/Icons";
import { MessageText } from "../components/MessageBubble";
import { IconButton } from "../components/primitives";
import { detectLanguage } from "../lib/language";
import { useSession } from "../session";
import { useSam } from "../state";

export const TRANSIENT_MAX_CHARS = 280;

/**
 * A compact glass line under Sam: what Sam heard and the start of its reply.
 * It is NOT a conversation log (that is the History view): it shows only the
 * latest exchange and can be dismissed. Text is rendered inert.
 */
export function TransientTranscript({ onOpenHistory }: { onOpenHistory: () => void }) {
  const { t } = useSam();
  const session = useSession();
  const exchange = session.exchange;
  const error = session.error;
  if (!exchange && !error) return null;
  const reply = exchange?.reply ?? null;
  const shortReply =
    reply && reply.text.length > TRANSIENT_MAX_CHARS
      ? { ...reply, text: `${reply.text.slice(0, TRANSIENT_MAX_CHARS)}…` }
      : reply;
  const language = reply ? (reply.language ?? detectLanguage(reply.text) ?? "en") : "en";
  const canSpeak = reply?.role === "sam" && session.profileFor(language) !== null;
  const speaking = reply !== null && session.speakingId === reply.id;
  return (
    <section className="transient" aria-label="Latest exchange" aria-live="polite">
      {error ? (
        <p className="transient-error" role="alert">
          {error}
        </p>
      ) : null}
      {exchange?.heard ? (
        <div className="transient-heard" data-role={exchange.heard.role}>
          <span className="transient-who">
            {exchange.heard.role === "guest" ? t("chat.guest") : t("chat.you")}
          </span>
          <MessageText message={exchange.heard} />
        </div>
      ) : null}
      {shortReply ? (
        <div className="transient-reply" data-role={shortReply.role} data-status={shortReply.status ?? "ok"}>
          <span className="transient-who">{shortReply.role === "sam" ? t("chat.sam") : "Notice"}</span>
          <MessageText message={shortReply} />
          {shortReply.status === "failed" && shortReply.referenceId ? (
            <span className="message-meta">Reference: {shortReply.referenceId}</span>
          ) : null}
        </div>
      ) : exchange?.heard ? (
        <p className="transient-wait">{t("home.state.thinking")}…</p>
      ) : null}
      <div className="transient-tools">
        {canSpeak && reply ? (
          speaking ? (
            <IconButton label={t("home.stopSpeaking")} onClick={session.stopSpeaking}>
              <IconStop />
            </IconButton>
          ) : (
            <IconButton label={t("home.speakReply")} onClick={() => void session.speak(reply)}>
              <IconSpeaker />
            </IconButton>
          )
        ) : null}
        {exchange ? (
          <button type="button" className="link-button" onClick={onOpenHistory}>
            {t("home.openHistory")}
          </button>
        ) : null}
        <button type="button" className="link-button" onClick={session.dismissExchange}>
          {t("home.dismiss")}
        </button>
      </div>
    </section>
  );
}
