import { useCallback, useEffect, useState } from "react";
import { BridgeError, toBridgeError } from "../bridge/bridge";
import type { GrantInfo } from "../bridge/types";
import { EmptyState, Notice, SectionHeader } from "../components/primitives";
import { PermissionCard } from "../components/PermissionCard";
import { useSam } from "../state";

export function PermissionsView() {
  const { bridge } = useSam();
  const [grants, setGrants] = useState<GrantInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setGrants((await bridge.permissions()).grants);
    } catch (failure) {
      setError((failure instanceof BridgeError ? failure : toBridgeError(failure)).message);
    }
  }, [bridge]);

  useEffect(() => {
    void load();
  }, [load]);

  const revoke = async (grantId: string) => {
    setBusy(true);
    setError(null);
    try {
      const result = await bridge.revokeGrant(grantId);
      if (result.status !== "ok") setError(result.message ?? "That couldn't be revoked.");
      await load();
    } catch (failure) {
      setError((failure instanceof BridgeError ? failure : toBridgeError(failure)).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page-narrow">
      <SectionHeader title="Permissions" description="What Sam is currently allowed to do for you." />
      <Notice live={false}>
        You can review and revoke permissions here. Sam decides every action itself using these rules — this window
        can't grant, widen or bypass them.
      </Notice>
      {error ? <Notice tone="danger">{error}</Notice> : null}
      {grants === null && !error ? (
        <p className="muted" role="status">
          Loading…
        </p>
      ) : null}
      {grants && grants.length === 0 ? (
        <EmptyState title="No active permissions" body="Sam isn't currently allowed to do anything on your behalf." />
      ) : null}
      {grants && grants.length > 0 ? (
        <div className="grid">
          {grants.map((grant) => (
            <PermissionCard key={grant.grant_id} grant={grant} busy={busy} onRevoke={(id) => void revoke(id)} />
          ))}
        </div>
      ) : null}
    </div>
  );
}
