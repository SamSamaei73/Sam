import { useSam } from "../state";
import { SamPresence } from "./SamPresence";

/**
 * Shown only while Sam's own backend is genuinely starting (bounded; see
 * ``STARTUP_LIMIT_MS``): Sam's presence, centred, and one quiet word. No
 * buttons, no navigation, no error text.
 */
export function StartupScreen() {
  const { t } = useSam();
  return (
    <div className="startup" role="status" aria-live="polite">
      <div className="startup-presence">
        <SamPresence state="idle" label={t("startup.label")} />
      </div>
      <p className="wordmark startup-word" aria-hidden="true">
        SAM
      </p>
      <p className="state-word">{t("startup.word")}</p>
    </div>
  );
}
