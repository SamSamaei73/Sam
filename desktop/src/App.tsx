import { useCallback, useState, type ReactNode } from "react";
import type { SamBridge } from "./bridge/bridge";
import {
  IconActivity,
  IconChat,
  IconKnowledge,
  IconMemory,
  IconPanelLeft,
  IconSettings,
  IconShield,
  IconTools,
} from "./components/Icons";
import type { ChatMessage } from "./components/MessageBubble";
import { IconButton } from "./components/primitives";
import { Sidebar, type NavEntry } from "./components/Sidebar";
import { SystemPanel } from "./components/SystemPanel";
import type { AudioEnvironment } from "./lib/recorder";
import { GuestBanner } from "./components/GuestBanner";
import { SamProvider, useSam } from "./state";
import { ActivityView } from "./views/ActivityView";
import { KnowledgeView } from "./views/KnowledgeView";
import { MemoryView } from "./views/MemoryView";
import { PermissionsView } from "./views/PermissionsView";
import { SamView } from "./views/SamView";
import { SettingsView } from "./views/SettingsView";
import { ToolsView } from "./views/ToolsView";

export type ViewId = "sam" | "knowledge" | "memory" | "tools" | "permissions" | "activity" | "settings";

const NAV_ICONS: Record<ViewId, ReactNode> = {
  sam: <IconChat />,
  knowledge: <IconKnowledge />,
  memory: <IconMemory />,
  tools: <IconTools />,
  permissions: <IconShield />,
  activity: <IconActivity />,
  settings: <IconSettings />,
};
const NAV_KEYS = {
  sam: "nav.chat",
  knowledge: "nav.knowledge",
  memory: "nav.memory",
  tools: "nav.tools",
  permissions: "nav.permissions",
  activity: "nav.activity",
  settings: "nav.settings",
} as const;
const VIEW_ORDER: ViewId[] = ["sam", "knowledge", "memory", "tools", "permissions", "activity", "settings"];

interface Conversation {
  id: string;
  title: string;
  messages: ChatMessage[];
}

let conversationCounter = 0;
const newConversation = (): Conversation => ({
  id: `c${++conversationCounter}`,
  title: "New chat",
  messages: [],
});

function titleFrom(messages: ChatMessage[]): string {
  const first = messages.find((m) => m.role === "user");
  if (!first) return "New chat";
  const text = first.text.replace(/\s+/g, " ").trim();
  return text.length > 36 ? `${text.slice(0, 36)}…` : text;
}

function Shell({ audioEnvironment }: { audioEnvironment?: AudioEnvironment | null }) {
  const { connection, prefs, updatePrefs, status, refreshStatus, t } = useSam();
  const NAV: (NavEntry & { id: ViewId })[] = VIEW_ORDER.map((id) => ({
    id,
    label: t(NAV_KEYS[id]),
    icon: NAV_ICONS[id],
  }));
  const [view, setView] = useState<ViewId>("sam");
  const [panelOpen, setPanelOpen] = useState(false);
  // Conversation history is session-local React state: never persisted.
  const [conversations, setConversations] = useState<Conversation[]>(() => [newConversation()]);
  const [activeId, setActiveId] = useState<string>(() => conversations[0]?.id ?? "");
  const active = conversations.find((c) => c.id === activeId) ?? conversations[0];

  const setMessages = useCallback(
    (update: React.SetStateAction<ChatMessage[]>) => {
      setConversations((current) =>
        current.map((c) => {
          if (c.id !== activeId) return c;
          const messages = typeof update === "function" ? update(c.messages) : update;
          return { ...c, messages, title: titleFrom(messages) };
        }),
      );
    },
    [activeId],
  );

  const newChat = () => {
    setView("sam");
    if (active && active.messages.length === 0) return;
    const created = newConversation();
    setConversations((current) => [created, ...current]);
    setActiveId(created.id);
  };

  const select = (id: string) => {
    setActiveId(id);
    setView("sam");
  };

  const collapsed = prefs.sidebarCollapsed;
  return (
    <div className="app">
      <div className="pulse" data-state={connection} aria-hidden="true" />
      <GuestBanner />
      {connection === "unavailable" ? (
        <div className="banner" role="alert">
          <span>Can't reach Sam's backend</span>
          <span className="spacer" />
          <button type="button" className="link-button" onClick={() => void refreshStatus()}>
            Try again
          </button>
        </div>
      ) : null}
      <div className="body" data-collapsed={collapsed ? "true" : "false"}>
        <Sidebar
          nav={NAV}
          view={view}
          onNavigate={(id) => setView(id as ViewId)}
          connection={connection}
          conversations={conversations.map((c) => ({ id: c.id, title: c.title }))}
          activeConversation={active?.id ?? ""}
          onSelectConversation={select}
          onNewChat={newChat}
          onCollapse={() => updatePrefs({ sidebarCollapsed: true })}
        />
        {collapsed ? (
          <div className="reopen">
            <IconButton label="Expand sidebar" onClick={() => updatePrefs({ sidebarCollapsed: false })}>
              <IconPanelLeft />
            </IconButton>
          </div>
        ) : null}
        <main className="main" aria-label={NAV.find((n) => n.id === view)?.label}>
          <div className="main-col">
            {view === "sam" && active ? (
              <SamView
                messages={active.messages}
                setMessages={setMessages}
                conversations={conversations.map((c) => ({ id: c.id, title: c.title }))}
                activeConversation={active.id}
                onSelectConversation={select}
                onNewChat={newChat}
                panelOpen={panelOpen}
                onTogglePanel={() => setPanelOpen((open) => !open)}
                onNavigate={setView}
                audioEnvironment={audioEnvironment}
              />
            ) : (
              <div className="page">{pageFor(view)}</div>
            )}
          </div>
          {view === "sam" && panelOpen ? <SystemPanel status={status} /> : null}
        </main>
      </div>
    </div>
  );
}

function pageFor(view: ViewId): ReactNode {
  switch (view) {
    case "knowledge":
      return <KnowledgeView />;
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

export function App({ bridge, audioEnvironment }: { bridge: SamBridge; audioEnvironment?: AudioEnvironment | null }) {
  return (
    <SamProvider bridge={bridge} audioEnvironment={audioEnvironment}>
      <div className="scene" aria-hidden="true" />
      <Shell audioEnvironment={audioEnvironment} />
    </SamProvider>
  );
}
