import type { ConnectionState } from "../state";
import { useSam } from "../state";
import { StatusPill, type Tone } from "./primitives";

const TONES: Record<ConnectionState, Tone> = {
  starting: "info",
  connected: "ok",
  degraded: "warn",
  unavailable: "danger",
};

export function ConnectionIndicator({ state }: { state: ConnectionState }) {
  const { t } = useSam();
  const label = t(`conn.${state}`);
  return (
    <span role="status" aria-live="polite" aria-label={`Connection: ${label}`}>
      <StatusPill tone={TONES[state]} label={label} />
    </span>
  );
}
