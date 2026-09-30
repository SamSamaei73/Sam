import type { AttentionItem } from "./useAttention";
import { useSam } from "../state";
import type { ViewId } from "../views/viewIds";

/** A compact list of what needs the owner; each links to where to resolve it. */
export function ActionRequired({
  items,
  onNavigate,
}: {
  items: AttentionItem[];
  onNavigate: (view: ViewId) => void;
}) {
  const { t } = useSam();
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
              {item.target ? (
                <button type="button" className="link-button" onClick={() => onNavigate(item.target!)}>
                  {item.label}
                </button>
              ) : (
                <span>{item.label}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
