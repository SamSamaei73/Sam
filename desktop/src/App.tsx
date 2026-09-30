import { useState, type ReactNode } from "react";
import type { SamBridge } from "./bridge/bridge";
import { AppNavigation, type NavEntry } from "./components/AppNavigation";
import { GuestBanner } from "./components/GuestBanner";
import {
  IconActivity,
  IconCareer,
  IconHistory,
  IconHome,
  IconKnowledge,
  IconMemory,
  IconProactive,
  IconProfessional,
  IconSettings,
  IconShield,
  IconTools,
} from "./components/Icons";
import type { StringKey } from "./i18n/strings";
import type { LocalSpeechOutput } from "./lib/localSpeech";
import type { AudioEnvironment } from "./lib/recorder";
import { StartupScreen } from "./home/StartupScreen";
import { SessionProvider } from "./session";
import { SamProvider, useSam } from "./state";
import { ActivityView } from "./views/ActivityView";
import { CareerView } from "./views/CareerView";
import { HistoryView } from "./views/HistoryView";
import { HomeView } from "./views/HomeView";
import { KnowledgeView } from "./views/KnowledgeView";
import { MemoryView } from "./views/MemoryView";
import { PermissionsView } from "./views/PermissionsView";
import { ProactiveView } from "./views/ProactiveView";
import { ProfessionalView } from "./views/ProfessionalView";
import { SettingsView } from "./views/SettingsView";
import { ToolsView } from "./views/ToolsView";
import type { ViewId } from "./views/viewIds";

export type { ViewId } from "./views/viewIds";

const NAV_ICONS: Record<ViewId, ReactNode> = {
  home: <IconHome />,
  history: <IconHistory />,
  knowledge: <IconKnowledge />,
  professional: <IconProfessional />,
  proactive: <IconProactive />,
  career: <IconCareer />,
  memory: <IconMemory />,
  tools: <IconTools />,
  permissions: <IconShield />,
  activity: <IconActivity />,
  settings: <IconSettings />,
};
const NAV_KEYS: Record<ViewId, StringKey> = {
  home: "nav.home",
  history: "nav.history",
  knowledge: "nav.knowledge",
  professional: "nav.professional",
  proactive: "nav.proactive",
  career: "nav.career",
  memory: "nav.memory",
  tools: "nav.tools",
  permissions: "nav.permissions",
  activity: "nav.activity",
  settings: "nav.settings",
};
const VIEW_ORDER: ViewId[] = [
  "home",
  "professional",
  "career",
  "proactive",
  "knowledge",
  "memory",
  "tools",
  "history",
  "activity",
  "permissions",
  "settings",
];

function Shell() {
  const { connection, prefs, updatePrefs, refreshStatus, t } = useSam();
  const nav: (NavEntry & { id: ViewId })[] = VIEW_ORDER.map((id) => ({
    id,
    label: t(NAV_KEYS[id]),
    icon: NAV_ICONS[id],
  }));
  const [view, setView] = useState<ViewId>("home");
  const collapsed = prefs.sidebarCollapsed;
  if (connection === "starting") return <StartupScreen />;
  return (
    <div className="app">
      <GuestBanner />
      {connection === "unavailable" && view !== "home" ? (
        <div className="banner" role="alert">
          <span>Can't reach Sam's backend</span>
          <span className="spacer" />
          <button type="button" className="link-button" onClick={() => void refreshStatus()}>
            Try again
          </button>
        </div>
      ) : null}
      <div className="body">
        <AppNavigation
          nav={nav}
          view={view}
          onNavigate={(id) => setView(id as ViewId)}
          connection={connection}
          collapsed={collapsed}
          onToggle={() => updatePrefs({ sidebarCollapsed: !collapsed })}
        />
        <main className="main" aria-label={nav.find((n) => n.id === view)?.label}>
          {view === "home" ? (
            <HomeView onNavigate={setView} />
          ) : (
            <div className="page">{pageFor(view)}</div>
          )}
        </main>
      </div>
    </div>
  );
}

function pageFor(view: ViewId): ReactNode {
  switch (view) {
    case "history":
      return <HistoryView />;
    case "knowledge":
      return <KnowledgeView />;
    case "professional":
      return <ProfessionalView />;
    case "proactive":
      return <ProactiveView />;
    case "career":
      return <CareerView />;
    case "memory":
      return <MemoryView />;
    case "tools":
      return <ToolsView />;
    case "permissions":
      return <PermissionsView />;
    case "activity":
      return <ActivityView />;
    case "settings":
      return <SettingsView />;
    default:
      return null;
  }
}

export function App({
  bridge,
  audioEnvironment,
  speech,
}: {
  bridge: SamBridge;
  audioEnvironment?: AudioEnvironment | null;
  /** Local voice for hands-free replies (tests inject a fake). */
  speech?: LocalSpeechOutput | null;
}) {
  return (
    <SamProvider bridge={bridge} audioEnvironment={audioEnvironment}>
      <SessionProvider {...(speech !== undefined ? { speech } : {})}>
        <div className="scene" aria-hidden="true" />
        <Shell />
      </SessionProvider>
    </SamProvider>
  );
}
