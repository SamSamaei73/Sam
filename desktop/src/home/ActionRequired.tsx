import { useState } from "react";
import type { AttentionItem } from "./useAttention";
import { useSam } from "../state";
import type { ViewId } from "../views/viewIds";
import { useSession } from "../session";

/** A compact list of what needs the owner; each links to where to resolve it. */
export function ActionRequired({
  items,
  onNavigate,
}: {
  items: AttentionItem[];
  onNavigate: (view: ViewId) => void;
}) {
  const { t, bridge, refreshStatus } = useSam();
  const [refreshing, setRefreshing] = useState(false);
  const retryModels = async () => {
    setRefreshing(true);
    try { await bridge.providersStatus(); }
    catch { /* Keep the actual blocker visible; a failed probe is never ready. */ }
    finally { await refreshStatus(); setRefreshing(false); }
  };
  const { wakeNow } = useSession();
  return (
    <section className="attention" aria-label={t("home.actionRequired")}>
      <h3>{t("home.actionRequired")}</h3>
      {items.length === 0 ? (
        <p className="faint">{t("home.noActions")}</p>
      ) : (
        <ul>
          {items.map((item) => (
            <li key={item.id} data-tone={item.tone}>
              <span className="attention-dot" aria-hidden="true" />
              <div className="attention-detail">
                <strong>{item.label}</strong><span className="attention-state">{item.state}</span>
                <p>{item.explanation}</p>
                {item.blocks.length > 0 ? <p className="faint">Blocks: {item.blocks.join(" · ")}</p> : null}
                <button type="button" className="link-button" onClick={() => item.retryVoice ? wakeNow() : item.target && onNavigate(item.target)}>{item.action}</button>
                {item.retryModel ? <button type="button" className="link-button" disabled={refreshing} onClick={() => void retryModels()}>{refreshing ? "Checking providers…" : "Retry providers"}</button> : null}
                <details><summary>Details</summary><code>{item.reason_code}</code></details>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
