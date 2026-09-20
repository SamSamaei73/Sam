import type { GrantInfo } from "../bridge/types";
import { humanize } from "../lib/format";
import { NeonButton, StatusPill } from "./primitives";

/** View + revoke only. There is no way to create or widen a grant here. */
export function PermissionCard({
  grant,
  busy,
  onRevoke,
}: {
  grant: GrantInfo;
  busy: boolean;
  onRevoke: (grantId: string) => void;
}) {
  const active = grant.status === "active";
  return (
    <article className="card" aria-label={`Permission ${grant.resource} ${grant.action}`}>
      <div className="card-row">
        <h4>
          {humanize(grant.resource)} · {humanize(grant.action)}
        </h4>
        <StatusPill tone={active ? "ok" : "muted"} label={active ? "Active" : "Revoked"} />
      </div>
      <div className="mono muted">{grant.scope}</div>
      <div className="card-row faint">
        <span>{grant.requires_confirmation ? "Asks you to confirm" : "No confirmation needed"}</span>
        <span>{grant.expires_at ? `Expires ${new Date(grant.expires_at).toLocaleString()}` : "No expiry"}</span>
      </div>
      {active ? (
        <div>
          <NeonButton variant="danger" disabled={busy} onClick={() => onRevoke(grant.grant_id)}>
            Revoke
          </NeonButton>
        </div>
      ) : null}
    </article>
  );
}
