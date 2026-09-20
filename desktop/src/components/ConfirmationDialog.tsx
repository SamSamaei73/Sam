import { useEffect, useId, useRef, useState } from "react";
import type { Challenge } from "../bridge/types";
import { humanize } from "../lib/format";
import { useSam } from "../state";
import { NeonButton, StatusPill } from "./primitives";


const FOCUSABLE =
  'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';

/**
 * Approve/Deny for ONE backend-issued confirmation.
 *
 * Every displayed field comes from the backend's own record and is rendered
 * as plain text (it is untrusted, e.g. a document name). The dialog can only
 * answer yes/no: it cannot edit the scope, target or risk. Deny is the
 * initial focus and Escape denies. Approving does not itself allow anything:
 * the PermissionEngine re-checks when the operation is retried.
 *
 * CRITICAL confirmations need real step-up authentication: the user types a
 * secret that only the backend can verify. A checkbox or click is never
 * enough, and if the backend has no step-up secret configured it refuses the
 * approval outright.
 */
export function ConfirmationDialog({
  challenge,
  error,
  onApprove,
  onDeny,
}: {
  challenge: Challenge;
  error: string | null;
  onApprove: (stepUp?: string) => void;
  onDeny: () => void;
}) {
  const { t } = useSam();
  const titleId = useId();
  const descId = useId();
  const ref = useRef<HTMLDivElement>(null);
  const denyRef = useRef<HTMLButtonElement>(null);
  const [stepUp, setStepUp] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const critical = challenge.risk === "critical";

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    denyRef.current?.focus();
    return () => previous?.focus?.();
  }, []);

  // A rejected attempt re-opens the dialog for another try (secret cleared).
  useEffect(() => {
    if (error) {
      setSubmitted(false);
      setStepUp("");
    }
  }, [error]);

  const approve = () => {
    if (submitted) return;
    setSubmitted(true);
    onApprove(critical ? stepUp : undefined);
    setStepUp("");
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      onDeny();
      return;
    }
    if (event.key !== "Tab" || !ref.current) return;
    const nodes = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (!first || !last) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="overlay">
      <div
        ref={ref}
        className="dialog rim"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        data-risk={challenge.risk}
        onKeyDown={onKeyDown}
      >
        <h2 id={titleId}>{t("confirm.title")}</h2>
        <div className="row">
          <StatusPill tone={challenge.risk} label={t(`confirm.risk.${challenge.risk}`)} />
        </div>
        <p id={descId} className="muted">
          Review exactly what will happen. Sam will only proceed if its permission rules also allow it.
        </p>
        <dl>
          <dt>Action</dt>
          <dd>{humanize(challenge.action)}</dd>
          <dt>Resource</dt>
          <dd>{humanize(challenge.resource)}</dd>
          <dt>Scope</dt>
          <dd className="mono">{challenge.scope}</dd>
          {challenge.target ? (
            <>
              <dt>Target</dt>
              <dd>{challenge.target}</dd>
            </>
          ) : null}
          {challenge.reason ? (
            <>
              <dt>Why</dt>
              <dd>{challenge.reason}</dd>
            </>
          ) : null}
        </dl>
        {critical ? (
          <div style={{ marginBottom: 14 }}>
            <label htmlFor="step-up" className="muted">
              {t("confirm.stepUp")}
            </label>
            <input
              id="step-up"
              className="text-input"
              style={{ marginTop: 6 }}
              type="password"
              autoComplete="off"
              spellCheck={false}
              maxLength={256}
              value={stepUp}
              onChange={(event) => setStepUp(event.target.value)}
            />
            {error ? (
              <div role="alert" className="faint" style={{ marginTop: 6, color: "var(--danger)" }}>
                {error}
              </div>
            ) : null}
          </div>
        ) : null}
        <div className="dialog-actions">
          <button ref={denyRef} type="button" className="neon-button" data-variant="quiet" onClick={onDeny}>
            {t("confirm.deny")}
          </button>
          <NeonButton
            variant={challenge.risk === "low" || challenge.risk === "medium" ? "primary" : "danger"}
            disabled={submitted || (critical && stepUp.length === 0)}
            onClick={approve}
          >
            {t("confirm.approve")}
          </NeonButton>
        </div>
      </div>
    </div>
  );
}
