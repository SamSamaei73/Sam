import type { Capability } from "../bridge/types";
import { Notice, SectionHeader, StatusPill, type Tone } from "../components/primitives";
import { ConnectionIndicator } from "../components/ConnectionIndicator";
import { useState } from "react";
import { ModelSettings } from "../components/ModelSettings";
import { EnrollmentDialog, GuestDialog, RemoveProfileDialog } from "../components/IdentityDialogs";
import { NeonButton } from "../components/primitives";
import { useSam } from "../state";

const CAPABILITY: Record<Capability, { label: string; tone: Tone }> = {
  available: { label: "Available", tone: "ok" },
  configured: { label: "Configured", tone: "ok" },
  not_configured: { label: "Not configured", tone: "muted" },
  foundation_ready: { label: "Foundation ready", tone: "info" },
  unavailable: { label: "Unavailable", tone: "danger" },
};

function SelectRow({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: [string, string][];
}) {
  return (
    <label className="row">
      <span>{label}</span>
      <select className="field" style={{ width: "auto" }} value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map(([v, text]) => (
          <option key={v} value={v}>
            {text}
          </option>
        ))}
      </select>
    </label>
  );
}

function capabilityPill(value: string | undefined, t: (k: "settings.configured" | "settings.notConfigured") => string) {
  const on = value === "configured";
  return <StatusPill tone={on ? "ok" : "muted"} label={on ? t("settings.configured") : t("settings.notConfigured")} />;
}

export function SettingsView() {
  const { status, connection, prefs, updatePrefs, refreshStatus, identity, refreshIdentity, bridge, t } = useSam();
  const [dialog, setDialog] = useState<null | "enroll" | "reEnroll" | "remove" | "guest">(null);
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
      <SectionHeader title={t("settings.title")} description="Connection status and interface preferences." />
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

      <section className="glass panel" aria-labelledby="lang-h">
        <h3 id="lang-h">{t("settings.language")}</h3>
        <div className="row" style={{ flexDirection: "column", alignItems: "flex-start", gap: 14 }}>
          <SelectRow
            label={t("settings.interfaceLanguage")}
            value={prefs.uiLanguage}
            onChange={(v) => updatePrefs({ uiLanguage: v === "fa" ? "fa" : "en" })}
            options={[["en", "English"], ["fa", "فارسی"]]}
          />
          <SelectRow
            label={t("settings.responseLanguage")}
            value={prefs.responseLanguage}
            onChange={(v) => updatePrefs({ responseLanguage: v === "fa" || v === "en" ? v : "auto" })}
            options={[["auto", t("settings.lang.auto")], ["fa", t("settings.lang.fa")], ["en", t("settings.lang.en")]]}
          />
        </div>
      </section>

      <section className="glass panel" aria-labelledby="owner-h">
        <h3 id="owner-h">{t("settings.ownerVoice")}</h3>
        {!identity?.available ? (
          <p className="muted">{t("settings.voiceIdentityOff")}</p>
        ) : (
          <>
            <div className="card-row" style={{ marginBottom: 10 }}>
              <span className="muted">{t("settings.ownerVoice")}</span>
              <StatusPill
                tone={identity.enrolled === true ? "ok" : identity.enrolled === false ? "muted" : "warn"}
                label={
                  identity.enrolled === true
                    ? t("settings.enrolled")
                    : identity.enrolled === false
                      ? t("settings.notEnrolled")
                      : t("settings.enrollUnknown")
                }
              />
            </div>
            <div className="card-row" style={{ marginBottom: 14 }}>
              <span className="muted">{t("settings.lastVerification")}</span>
              <span>
                {identity.last_verification
                  ? t(`settings.verification.${identity.last_verification}`)
                  : t("settings.verification.none")}
              </span>
            </div>
            <div className="row">
              {identity.enrolled === false ? (
                <NeonButton onClick={() => setDialog("enroll")}>{t("settings.enroll")}</NeonButton>
              ) : null}
              {identity.enrolled === true ? (
                <>
                  <NeonButton variant="quiet" onClick={() => setDialog("reEnroll")}>
                    {t("settings.reEnroll")}
                  </NeonButton>
                  <NeonButton variant="danger" onClick={() => setDialog("remove")}>
                    {t("settings.remove")}
                  </NeonButton>
                </>
              ) : null}
            </div>
          </>
        )}
        <p className="faint" style={{ marginBottom: 0 }}>
          {t("settings.privacy")}
        </p>
      </section>

      <section className="glass panel" aria-labelledby="guest-h">
        <h3 id="guest-h">{t("settings.guestMode")}</h3>
        <div className="card-row" style={{ marginBottom: 12 }}>
          <span className="muted">{t("settings.guestMode")}</span>
          <StatusPill
            tone={identity?.guest.active ? "warn" : "muted"}
            label={identity?.guest.active ? t("guest.active") : t("guest.off")}
          />
        </div>
        {identity?.guest.active ? (
          <NeonButton
            variant="danger"
            onClick={() => void bridge.guestEnd().finally(() => void refreshIdentity())}
          >
            {t("guest.end")}
          </NeonButton>
        ) : (
          <>
            <NeonButton disabled={identity?.enrolled !== true} onClick={() => setDialog("guest")}>
              {t("guest.start")}
            </NeonButton>
            {identity?.available && identity.enrolled !== true ? (
              <p className="faint">{t("guest.needsEnrollment")}</p>
            ) : null}
          </>
        )}
      </section>

      <ModelSettings />

      <section className="glass panel" aria-labelledby="vp-h">
        <h3 id="vp-h">{t("settings.voiceProcessing")}</h3>
        <div className="kv">
          <span className="muted">{t("settings.localStt")}</span>
          {capabilityPill(identity?.local_stt, t)}
        </div>
        <div className="kv">
          <span className="muted">{t("settings.speakerModel")}</span>
          {capabilityPill(identity?.speaker_model, t)}
        </div>
        <div className="kv">
          <span className="muted">{t("settings.persianTts")}</span>
          {capabilityPill(status?.persian_tts, t)}
        </div>
        <p className="muted small" data-testid="persian-tts-note">
          {t("settings.persianTtsNote")}
        </p>
      </section>

      {dialog === "enroll" || dialog === "reEnroll" ? (
        <EnrollmentDialog
          reEnroll={dialog === "reEnroll"}
          onClose={() => setDialog(null)}
          onDone={() => void refreshIdentity()}
        />
      ) : null}
      {dialog === "remove" ? (
        <RemoveProfileDialog onClose={() => setDialog(null)} onDone={() => void refreshIdentity()} />
      ) : null}
      {dialog === "guest" ? (
        <GuestDialog onClose={() => setDialog(null)} onDone={() => void refreshIdentity()} />
      ) : null}

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
