import { useSam } from "../state";
import { useSession } from "../session";
import { useState, useSyncExternalStore } from "react";
import { getCaptureDiagnostics, subscribeCaptureDiagnostics } from "../lib/voiceDiagnostics";

function CaptureReadings() {
  const capture = useSyncExternalStore(subscribeCaptureDiagnostics, getCaptureDiagnostics);
  const number = (value: number | null) => value === null ? "—" : value.toFixed(1);
  return <>
    <p>Wake listener: {capture.listener}</p>
    <p>Microphone: {capture.microphone} · {capture.device ?? "Not opened"}</p>
    <p>RMS: {number(capture.rmsDbfs)} dBFS · Peak: {number(capture.peakDbfs)} dBFS</p>
    <p>Non-zero samples: {capture.nonZeroRatio === null ? "—" : (capture.nonZeroRatio * 100).toFixed(1)}% · Highest RMS: {number(capture.maxRmsDbfs)} dBFS</p>
    {capture.reason ? <p>{capture.reason === "microphone_digital_silence" ? "Input is digitally silent. If you are speaking, check the selected microphone and macOS permission." : capture.reason === "microphone_start_timeout" ? "microphone_start_timeout: macOS did not open the microphone. Check Sam's microphone permission and the selected input device, then retry the listener." : capture.reason}</p> : null}
  </>;
}

/** Owner-visible safe runtime state; no recordings or idle transcripts. */
export function VoiceDiagnostics() {
  const { status, identity, samVoice } = useSam();
  const { handsFree, voice, speechIssue, speakingId } = useSession();
  const [expanded, setExpanded] = useState(false);
  const session = voice === "verifying" || voice === "waking" ? "VERIFYING"
    : handsFree.phase === "attending" ? "LISTENING"
    : handsFree.phase === "greeting" || speakingId ? "SPEAKING"
    : handsFree.phase.toUpperCase();
  return <details className="context-block" aria-label="Voice diagnostics" onToggle={(event) => setExpanded(event.currentTarget.open)}>
    <summary>Voice diagnostics</summary>
    {expanded ? <CaptureReadings /> : null}
    <p>Wake model: {handsFree.lastWake === "wake_recognizer_unavailable" ? "ERROR" : status?.voice_activation === "unavailable" ? "MISSING" : "READY"}</p>
    <p>Owner profile: {identity?.enrolled === true && identity.profile_compatible !== false ? "ENROLLED" : identity?.enrolled === false ? "NOT_ENROLLED" : "ERROR"}</p>
    <p>TTS: {speechIssue ? "ERROR" : samVoice?.state === "SAM_VOICE_READY" ? "READY" : "UNAVAILABLE"}</p>
    <p>Voice session: {session}</p>
    <p>Wake checks: {handsFree.wakeChecks} · Verified wakes: {handsFree.ownerWakes}</p>
    <p>Last wake result: {handsFree.lastWake ?? "No check yet"}</p>
  </details>;
}
