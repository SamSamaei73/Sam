import { SystemPanel } from "../components/SystemPanel";
import { useSam } from "../state";
import type { ViewId } from "../views/viewIds";
import { ActionRequired } from "./ActionRequired";
import type { AttentionItem, NextUp } from "./useAttention";

/** Identity as safe labels only: never a score, embedding or threshold. */
export function identityLabel(
  identity: ReturnType<typeof useSam>["identity"],
  t: ReturnType<typeof useSam>["t"],
): { label: string; tone: "ok" | "warn" | "guest" | "muted" } {
  if (identity?.guest.active) return { label: t("home.identity.guest"), tone: "guest" };
  if (!identity || !identity.available || identity.enrolled !== true) {
    return { label: t("home.identity.none"), tone: "muted" };
  }
  if (identity.last_verification === "verified") return { label: t("home.identity.owner"), tone: "ok" };
  if (identity.last_verification === "not_verified") return { label: t("home.identity.verify"), tone: "warn" };
  return { label: t("home.identity.unknown"), tone: "muted" };
}

export function ContextPanel({
  items,
  nextUp,
  onNavigate,
}: {
  items: AttentionItem[];
  nextUp: NextUp | null;
  onNavigate: (view: ViewId) => void;
}) {
  const { status, identity, t } = useSam();
  const who = identityLabel(identity, t);
  return (
    <SystemPanel status={status}>
      <ActionRequired items={items} onNavigate={onNavigate} />
      <section className="context-block" aria-label={t("home.identity.title")}>
        <h3>{t("home.identity.title")}</h3>
        <p className="identity-line" data-tone={who.tone}>
          {who.label}
        </p>
      </section>
      {nextUp ? (
        <section className="context-block" aria-label={t("home.nextUp")}>
          <h3>{t("home.nextUp")}</h3>
          <p>
            {nextUp.title}
            <span className="faint"> · {new Date(nextUp.at).toLocaleString()}</span>
          </p>
        </section>
      ) : null}
    </SystemPanel>
  );
}
