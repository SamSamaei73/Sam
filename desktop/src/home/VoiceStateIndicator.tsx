import type { VoiceState } from "../session";
import { useSam } from "../state";
import type { StringKey } from "../i18n/strings";

const LABEL: Record<VoiceState, StringKey> = {
  idle: "home.state.idle",
  sleeping: "home.state.sleeping",
  waking: "home.state.waking",
  attending: "home.state.attending",
  listening: "home.state.listening",
  thinking: "home.state.thinking",
  speaking: "home.state.speaking",
  permission: "home.state.permission",
  error: "home.state.error",
  offline: "home.state.offline",
};

/** "SAM" and one word for what Sam is doing right now (real state only). */
export function VoiceStateIndicator({ state }: { state: VoiceState }) {
  const { t } = useSam();
  return (
    <header className="sam-heading">
      <h1 className="wordmark">SAM</h1>
      <p className="state-word" data-state={state} role="status" aria-live="polite">
        <span className="state-dot" aria-hidden="true" />
        {t(LABEL[state])}
      </p>
    </header>
  );
}
