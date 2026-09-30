import { useEffect, useRef, useState } from "react";
import { Composer } from "../components/Composer";
import { useSession } from "../session";
import { useSam } from "../state";

/**
 * Secondary, accessible text input: a compact overlay (command palette), not a
 * permanent composer. It closes after sending or on Escape.
 */
export function CompactTextInput({ onClose }: { onClose: () => void }) {
  const { t } = useSam();
  const session = useSession();
  const [privateMessage, setPrivateMessage] = useState(false);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    panel.current?.querySelector("textarea")?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="palette-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={panel} className="palette" role="dialog" aria-modal="true" aria-label={t("home.type")}>
        <Composer
          disabled={!session.chatAvailable || session.textSending}
          value={session.textDraft}
          onChange={session.setTextDraft}
          onSend={(text) => {
            session.setTextDraft(""); // only an explicit send clears the draft
            onClose();
            void session.sendText(text, privateMessage);
          }}
          leading={
            <button
              type="button"
              className="pill"
              data-tone={privateMessage ? "warn" : "muted"}
              aria-pressed={privateMessage}
              title={t("models.privateChatHint")}
              onClick={() => setPrivateMessage(!privateMessage)}
            >
              {t("models.privateChat")}
            </button>
          }
        />
        {!session.chatAvailable ? <p className="faint palette-note">{t("home.chatUnavailable")}</p> : null}
      </div>
    </div>
  );
}
