import type { ReactNode } from "react";
import type { ConnectionState } from "../state";
import { useSam } from "../state";
import { IconPanelLeft } from "./Icons";
import { IconButton } from "./primitives";

export interface NavEntry {
  id: string;
  label: string;
  icon: ReactNode;
}

const STATE_COPY: Record<ConnectionState, string> = {
  starting: "Starting",
  connected: "Connected",
  degraded: "Degraded",
  unavailable: "Unavailable",
};

/**
 * A slim navigation rail: icons with compact labels (icons only when
 * collapsed). Home is Sam; everything else is Sam's systems and workspace.
 */
export function AppNavigation({
  nav,
  view,
  onNavigate,
  connection,
  collapsed,
  onToggle,
}: {
  nav: NavEntry[];
  view: string;
  onNavigate: (id: string) => void;
  connection: ConnectionState;
  collapsed: boolean;
  onToggle: () => void;
}) {
  const { t } = useSam();
  return (
    <nav className="rail" aria-label="Main" data-collapsed={collapsed ? "true" : "false"}>
      <div className="rail-head">
        <IconButton label={collapsed ? "Expand sidebar" : "Collapse sidebar"} onClick={onToggle}>
          <IconPanelLeft />
        </IconButton>
      </div>
      <ul className="rail-list">
        {nav.map((item) => (
          <li key={item.id}>
            <button
              type="button"
              className="rail-item"
              aria-current={view === item.id ? "page" : undefined}
              title={item.label}
              onClick={() => onNavigate(item.id)}
            >
              {item.icon}
              <span className="rail-label">{item.label}</span>
            </button>
          </li>
        ))}
      </ul>
      <div className="rail-foot">
        <span
          className="conn-dot"
          data-state={connection}
          role="status"
          aria-label={`Connection: ${STATE_COPY[connection]}`}
          title={`${t("nav.connection")}: ${STATE_COPY[connection]}`}
        />
      </div>
    </nav>
  );
}
