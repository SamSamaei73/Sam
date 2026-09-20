import { useEffect, useRef, useState } from "react";
import { BridgeError, toBridgeError } from "../bridge/bridge";
import type { ChallengeResponse } from "../bridge/types";
import { useSam } from "../state";
import { Modal } from "./Modal";
import { NeonButton, Notice } from "./primitives";
import { VoiceRecorder } from "./VoiceRecorder";
import type { StringKey } from "../i18n/strings";

/** A translated, safe message for a bridge failure (never a raw error). */
function useErrorText(): (error: unknown) => string {
  const { t } = useSam();
  return (error) => {
    const known = error instanceof BridgeError ? error : toBridgeError(error);
    const key = `error.${known.code}` as StringKey;
    try {
      return t(key) || known.message;
    } catch {
      return known.message;
    }
  };
}

function StepUpField({
  value,
  onChange,
  label,
  hint,
}: {
  value: string;
  onChange: (value: string) => void;
  label: string;
  hint?: string;
}) {
  return (
    <div style={{ marginBottom: 14 }}>
      <label htmlFor="identity-step-up" className="muted">
        {label}
      </label>
      <input
        id="identity-step-up"
        className="text-input"
        style={{ marginTop: 6 }}
        type="password"
        autoComplete="off"
        spellCheck={false}
        maxLength={256}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      {hint ? <div className="faint" style={{ marginTop: 4 }}>{hint}</div> : null}
    </div>
  );
}

/**
 * Owner enrollment. The step-up secret is typed by the user, sent once to the
 * backend (which verifies it), and cleared from state immediately after. No
 * recording is stored: each sample is embedded by the backend and dropped.
 */
