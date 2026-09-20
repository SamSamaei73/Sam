import type { Capability, StatusResponse } from "../bridge/types";
import { StatusPill, type Tone } from "./primitives";

const COPY: Record<Capability, { label: string; tone: Tone }> = {
  available: { label: "Available", tone: "ok" },
  configured: { label: "Configured", tone: "ok" },
  not_configured: { label: "Not configured", tone: "muted" },
  foundation_ready: { label: "Foundation ready", tone: "info" },
  unavailable: { label: "Unavailable", tone: "danger" },
};

/** OpenJarvis-style right-hand system panel: read-only capability status. */
export function SystemPanel({ status }: { status: StatusResponse | null }) {
  const rows: [string, Capability | undefined][] = [
    ["Language model", status?.agent],
    ["Knowledge", status?.knowledge],
    ["Memory", status?.memory],
    ["Tools", status?.tools],
    ["Voice input", status?.voice_input],
    ["Read aloud", status?.speech_output],
    ["Computer control", status?.computer_control],
    ["Coding agent", status?.coding_agent],
  ];
  return (
    <aside className="glass system-panel" aria-label="System panel">
      <h3>System</h3>
      {rows.map(([name, value]) => (
        <div className="kv" key={name}>
          <span className="muted">{name}</span>
          {value ? (
            <StatusPill tone={COPY[value].tone} label={COPY[value].label} />
          ) : (
            <span className="faint">Checking…</span>
          )}
        </div>
      ))}
      <h3 style={{ marginTop: 22 }}>Session</h3>
      <div className="kv">
        <span className="muted">Conversations</span>
        <span>This window only</span>
      </div>
      <div className="kv">
        <span className="muted">Memory storage</span>
        <span>{status?.memory_storage === "in_process" ? "In process" : "—"}</span>
      </div>
    </aside>
  );
}
