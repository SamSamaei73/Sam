import type { ReactNode } from "react";
import type { Capability, StatusResponse, SystemHealth } from "../bridge/types";
import { StatusPill, type Tone } from "./primitives";

const COPY: Record<Capability, { label: string; tone: Tone }> = {
  available: { label: "Available", tone: "ok" },
  configured: { label: "Configured", tone: "ok" },
  not_configured: { label: "Not configured", tone: "muted" },
  foundation_ready: { label: "Foundation ready", tone: "info" },
  unavailable: { label: "Unavailable", tone: "danger" },
};

/** OpenJarvis-style right-hand system panel: read-only capability status. */
export function SystemPanel({ status, children }: { status: StatusResponse | null; children?: ReactNode }) {
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
      {children}
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
      {status?.health ? <HealthSection health={status.health} /> : null}
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

const OVERALL: Record<SystemHealth["status"], { label: string; tone: Tone }> = {
  ready: { label: "Ready", tone: "ok" },
  degraded: { label: "Needs attention", tone: "warn" },
  blocked: { label: "Blocked", tone: "danger" },
};

/** Reviewed, static explanations for reason codes. Anything else is shown
 * generically: health never carries raw errors, paths or data. */
const REASONS: Record<string, string> = {
  another_instance_running: "Sam is already running. Close the other copy.",
  database_corrupt: "The local database failed its integrity check. Restore a backup; nothing was changed.",
  database_open_failed: "The local database could not be opened. Nothing was changed.",
  migration_failed: "Updating the local database failed and was rolled back. Nothing was changed.",
  schema_from_future: "The local data comes from a newer Sam. Use that version or restore a backup.",
  production_requires_durable_storage: "This build must store data on this Mac but was configured not to.",
  production_debug_logging: "Debug logging is not allowed in this build.",
  storage_path_is_symlink: "The data folder is not safe (a link). Nothing was changed.",
  storage_permissions_too_open: "The data folder is not private. Nothing was changed.",
  storage_not_owned_by_user: "The data folder belongs to another user. Nothing was changed.",
  keychain_unavailable: "The Keychain could not be used, so providers stored there are off.",
  credential_unavailable: "A saved credential could not be read, so that provider is off.",
  reconciliation_required: "Some sends or submissions may or may not have happened. Sam will not retry them.",
};

const DEGRADED = new Set(["degraded", "unavailable", "blocked"]);

function words(value: string): string {
  return value.replace(/_/g, " ");
}

function HealthSection({ health }: { health: SystemHealth }) {
  const problems = health.subsystems.filter((s) => DEGRADED.has(s.status));
  const reason = health.reason_code ? (REASONS[health.reason_code] ?? "Sam could not start safely.") : null;
  return (
    <section aria-label="Sam health">
      <h3 style={{ marginTop: 22 }}>Health</h3>
      <div className="kv">
        <span className="muted">Sam</span>
        <StatusPill tone={OVERALL[health.status].tone} label={OVERALL[health.status].label} />
      </div>
      {reason ? <p className="faint">{reason}</p> : null}
      <div className="kv">
        <span className="muted">Local data</span>
        <span>
          {health.storage_mode === "sqlite"
            ? `Saved on this Mac${health.schema_version ? ` · schema v${health.schema_version}` : ""}`
            : "This session only"}
        </span>
      </div>
      <div className="kv">
        <span className="muted">Last backup</span>
        <span>{health.last_backup_at ? health.last_backup_at.slice(0, 10) : "None yet"}</span>
      </div>
      <div className="kv">
        <span className="muted">Scheduling</span>
        <span>
          {health.scheduler === "off" ? "Off" : health.scheduler === "on" ? "On" : "On (stays on after restart)"}
        </span>
      </div>
      {health.reconciliation_required > 0 ? (
        <p className="faint">
          {health.reconciliation_required} item(s) need checking: {REASONS.reconciliation_required}
        </p>
      ) : null}
      {problems.length > 0 ? (
        <ul aria-label="Needs attention" className="faint">
          {problems.map((p) => (
            <li key={p.name}>
              {words(p.name)}: {p.reason_code && REASONS[p.reason_code] ? REASONS[p.reason_code] : words(p.status)}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