export function EnrollmentDialog({
  reEnroll,
  onClose,
  onDone,
}: {
  reEnroll: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const { bridge, t } = useSam();
  const errorText = useErrorText();
  const [stepUp, setStepUp] = useState("");
  const [session, setSession] = useState<string | null>(null);
  const [count, setCount] = useState(0);
  const [needed, setNeeded] = useState(3);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [finished, setFinished] = useState(false);
  const sessionRef = useRef<string | null>(null);
  useEffect(() => {
    sessionRef.current = session;
  }, [session]);

  // Leaving mid-enrollment cancels the backend session (its samples are dropped).
  useEffect(() => {
    return () => {
      if (sessionRef.current) void bridge.identityEnrollCancel(sessionRef.current).catch(() => undefined);
    };
  }, [bridge]);

  const begin = async () => {
    const secret = stepUp;
    setStepUp(""); // never retained after submit
    setBusy(true);
    setMessage(null);
    try {
      const result = await bridge.identityEnrollBegin({ stepUp: secret, reEnroll });
      if (result.status === "ok" && result.session_id) {
        setSession(result.session_id);
        setNeeded(result.samples_needed);
      } else {
        setMessage(result.message ?? t("error.failed"));
      }
    } catch (error) {
      setMessage(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const sample = async (audio: string) => {
    if (!session) return;
    setBusy(true);
    setMessage(null);
    try {
      const result = await bridge.identityEnrollSample(session, audio);
      setCount(result.sample_count);
      setNeeded(result.samples_needed);
      if (!result.accepted) setMessage(result.message ?? t("error.failed"));
    } catch (error) {
      setMessage(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const complete = async () => {
    if (!session) return;
    setBusy(true);
    try {
      const result = await bridge.identityEnrollComplete(session);
      if (result.status === "ok") {
        setSession(null);
        setFinished(true);
        onDone();
      } else {
        setMessage(result.message ?? t("error.failed"));
      }
    } catch (error) {
      setMessage(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={reEnroll ? t("enroll.reTitle") : t("enroll.title")} onClose={onClose}>
      {finished ? (
        <>
          <Notice>{t("enroll.done")}</Notice>
          <div className="dialog-actions">
            <NeonButton onClick={onClose}>{t("common.close")}</NeonButton>
          </div>
        </>
      ) : !session ? (
        <>
          <p className="muted">{t("enroll.intro")}</p>
          <StepUpField
            value={stepUp}
            onChange={setStepUp}
            label={t("enroll.stepUp")}
            hint={t("enroll.stepUpHint")}
          />
          {message ? <Notice tone="danger">{message}</Notice> : null}
          <div className="dialog-actions">
            <NeonButton variant="quiet" onClick={onClose}>
              {t("common.cancel")}
            </NeonButton>
            <NeonButton disabled={busy || stepUp.length === 0} onClick={() => void begin()}>
              {t("enroll.begin")}
            </NeonButton>
          </div>
        </>
      ) : (
        <>
          <ul className="step-list">
            <li lang="fa" dir="rtl">{t("enroll.promptFa")}</li>
            <li lang="en" dir="ltr">{t("enroll.promptEn")}</li>
          </ul>
          <div className="card-row" style={{ marginBottom: 12 }}>
            <span>
              {t("enroll.progress")}: {count} / {needed}
            </span>
            <VoiceRecorder
              available
              busy={busy}
              onRecorded={(audio) => void sample(audio)}
              onError={setMessage}
            />
          </div>
          {message ? <Notice tone="warn">{message}</Notice> : null}
          <div className="dialog-actions">
            <NeonButton variant="quiet" onClick={onClose}>
              {t("common.cancel")}
            </NeonButton>
            <NeonButton disabled={busy || count < needed} onClick={() => void complete()}>
              {t("enroll.complete")}
            </NeonButton>
          </div>
        </>
      )}
    </Modal>
  );
}

/** Remove the owner voice profile (needs the step-up secret). */
export function RemoveProfileDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const { bridge, t } = useSam();
  const errorText = useErrorText();
  const [stepUp, setStepUp] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const remove = async () => {
    const secret = stepUp;
    setStepUp("");
    setBusy(true);
    setMessage(null);
    try {
      const result = await bridge.identityDelete(secret);
      if (result.status === "ok") {
        onDone();
        onClose();
      } else {
        setMessage(result.message ?? t("error.failed"));
      }
    } catch (error) {
      setMessage(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={t("enroll.removeTitle")} onClose={onClose}>
      <p className="muted">{t("enroll.removeBody")}</p>
      <StepUpField value={stepUp} onChange={setStepUp} label={t("enroll.stepUp")} />
      {message ? <Notice tone="danger">{message}</Notice> : null}
      <div className="dialog-actions">
        <NeonButton variant="quiet" onClick={onClose}>
          {t("common.cancel")}
        </NeonButton>
        <NeonButton variant="danger" disabled={busy || stepUp.length === 0} onClick={() => void remove()}>
          {t("enroll.removeConfirm")}
        </NeonButton>
      </div>
    </Modal>
  );
}

/**
 * Start Guest Mode: the OWNER speaks a fresh, random phrase (speaker match +
 * challenge content are both checked by the backend) and enters the step-up
 * secret. Nothing here names a principal or grants a capability.
 */
export function GuestDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const { bridge, t, prefs } = useSam();
  const errorText = useErrorText();
  const [minutes, setMinutes] = useState(15);
  const [stepUp, setStepUp] = useState("");
  const [challenge, setChallenge] = useState<ChallengeResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const getChallenge = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await bridge.guestChallenge();
      if (result.status === "ok" && result.challenge_id) setChallenge(result);
      else setMessage(result.message ?? t("error.failed"));
    } catch (error) {
      setMessage(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const start = async (audio: string) => {
    if (!challenge?.challenge_id) return;
    const secret = stepUp;
    setStepUp("");
    setBusy(true);
    setMessage(null);
    try {
      const result = await bridge.guestStart({
        challengeId: challenge.challenge_id,
        audioBase64: audio,
        stepUp: secret,
        minutes,
      });
      if (result.status === "ok") {
        onDone();
        onClose();
      } else {
        setChallenge(null); // a challenge is one-time: a new phrase is needed
        setMessage(result.message ?? t("error.failed"));
      }
    } catch (error) {
      setChallenge(null);
      setMessage(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const phrase = prefs.uiLanguage === "fa" ? challenge?.text_fa : challenge?.text_en;
  return (
    <Modal title={t("guest.title")} onClose={onClose} tone="warn">
      <p className="muted">{t("guest.intro")}</p>
      {!challenge ? (
        <>
          <label className="row" style={{ marginBottom: 12 }}>
            <span>{t("guest.minutes")}</span>
            <select
              className="field"
              style={{ width: "auto" }}
              value={minutes}
              onChange={(event) => setMinutes(Number(event.target.value))}
            >
              {[5, 15, 30].map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
          <StepUpField value={stepUp} onChange={setStepUp} label={t("enroll.stepUp")} />
          {message ? <Notice tone="danger">{message}</Notice> : null}
          <div className="dialog-actions">
            <NeonButton variant="quiet" onClick={onClose}>
              {t("common.cancel")}
            </NeonButton>
            <NeonButton disabled={busy || stepUp.length === 0} onClick={() => void getChallenge()}>
              {t("guest.getChallenge")}
            </NeonButton>
          </div>
        </>
      ) : (
        <>
          <p>{t("guest.saySlip")}</p>
          <div className="challenge-box" dir="auto" data-testid="challenge">
            {phrase}
          </div>
          <div className="row" style={{ justifyContent: "center", marginBottom: 12 }}>
            <VoiceRecorder available busy={busy} onRecorded={(a) => void start(a)} onError={setMessage} />
          </div>
          {message ? <Notice tone="warn">{message}</Notice> : null}
          <div className="dialog-actions">
            <NeonButton variant="quiet" onClick={onClose}>
              {t("common.cancel")}
            </NeonButton>
          </div>
        </>
      )}
    </Modal>
  );
}
