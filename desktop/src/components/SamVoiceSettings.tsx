import { useState } from "react";
import { useSam } from "../state";
import { useSession } from "../session";
import { speechProblem } from "../lib/samVoice";
import { SamPresence } from "../home/SamPresence";

export function SamVoiceSettings() {
  const { samVoice, setSamVoice, refreshSamVoice, identity, prefs } = useSam();
  const session = useSession();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const guest = identity?.guest.active === true;
  const fa = prefs.uiLanguage === "fa";
  const change = async (enabled: boolean, external = samVoice?.external_approved ?? false) => {
    if (!enabled) session.stopSpeaking(); // stop audio immediately, before IPC
    setSaving(true);
    setError(null);
    try { await setSamVoice(enabled, external); }
    catch { setError("The speech setting couldn't be saved. Try again."); }
    finally { setSaving(false); }
  };
  return <section className="glass panel" aria-labelledby="sam-voice-heading">
    <h3 id="sam-voice-heading">{fa ? "صدای سام" : "Sam Voice"}</h3>
    <label className="row">
      <input type="checkbox" checked={samVoice?.enabled ?? false} disabled={guest || saving || !samVoice}
        onChange={(e) => void change(e.target.checked)} />
      <span>{fa ? "سام پاسخ‌ها را با صدا می‌گوید" : "Sam speaks responses"}</span>
    </label>
    <p>{samVoice?.voice_name
      ? `${samVoice.voice_name} · ${samVoice.language} · ${samVoice.external ? samVoice.provider : "On this Mac"}`
      : "No trusted voice is available for the selected language."}</p>
    {samVoice?.external ? <>
      <p>Speech text is sent to {samVoice.provider} for processing. Secret content is withheld; private content stays local. Sam does not switch providers or billing automatically.</p>
      <label className="row"><input type="checkbox" checked={samVoice.external_approved}
        disabled={guest || saving} onChange={(e) => void change(samVoice.enabled, e.target.checked)} />
        <span>Allow this configured provider to speak Sam's normal responses</span></label>
    </> : <p className="muted">Speech is generated on this Mac. No speech text is sent to an external provider.</p>}
    {samVoice && samVoice.state !== "SAM_VOICE_READY" ? <p role="status">{speechProblem(samVoice.reason_code)}</p> : null}
    <div className="row">
      <button className="neon-button" disabled={guest || saving || session.previewingVoice || samVoice?.state !== "SAM_VOICE_READY"}
        onClick={() => void session.previewSamVoice()}>{fa ? "پیش‌نمایش صدای سام" : "Preview Sam's voice"}</button>
      <button className="link-button" disabled={guest} onClick={() => void refreshSamVoice()}>{fa ? "بررسی دوباره" : "Check voice again"}</button>
      {session.previewingVoice ? <button className="link-button" onClick={session.stopSpeaking}>Stop preview</button> : null}
    </div>
    {session.previewingVoice ? <div className="voice-preview">
      <p role="status">{session.voice === "speaking" ? (fa ? "سام در حال صحبت است" : "Sam is speaking…") : (fa ? "در حال آماده‌سازی صدا" : "Preparing Sam's voice…")}</p>
      <SamPresence state={session.voice} level={0} attention={false} guest={false} />
    </div> : null}
    {session.speechIssue || error ? <p role="alert">{error ?? speechProblem(session.speechIssue)}</p> : null}
    <p className="faint">Preview uses a fixed phrase and makes no language-model request. This setting grants no permission to send messages, submit applications, or delete anything.</p>
  </section>;
}
