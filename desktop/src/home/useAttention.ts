import { useEffect, useState } from "react";
import type { StatusResponse, VoiceSetupState } from "../bridge/types";
import type { ConnectionState } from "../state";
import { useSession } from "../session";
import { useSam } from "../state";
import type { ViewId } from "../views/viewIds";

export interface AttentionItem {
  id: string;
  label: string;
  tone: "warn" | "danger" | "info";
  /** Where the owner can resolve it (never an automatic action). */
  target?: ViewId;
}

export interface NextUp {
  title: string;
  at: string;
}

const SUBSYSTEM_TEXT: Record<string, string> = {
  database: "Local data is unavailable",
  migrations: "Local data needs attention",
  keychain: "A saved credential couldn't be read",
  career: "Some applications or messages need checking",
};

/**
 * What needs the owner, derived ONLY from real state: backend status and
 * health, voice identity, and (when the owner is present, not in Guest
 * Mode) the Career review queue and unread automation notices. Read-only:
 * nothing here approves, grants or changes anything.
 */
export function useAttention(): { items: AttentionItem[]; nextUp: NextUp | null } {
  const { bridge, status, connection, identity } = useSam();
  const { handsFree } = useSession();
  const [reviews, setReviews] = useState(0);
  const [unread, setUnread] = useState(0);
  const [nextUp, setNextUp] = useState<NextUp | null>(null);
  const guest = identity?.guest.active === true;

  useEffect(() => {
    if (guest || connection === "unavailable" || connection === "starting") return;
    let alive = true;
    const load = async () => {
      try {
        const career = await bridge.careerOverview();
        if (alive) setReviews(career.review_queue?.length ?? 0);
      } catch {
        if (alive) setReviews(0);
      }
      try {
        const proactive = await bridge.proactiveOverview();
        if (!alive) return;
        setUnread((proactive.notifications ?? []).filter((n) => !n.read).length);
        const upcoming = (proactive.tasks ?? [])
          .filter((task) => task.enabled && task.next_run_at)
          .sort((a, b) => String(a.next_run_at).localeCompare(String(b.next_run_at)))[0];
        setNextUp(upcoming?.next_run_at ? { title: upcoming.title, at: upcoming.next_run_at } : null);
      } catch {
        if (alive) {
          setUnread(0);
          setNextUp(null);
        }
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 60_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [bridge, connection, guest]);

  const voice = {
    micDenied: handsFree.micDenied,
    needsSetup: handsFree.needsSetup,
    setupState: handsFree.setupState,
  };
  return {
    items: attentionItems(status, connection, identity?.last_verification ?? null, reviews, unread, voice),
    nextUp,
  };
}

export function attentionItems(
  status: StatusResponse | null,
  connection: ConnectionState,
  verification: string | null,
  reviews: number,
  unread: number,
  voice: { micDenied: boolean; needsSetup: boolean; setupState?: VoiceSetupState | null } = {
    micDenied: false,
    needsSetup: false,
  },
): AttentionItem[] {
  const items: AttentionItem[] = [];
  if (connection === "unavailable") {
    items.push({ id: "offline", label: "Sam's backend isn't reachable", tone: "danger" });
    return items;
  }
  const health = status?.health;
  if (health?.status === "blocked") {
    items.push({ id: "blocked", label: "Sam couldn't start safely", tone: "danger", target: "settings" });
  }
  for (const sub of health?.subsystems ?? []) {
    if (sub.status === "degraded" || sub.status === "unavailable" || sub.status === "blocked") {
      const label = SUBSYSTEM_TEXT[sub.name];
      if (label && health?.status !== "blocked") {
        items.push({
          id: `health-${sub.name}`,
          label,
          tone: "warn",
          target: sub.name === "career" ? "career" : "settings",
        });
      }
    }
  }
  if (status && status.agent !== "configured") {
    items.push({ id: "model", label: "No language model is available", tone: "warn", target: "settings" });
  }
  if (voice.micDenied) {
    // macOS permission, not a Sam permission: only the owner can change it.
    items.push({
      id: "mic",
      label: "Microphone access is off (System Settings › Privacy & Security › Microphone)",
      tone: "warn",
    });
  }
  if (voice.needsSetup) {
    const label =
      voice.setupState === "models_missing"
        ? "Install voice components"
        : voice.setupState === "restart_required"
          ? "Finish voice setup: restart Sam's engine"
          : voice.setupState === "setup_required"
            ? "Set up owner security and your voice"
            : "Set up your voice for hands-free Sam";
    items.push({ id: "voice-setup", label, tone: "info", target: "settings" });
  }
  if (verification === "not_verified") {
    items.push({ id: "owner", label: "Owner verification required", tone: "warn", target: "settings" });
  }
  if (reviews > 0) {
    items.push({ id: "career", label: `${reviews} career item(s) need your review`, tone: "info", target: "career" });
  }
  if (unread > 0) {
    items.push({ id: "automations", label: `${unread} automation notice(s)`, tone: "info", target: "proactive" });
  }
  return items;
}
