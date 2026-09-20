import { useEffect, useState } from "react";
import { useSam } from "../state";
import { NeonButton } from "./primitives";

function clock(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** Always visible while Guest Mode is active; the owner can end it instantly. */
export function GuestBanner() {
  const { identity, refreshIdentity, bridge, t } = useSam();
  const active = identity?.guest.active === true;
  const remoteSeconds = identity?.guest.seconds_remaining ?? 0;
  // The backend's remaining time, anchored to when we received it, so the
  // countdown is correct on the very first frame (no 0:00 flash).
  const [anchor, setAnchor] = useState<{ seconds: number; at: number; active: boolean } | null>(null);
  const [now, setNow] = useState(0);

  useEffect(() => {
    const at = Date.now();
    setAnchor({ seconds: remoteSeconds, at, active });
    setNow(at);
  }, [remoteSeconds, active]);

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);

  // Until the anchor exists, show the backend's own number (no 0:00 flash).
  const fresh = anchor !== null && anchor.seconds === remoteSeconds && anchor.active === active;
  const left =
    fresh && anchor
      ? Math.max(0, anchor.seconds - Math.max(0, Math.floor((now - anchor.at) / 1000)))
      : remoteSeconds;

  useEffect(() => {
    if (active && left === 0) void refreshIdentity(); // expiry is enforced by the backend
  }, [active, left, refreshIdentity]);

  if (!active) return null;
  return (
    <div className="guest-banner" role="status" aria-live="polite">
      <strong>{t("guest.banner")}</strong>
      <span>
        {t("guest.expiresIn")} {clock(left)}
      </span>
      <span className="spacer" />
      <NeonButton
        variant="danger"
        onClick={() => void bridge.guestEnd().finally(() => void refreshIdentity())}
      >
        {t("guest.end")}
      </NeonButton>
    </div>
  );
}
