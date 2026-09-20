import { useState, type ReactNode } from "react";
import type { ConnectionState } from "../state";
import { IconPanelLeft, IconPlus, IconSearch, SamMark } from "./Icons";
import { IconButton, SidebarItem } from "./primitives";

export interface ConversationSummary {
  id: string;
  title: string;
}

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
 * OpenJarvis-style sidebar: header (collapse, new chat), status badge, chat
 * search, session-local conversation list, and the page navigation at the
 * bottom with a glowing active bar. Conversation titles live in memory only.
 */
export function Sidebar({
  nav,
  view,
  onNavigate,
  connection,
  conversations,
  activeConversation,
  onSelectConversation,
  onNewChat,
  onCollapse,
}: {
  nav: NavEntry[];
  view: string;
  onNavigate: (id: string) => void;
  connection: ConnectionState;
  conversations: ConversationSummary[];
  activeConversation: string;
  onSelectConversation: (id: string) => void;
  onNewChat: () => void;
  onCollapse: () => void;
}) {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const shown = conversations.filter((c) => c.title.toLowerCase().includes(needle));

  return (
    <nav className="sidebar" aria-label="Main">
      <div className="sidebar-inner">
        <div className="sidebar-head">
          <div className="brand">
            <span className="brand-mark">
              <SamMark />
            </span>
            <span>Sam</span>
          </div>
          <div className="sidebar-tools">
            <IconButton label="New chat" onClick={onNewChat}>
              <IconPlus />
            </IconButton>
            <IconButton label="Collapse sidebar" onClick={onCollapse}>
              <IconPanelLeft />
            </IconButton>
          </div>
        </div>

        <button
          type="button"
          className="status-badge"
          data-state={connection}
          onClick={() => onNavigate("settings")}
          aria-label={`Sam engine status: ${STATE_COPY[connection]}. Open settings`}
        >
          <span className="dot" aria-hidden="true" />
          <span className="grow">
            <strong>Sam engine</strong>
            <span className="faint">{STATE_COPY[connection]}</span>
          </span>
        </button>

        <label className="search">
          <IconSearch />
          <input
            type="text"
            aria-label="Search chats"
            placeholder="Search chats…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>

        <div className="convo-list" role="list" aria-label="Conversations">
          <div className="convo-label">This session</div>
          {shown.length === 0 ? <div className="faint" style={{ padding: "4px 12px" }}>No chats match.</div> : null}
          {shown.map((conversation) => (
            <button
              key={conversation.id}
              type="button"
              role="listitem"
              className="convo"
              aria-current={view === "sam" && conversation.id === activeConversation ? "true" : undefined}
              onClick={() => onSelectConversation(conversation.id)}
              title={conversation.title}
            >
              {conversation.title}
            </button>
          ))}
        </div>

        <div className="sidebar-nav">
          {nav.map((item) => (
            <SidebarItem
              key={item.id}
              icon={item.icon}
              label={item.label}
              current={view === item.id}
              onSelect={() => onNavigate(item.id)}
            />
          ))}
        </div>
      </div>
    </nav>
  );
}
