import { useCallback, useEffect, useState } from "react";
import { BridgeError } from "../bridge/bridge";
import type {
  ClaudeImprovementState,
  ProviderPreferences,
  ProvidersStatus,
  ProviderId,
  ProviderState,
} from "../bridge/types";
import { NeonButton, Notice, StatusPill, type Tone } from "./primitives";
import type { StringKey } from "../i18n/strings";
import { useSam } from "../state";

const STATE_TONE: Record<ProviderState, Tone> = {
  available: "ok",
  rate_limited: "warn",
  usage_limit: "warn",
  unauthorized: "warn",
  not_configured: "muted",
  disabled: "muted",
  unattested: "warn",
  unavailable: "danger",
};

const IMPROVEMENT: ClaudeImprovementState[] = ["unknown", "owner_reports_disabled", "owner_reports_enabled"];

/** Provider status and the owner's routing/privacy/content preferences.
 * Everything shown is trusted display state from the backend: no key, token,
 * prefix or provider response ever reaches this component, and nothing here
 * can enable a paid provider. */
export function ModelSettings() {
  const { bridge, t } = useSam();
  const [models, setModels] = useState<ProvidersStatus | null>(null);
  const [draft, setDraft] = useState<ProviderPreferences | null>(null);
  const [blocklist, setBlocklist] = useState("");
  const [stepUp, setStepUp] = useState("");
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const apply = useCallback((next: ProvidersStatus) => {
    setModels(next);
    if (next.preferences) setDraft(next.preferences);
    setBlocklist((next.content?.topic_blocklist ?? []).join("\n"));
  }, []);

  useEffect(() => {
    let live = true;
    bridge
      .providersStatus()
      .then((next) => live && apply(next))
      .catch(() => live && setModels({ available: false } as ProvidersStatus));
    return () => {
      live = false;
    };
  }, [bridge, apply]);

  if (models === null) return null;
  if (!models.available || !draft || !models.preferences) {
    return (
      <section className="glass panel" aria-labelledby="models-h">
        <h3 id="models-h">{t("models.title")}</h3>
        <p className="muted">{t("models.unavailable")}</p>
      </section>
    );
  }

  const current = models.preferences;
  const loosening =
    (draft.personal_to_free_tier && !current.personal_to_free_tier) ||
    (draft.private_to_free_tier && !current.private_to_free_tier) ||
    (draft.private_to_claude_when_improvement_enabled && !current.private_to_claude_when_improvement_enabled) ||
    (draft.gemini_attestation === "owner_attested_unbilled" && current.gemini_attestation !== "owner_attested_unbilled");
  const patch = (change: Partial<ProviderPreferences>) => {
    setSaved(false);
    setDraft({ ...draft, ...change });
  };
  const enabled = models.providers.filter((p) => p.enabled);

  const save = async () => {
    setError(null);
    setSaved(false);
    try {
      const next = await bridge.setProviderPreferences({
        ...draft,
        topic_blocklist: blocklist
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean),
        stepUp: loosening ? stepUp : undefined,
      });
      apply(next);
      setStepUp("");
      setSaved(true);
    } catch (failure) {
      setError(failure instanceof BridgeError ? failure.message : t("error.failed"));
    }
  };

  return (
    <>
      <section className="glass panel" aria-labelledby="models-h">
        <h3 id="models-h">{t("models.title")}</h3>
        {models.providers.map((provider) => (
          <div key={provider.provider_id} data-testid={`provider-${provider.provider_id}`} style={{ marginBottom: 14 }}>
            <div className="card-row">
              <span>{provider.display_name}</span>
              <StatusPill tone={STATE_TONE[provider.state]} label={t(`models.state.${provider.state}`)} />
            </div>
            <div className="faint">{t(`models.cost.${provider.cost_class}`)}</div>
            {provider.external && provider.enabled ? <div className="faint">{t("models.external")}</div> : null}
            <div className="faint" dir="auto">
              {provider.note}
            </div>
            {provider.detail ? (
              <div className="faint" data-testid={`detail-${provider.provider_id}`}>
                {t(`models.detail.${provider.detail}` as StringKey)}
              </div>
            ) : null}
            {provider.provider_id === "gemini_free" ? (
              <div className="faint" data-testid="gemini-attestation-state">
                {draft.gemini_attestation === "owner_attested_unbilled"
                  ? t("models.attest.attested")
                  : t("models.attest.intended")}
              </div>
            ) : null}
          </div>
        ))}
        <div className="card-row">
          <span className="muted">{t("models.routing")}</span>
          <span>{t("models.auto")}</span>
        </div>
        <div className="card-row">
          <span className="muted">{t("models.paidFallback")}</span>
          <StatusPill tone="ok" label={t("models.off")} />
        </div>
        <div className="card-row">
          <span className="muted">{t("models.maxAttempts")}</span>
          <span>{models.max_provider_attempts}</span>
        </div>
      </section>

      <section className="glass panel" aria-labelledby="privacy-h">
        <h3 id="privacy-h">{t("models.privacyTitle")}</h3>
        <div className="row" style={{ flexDirection: "column", alignItems: "flex-start", gap: 14 }}>
          <label className="row">
            <span>{t("models.preferred")}</span>
            <select
              className="field"
              style={{ width: "auto" }}
              value={draft.preferred_provider ?? ""}
              onChange={(event) => patch({ preferred_provider: (event.target.value || null) as ProviderId | null })}
            >
              <option value="">{t("models.noPreference")}</option>
              {enabled.map((provider) => (
                <option key={provider.provider_id} value={provider.provider_id}>
                  {provider.display_name}
                </option>
              ))}
            </select>
          </label>
          <label className="row">
            <input
              type="checkbox"
              checked={draft.allow_free_fallback}
              onChange={(event) => patch({ allow_free_fallback: event.target.checked })}
            />
            <span>{t("models.allowFallback")}</span>
          </label>
          <label className="row">
            <input
              type="checkbox"
              checked={draft.personal_to_free_tier}
              onChange={(event) => patch({ personal_to_free_tier: event.target.checked })}
            />
            <span>{t("models.personalToFree")}</span>
          </label>
          <label className="row">
            <input
              type="checkbox"
              checked={draft.private_to_free_tier}
              onChange={(event) => patch({ private_to_free_tier: event.target.checked })}
            />
            <span>{t("models.privateToFree")}</span>
          </label>
          <label className="row">
            <input
              type="checkbox"
              checked={draft.gemini_attestation === "owner_attested_unbilled"}
              onChange={(event) =>
                patch({ gemini_attestation: event.target.checked ? "owner_attested_unbilled" : "unknown" })
              }
            />
            <span>{t("models.attest.label")}</span>
          </label>
          <p className="faint" style={{ margin: 0 }}>
            {t("models.attest.note")}
          </p>
          <label className="row">
            <span>{t("models.claudeImprovement")}</span>
            <select
              className="field"
              style={{ width: "auto" }}
              value={draft.claude_improvement_state}
              onChange={(event) => patch({ claude_improvement_state: event.target.value as ClaudeImprovementState })}
            >
              {IMPROVEMENT.map((state) => (
                <option key={state} value={state}>
                  {t(`models.improvement.${state}`)}
                </option>
              ))}
            </select>
          </label>
          <p className="faint" style={{ margin: 0 }}>
            {t("models.improvementNote")}
          </p>
          {loosening ? (
            <label className="row">
              <span>{t("models.stepUp")}</span>
              <input
                className="field"
                type="password"
                autoComplete="off"
                value={stepUp}
                onChange={(event) => setStepUp(event.target.value)}
              />
            </label>
          ) : null}
        </div>
      </section>

      <section className="glass panel" aria-labelledby="content-h">
        <h3 id="content-h">{t("models.contentTitle")}</h3>
        <div className="card-row">
          <span className="muted">{t("models.contentMode")}</span>
          <StatusPill tone="info" label={t("models.permissive")} />
        </div>
        {(["models.profanity", "models.adult", "models.sensitive", "models.controversial"] as const).map((key) => (
          <div className="card-row" key={key}>
            <span className="muted">{t(key)}</span>
            <span>{t("models.allowed")}</span>
          </div>
        ))}
        <div className="card-row">
          <span className="muted">{t("models.mirrorTone")}</span>
          <span>{models.content?.follow_user_tone ? t("models.on") : t("models.off")}</span>
        </div>
        <label style={{ display: "block", marginTop: 12 }}>
          <span className="muted">{t("models.blocklist")}</span>
          <textarea
            className="field"
            rows={3}
            dir="auto"
            value={blocklist}
            placeholder={t("models.blocklistEmpty")}
            onChange={(event) => {
              setSaved(false);
              setBlocklist(event.target.value);
            }}
          />
        </label>
        <p className="faint">{t("models.privateMemory")}</p>
        <p className="faint" style={{ marginBottom: 0 }}>
          {t("models.providerRules")}
        </p>
      </section>

      <div className="row" style={{ marginBottom: 16 }}>
        <NeonButton onClick={() => void save()}>{t("models.save")}</NeonButton>
        {saved ? <span role="status">{t("models.saved")}</span> : null}
      </div>
      {error ? <Notice tone="danger">{error}</Notice> : null}
    </>
  );
}
