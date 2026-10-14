import { useEffect, useState } from "react";
import { speechProblem } from "../lib/samVoice";
import { getCaptureDiagnostics } from "../lib/voiceDiagnostics";
import type { StatusResponse, VoiceSetupState, IdentityStatus, SamVoiceStatus } from "../bridge/types";
import type { ConnectionState } from "../state";
import { useSession } from "../session";
import { useSam } from "../state";
import type { ViewId } from "../views/viewIds";

export interface AttentionItem {
  id: string;
  label: string;
  reason_code: string;
  subsystem: string;
  explanation: string;
  blocks: string[];
  state: string;
  action: string;
  retryVoice?: boolean;
  retryModel?: boolean;
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
  const { bridge, status, connection, identity, samVoice } = useSam();
  const { handsFree, identityIssue, speechIssue } = useSession();
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
    micFailed: handsFree.micFailed,
    micReason: handsFree.micFailed ? getCaptureDiagnostics().reason : null,
    identity, samVoice, identityIssue, speechIssue,
    needsSetup: handsFree.needsSetup,
    setupState: handsFree.setupState,
  };
  return {
    items: attentionItems(status, connection, identity?.last_verification ?? null, reviews, unread, voice),
    nextUp,
  };
}

export interface VoiceAttentionState {
  micDenied: boolean;
  needsSetup: boolean;
  micFailed?: boolean;
  micReason?: string | null;
  setupState?: VoiceSetupState | null;
  identity?: IdentityStatus | null;
  samVoice?: SamVoiceStatus | null;
  identityIssue?: string | null;
  speechIssue?: string | null;
}

export function attentionItems(
  status: StatusResponse | null,
  connection: ConnectionState,
  _verification: string | null,
  reviews: number,
  unread: number,
  voice: VoiceAttentionState = { micDenied: false, needsSetup: false },
): AttentionItem[] {
  const items: AttentionItem[] = [];
  const add = (id: string, label: string, reason: string, explanation: string, blocks: string[], action: string,
    target?: ViewId, state = "Needs attention", retryVoice = false) => {
    if (!items.some((item) => item.id === id)) items.push({ id, label, reason_code: reason,
      subsystem: id, explanation, blocks, action, target, state, retryVoice, tone: "warn" });
  };
  if (connection === "unavailable") {
    add("offline", "Sam engine", "backend_unreachable", "Sam's local engine is not responding.",
      ["Voice conversation", "Wake greeting"], "Check connection", "settings");
    return items;
  }
  const health = status?.health;
  if (health?.status === "blocked") {
    add("blocked", "Sam startup", "startup_blocked", "Sam couldn't start safely. Review the health details in Settings.",
      ["Conversation", "Speech", "Wake mode"], "Review startup", "settings");
    return items;
  }
  for (const sub of health?.subsystems ?? []) {
    if (["degraded", "unavailable", "blocked"].includes(sub.status) && SUBSYSTEM_TEXT[sub.name]) {
      add(`health-${sub.name}`, sub.name === "keychain" ? "Secure storage" : "Sam health",
        `${sub.name}_unavailable`, SUBSYSTEM_TEXT[sub.name]!, [sub.name], "Review", sub.name === "career" ? "career" : "settings");
    }
  }
  if (status && status.agent !== "configured") {
    const problem = status.model_issue;
    add("model", problem?.message ?? "No configured model provider is ready", problem?.reason_code ?? "language_model_unavailable",
      problem?.resolution ?? "Review AI providers in Settings. Local voice preview and the wake greeting are independent of the model.",
      ["Conversation answers"], "Review AI providers", "settings", "Unavailable");
    items[items.length - 1]!.retryModel = true;
  }
  if (voice.micDenied) {
    add("mic", "Microphone", "microphone_permission_required",
      "Allow Sam in macOS System Settings → Privacy & Security → Microphone, then retry.",
      ["Voice input", "Wake mode"], "Retry microphone", undefined, "Permission required", true);
  } else if (voice.micFailed) {
    add("mic", "Voice listener", voice.micReason ?? "microphone_capture_failed",
      voice.micReason === "microphone_start_timeout"
        ? "macOS did not open the microphone. Check Sam's microphone permission and the selected input in System Settings, then retry."
        : "The microphone listener stopped. Retry to reopen local capture.",
      ["Voice input", "Wake mode"], "Retry listener", undefined, "Stopped", true);
  }
  const identity = voice.identity;
  const setup = identity?.setup_state ?? voice.setupState;
  if (identity?.enrolled === true && identity.profile_compatible !== false) {
    // Last verification is history, not a persistent setup blocker. Only a
    // failure from the current voice session asks for a retry.
    if (voice.identityIssue) add("owner", "Owner voice", voice.identityIssue,
      "The last recording could not verify you. Your profile is enrolled; try speaking again.",
      ["Owner voice conversation", "Verified wake greeting"], "Try again", undefined, "Retry needed", true);
  } else if (identity?.reason_code === "profile_load_failed" || identity?.enrolled === null && identity.available) {
    add("owner", "Owner voice", identity.reason_code ?? "profile_load_failed", "The saved owner profile couldn't be loaded.",
      ["Owner voice conversation", "Verified wake greeting"], "Check voice profile", "settings", "Unavailable");
  } else if (voice.needsSetup || identity?.available && setup !== "enrolled") {
    const detail = setup === "models_missing" ? ["voice_models_missing", "Install the local voice components.", "Install components"]
      : setup === "restart_required" ? ["voice_restart_required", "The installed components need an engine restart.", "Finish setup"]
      : setup === "voice_unavailable" ? ["identity_not_configured", "Local owner verification is unavailable.", "Check voice setup"]
      : identity?.profile_compatible === false ? ["profile_model_mismatch", "The saved profile is incompatible with the selected speaker model.", "Review profile"]
      : ["voice_not_enrolled", "Set up owner voice before starting a voice conversation.", "Set up your voice"];
    add("owner", "Owner voice", detail[0]!, detail[1]!, ["Owner voice conversation", "Verified wake greeting"], detail[2]!, "settings");
  }
  const speech = voice.samVoice;
  const speechReason = voice.speechIssue ?? (speech?.state !== "SAM_VOICE_READY" ? speech?.reason_code : null);
  if (speechReason) add("sam-voice", "Sam Voice", speechReason, speechProblem(speechReason),
    ["Spoken greeting", "Spoken answers"], "Set up / preview voice", "settings", speech?.state === "SAM_VOICE_PERMISSION_REQUIRED" ? "Permission required" : "Unavailable");
  if (reviews > 0) add("career", "Career review", "career_review_pending", `${reviews} career item(s) need your review.`,
    [], "Review items", "career");
  if (unread > 0) add("automations", "Automation notices", "automation_notices_unread", `${unread} automation notice(s) are unread.`,
    [], "Read notices", "proactive");
  return items;
}
