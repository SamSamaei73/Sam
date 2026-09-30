import { useState } from "react";
import { IconPanelRight } from "../components/Icons";
import { IconButton } from "../components/primitives";
import { CompactTextInput } from "../home/CompactTextInput";
import { ContextPanel } from "../home/ContextPanel";
import { SamPresence } from "../home/SamPresence";
import { TransientTranscript } from "../home/TransientTranscript";
import { useAttention } from "../home/useAttention";
import { VoiceControl } from "../home/VoiceControl";
import { VoiceStateIndicator } from "../home/VoiceStateIndicator";
import { useSession } from "../session";
import { useSam } from "../state";
import type { ViewId } from "./viewIds";

/**
 * Home is Sam: the presence in the centre, one voice control beneath it, a
 * transient line for the latest exchange, and a collapsible context panel.
 * No chat log and no permanent composer: history lives in History, text in
 * a compact overlay.
 */
export function HomeView({ onNavigate }: { onNavigate: (view: ViewId) => void }) {
  const { t, identity } = useSam();
  const session = useSession();
  const [panelOpen, setPanelOpen] = useState(false);
  const [typing, setTyping] = useState(false);
  const { items, nextUp } = useAttention();
  const attention = items.length > 0;

  return (
    <div className="home" data-panel={panelOpen ? "open" : "closed"}>
      <section className="home-stage" aria-label="Sam">
        <div className="home-top">
          {attention ? (
            <button
              type="button"
              className="attention-pill"
              onClick={() => setPanelOpen(true)}
              aria-label={`${t("home.actionRequired")}: ${items.length}`}
            >
              <span className="attention-dot" aria-hidden="true" />
              {t("home.actionRequired")}
              <span className="attention-count">{items.length}</span>
            </button>
          ) : null}
          <IconButton
            label={panelOpen ? "Hide system panel" : "Show system panel"}
            selected={panelOpen}
            onClick={() => setPanelOpen((open) => !open)}
          >
            <IconPanelRight />
          </IconButton>
        </div>
        <VoiceStateIndicator state={session.voice} />
        <SamPresence
          state={session.voice}
          level={session.level}
          attention={attention}
          guest={identity?.guest.active === true}
        />
        <TransientTranscript onOpenHistory={() => onNavigate("history")} />
        <VoiceControl onType={() => setTyping(true)} onSetup={() => onNavigate("settings")} />
      </section>
      {panelOpen ? <ContextPanel items={items} nextUp={nextUp} onNavigate={onNavigate} /> : null}
      {typing ? <CompactTextInput onClose={() => setTyping(false)} /> : null}
    </div>
  );
}
