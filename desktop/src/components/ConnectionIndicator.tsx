import type { ConnectionState } from "../state";
import { StatusPill, type Tone } from "./primitives";

const COPY: Record<ConnectionState, { label: string; tone: Tone; detail: string }> = {
  starting: { label: "Starting", tone: "info", detail: "Connecting to Sam's backend" },
  connected: { label: "Connected", tone: "ok", detail: "" },
  degraded: { label: "Degraded", tone: "warn", detail: "Some capabilities aren't available" },
  unavailable: { label: "Unavailable", tone: "danger", detail: "Can't reach Sam's backend" },
};

export function ConnectionIndicator({ state }: { state: ConnectionState }) {
  const copy = COPY[state];
  return (
    <span role="status" aria-live="polite" aria-label={`Connection: ${copy.label}`}>
      <StatusPill tone={copy.tone} label={copy.label} detail={copy.detail} />
    </span>
  );
}
