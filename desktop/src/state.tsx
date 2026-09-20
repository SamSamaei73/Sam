import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { SamBridge } from "./bridge/bridge";
import { BridgeError, toBridgeError } from "./bridge/bridge";
import type { Challenge, OperationResult, StatusResponse } from "./bridge/types";
import { ConfirmationDialog } from "./components/ConfirmationDialog";
import { loadPrefs, savePrefs, type Prefs } from "./lib/prefs";

export type ConnectionState = "starting" | "connected" | "degraded" | "unavailable";

export function deriveConnection(status: StatusResponse | null, failed: boolean): ConnectionState {
  if (failed) return "unavailable";
  if (!status) return "starting";
  if (status.backend.status !== "ok") return "degraded";
  return status.agent === "configured" ? "connected" : "degraded";
}

interface SamContextValue {
  bridge: SamBridge;
  status: StatusResponse | null;
  connection: ConnectionState;
  refreshStatus: () => Promise<void>;
  prefs: Prefs;
  updatePrefs: (patch: Partial<Prefs>) => void;
  /**
   * Run an operation; when the backend answers `confirmation_required`, show
   * the confirmation dialog. Approve/Deny only *records the human's answer*
   * with the backend. The operation is then retried and the backend's
   * PermissionEngine decides again — the UI never assumes it was allowed.
   */
  confirmable: <T extends OperationResult>(run: (confirmationId?: string) => Promise<T>) => Promise<T>;
}

const SamContext = createContext<SamContextValue | null>(null);

export function useSam(): SamContextValue {
  const value = useContext(SamContext);
  if (!value) throw new Error("SamProvider is missing");
  return value;
}

interface Decision {
  approved: boolean;
  stepUp?: string;
}

interface PendingConfirmation {
  challenge: Challenge;
  error: string | null;
  resolve: (decision: Decision) => void;
}

export const STATUS_POLL_MS = 30_000;

export function SamProvider({ bridge, children }: { bridge: SamBridge; children: ReactNode }) {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [failed, setFailed] = useState(false);
  const [prefs, setPrefs] = useState<Prefs>(() => loadPrefs());
  const [pending, setPending] = useState<PendingConfirmation | null>(null);
  const alive = useRef(true);

  const refreshStatus = useCallback(async () => {
    try {
      const next = await bridge.status();
      if (!alive.current) return;
      setStatus(next);
      setFailed(false);
    } catch {
      if (!alive.current) return;
      setFailed(true);
    }
  }, [bridge]);

  useEffect(() => {
    alive.current = true;
    void refreshStatus();
    const timer = window.setInterval(() => void refreshStatus(), STATUS_POLL_MS);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
    };
  }, [refreshStatus]);

  useEffect(() => {
    document.documentElement.dataset.reducedMotion = prefs.reducedMotion ? "true" : "false";
  }, [prefs.reducedMotion]);

  const updatePrefs = useCallback((patch: Partial<Prefs>) => {
    setPrefs((current) => {
      const next = { ...current, ...patch };
      savePrefs(next);
      return next;
    });
  }, []);

  const confirmable = useCallback(
    async <T extends OperationResult>(run: (confirmationId?: string) => Promise<T>): Promise<T> => {
      const first = await run();
      const challenge = first.challenge;
      if (first.status !== "confirmation_required" || !challenge) return first;
      const denied = (message: string): T => ({
        ...first,
        status: "denied",
        reason_code: "confirmation_denied",
        message,
        challenge: null,
      });
      let error: string | null = null;
      for (;;) {
        const decision = await new Promise<Decision>((resolve) =>
          setPending({ challenge, error, resolve }),
        );
        try {
          await bridge.decideConfirmation(
            challenge.confirmation_id,
            decision.approved,
            decision.stepUp,
          );
          setPending(null);
          if (!decision.approved) return denied("You declined, so nothing was changed.");
          return run(challenge.confirmation_id);
        } catch (failure) {
          const known = failure instanceof BridgeError ? failure : toBridgeError(failure);
          if (known.code === "step_up_failed") {
            error = known.message; // ask again; the backend counts attempts
            continue;
          }
          setPending(null);
          if (known.code === "step_up_locked" || known.code === "step_up_unavailable") {
            return denied(known.message);
          }
          throw known;
        }
      }
    },
    [bridge],
  );

  const value = useMemo<SamContextValue>(
    () => ({
      bridge,
      status,
      connection: deriveConnection(status, failed),
      refreshStatus,
      prefs,
      updatePrefs,
      confirmable,
    }),
    [bridge, status, failed, refreshStatus, prefs, updatePrefs, confirmable],
  );

  return (
    <SamContext.Provider value={value}>
      {children}
      {pending ? (
        <ConfirmationDialog
          challenge={pending.challenge}
          error={pending.error}
          onApprove={(stepUp) => pending.resolve({ approved: true, stepUp })}
          onDeny={() => pending.resolve({ approved: false })}
        />
      ) : null}
    </SamContext.Provider>
  );
}
