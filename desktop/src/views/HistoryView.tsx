import { MessageBubble } from "../components/MessageBubble";
import { IconSpeaker, IconStop } from "../components/Icons";
import { IconButton, NeonButton, SectionHeader, SelectPill } from "../components/primitives";
import { detectLanguage } from "../lib/language";
import { useSession } from "../session";
import { useSam } from "../state";

/**
 * The full conversation for this session (memory only, never saved). This is
 * where the transcript lives, so Home can stay a clean presence.
 */
export function HistoryView() {
  const { t } = useSam();
  const session = useSession();
  const messages = session.active.messages;
  return (
    <div className="page-narrow history">
      <SectionHeader
        title={t("nav.history")}
        description={t("history.description")}
        actions={
          <span className="row">
            <SelectPill
              label="Conversation"
              value={session.active.id}
              onChange={session.selectConversation}
              options={session.conversations.map((c) => ({ value: c.id, label: c.title }))}
            />
            <NeonButton variant="quiet" onClick={session.newConversation}>
              {t("chat.newChat")}
            </NeonButton>
            {messages.length > 0 ? (
              <NeonButton variant="quiet" onClick={session.clearActive}>
                {t("chat.clear")}
              </NeonButton>
            ) : null}
          </span>
        }
      />
      <div className="thread" role="log" aria-live="polite" aria-relevant="additions">
        <div className="thread-inner">
          {messages.length === 0 ? (
            <p className="faint">{t("history.empty")}</p>
          ) : (
            messages.map((message) => {
              const language = message.language ?? detectLanguage(message.text) ?? "en";
              const profile = message.role === "sam" ? session.profileFor(language) : null;
              const speaking = session.speakingId === message.id;
              return (
                <MessageBubble
                  key={message.id}
                  message={message}
                  tools={
                    profile ? (
                      speaking ? (
                        <IconButton label={t("chat.stopReading")} onClick={session.stopSpeaking}>
                          <IconStop />
                        </IconButton>
                      ) : (
                        <IconButton label={t("chat.readAloud")} onClick={() => void session.speak(message)}>
                          <IconSpeaker />
                        </IconButton>
                      )
                    ) : null
                  }
                />
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
