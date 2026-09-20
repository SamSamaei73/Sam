import type { Capability } from "../bridge/types";
import { Notice, SectionHeader, StatusPill, type Tone } from "../components/primitives";
import { ConnectionIndicator } from "../components/ConnectionIndicator";
import { useSam } from "../state";

const CAPABILITY: Record<Capability, { label: string; tone: Tone }> = {
  available: { label: "Available", tone: "ok" },
  configured: { label: "Configured", tone: "ok" },
  not_configured: { label: "Not configured", tone: "muted" },
  foundation_ready: { label: "Foundation ready", tone: "info" },
  unavailable: { label: "Unavailable", tone: "danger" },
};

export function SettingsView() {
  const { status, connection, prefs, updatePrefs, refreshStatus } = useSam();
  const rows: [string, Capability | undefined, string][] = [
    ["Sam (language model)", status?.agent, "Answers your messages."],
    ["Knowledge", status?.knowledge, "Your documents, kept for this session."],
    ["Memory", status?.memory, "Read-only view of what Sam remembers."],
    ["Tools & integrations", status?.tools, "Connectors registered by trusted configuration."],
    ["Voice input", status?.voice_input, "Push-to-record speech understanding."],
    ["Read aloud", status?.speech_output, "Speech generation for Sam's replies."],
    ["Computer control", status?.computer_control, "Not available from this window."],
    ["Coding agent", status?.coding_agent, "Not available from this window."],
  ];
  const profiles = status?.speech_profiles ?? [];

  return (
    <div className="page-narrow">
      <SectionHeader title="Settings" description="Connection status and interface preferences." />
      <section className="glass panel" aria-labelledby="conn-h">
        <h3 id="conn-h">Connection</h3>
        <div className="card-row">
          <ConnectionIndicator state={connection} />
          <button type="button" className="neon-button" data-variant="quiet" onClick={() => void refreshStatus()}>
            Check again
          </button>
        </div>
        <p className="faint" style={{ marginBottom: 0 }}>
          The app talks to Sam's engine on this computer only. The address and credentials are held by the app shell —
          they're never shown or editable here.
        </p>
      </section>

      <section className="glass panel" aria-labelledby="cap-h">
        <h3 id="cap-h">Capabilities</h3>
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Capability</th>
              <th scope="col">Status</th>
              <th scope="col">Notes</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([name, value, note]) => {
              const copy = value ? CAPABILITY[value] : null;
              return (
                <tr key={name}>
                  <th scope="row" style={{ textTransform: "none", letterSpacing: 0, fontSize: 14, color: "inherit" }}>
                    {name}
                  </th>
                  <td>{copy ? <StatusPill tone={copy.tone} label={copy.label} /> : <span className="faint">Checking…</span>}</td>
                  <td className="muted">{note}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <section className="glass panel" aria-labelledby="pref-h">
        <h3 id="pref-h">Interface</h3>
        <div className="row" style={{ flexDirection: "column", alignItems: "flex-start", gap: 14 }}>
          <label className="row">
            <input
              type="checkbox"
              checked={prefs.reducedMotion}
              onChange={(event) => updatePrefs({ reducedMotion: event.target.checked })}
            />
            <span>Reduce motion</span>
          </label>
          <label className="row">
            <input
              type="checkbox"
              checked={prefs.sidebarCollapsed}
              onChange={(event) => updatePrefs({ sidebarCollapsed: event.target.checked })}
            />
            <span>Collapse the sidebar</span>
          </label>
          {profiles.length > 0 ? (
            <label className="row">
              <span>Read-aloud voice</span>
              <select
                className="field"
                style={{ width: "auto" }}
                value={prefs.readAloudVoice ?? profiles[0]?.profile_id ?? ""}
                onChange={(event) => updatePrefs({ readAloudVoice: event.target.value })}
              >
                {profiles.map((profile) => (
                  <option key={profile.profile_id} value={profile.profile_id}>
                    {profile.profile_id}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </div>
      </section>

      <Notice live={false}>
        Privacy: conversation history is kept only in this window and disappears when you close it. Only the
        interface preferences above are stored on this device — never messages, transcripts, documents or
        credentials.
      </Notice>
    </div>
  );
}
