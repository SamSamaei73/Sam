import { useEffect, useState } from "react";
import { BridgeError, toBridgeError } from "../bridge/bridge";
import type { ToolsResponse } from "../bridge/types";
import { EmptyState, Notice, SectionHeader, StatusPill } from "../components/primitives";
import { humanize } from "../lib/format";
import { useSam } from "../state";

export function ToolsView() {
  const { bridge } = useSam();
  const [data, setData] = useState<ToolsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    bridge
      .tools()
      .then((result) => live && setData(result))
      .catch((failure) => live && setError((failure instanceof BridgeError ? failure : toBridgeError(failure)).message));
    return () => {
      live = false;
    };
  }, [bridge]);

  return (
    <div className="page-narrow">
      <SectionHeader title="Tools" description="Connected tools and integrations. A read-only view." />
      <Notice live={false}>
        Tools are registered by trusted configuration, not from here. Each tool still needs a permission grant and
        may ask you to confirm before it runs.
      </Notice>
      {error ? <Notice tone="danger">{error}</Notice> : null}
      {data === null && !error ? (
        <p className="muted" role="status">
          Loading…
        </p>
      ) : null}
      {data && data.tools.length === 0 ? (
        <EmptyState
          title="No tools connected"
          body="The tool framework is ready, but no integrations are configured yet."
          action={<StatusPill tone="info" label="Foundation ready" />}
        />
      ) : null}
      {data && data.tools.length > 0 ? (
        <div className="grid">
          {data.tools.map((tool) => (
            <article className="card" key={tool.tool_id} aria-label={`Tool ${tool.tool_id}`}>
              <div className="card-row">
                <h4>{tool.tool_id}</h4>
                <StatusPill tone={tool.enabled ? "ok" : "muted"} label={tool.enabled ? "Enabled" : "Disabled"} />
              </div>
              <p className="muted" style={{ margin: 0, overflowWrap: "anywhere" }}>
                {tool.description}
              </p>
              <div className="card-row faint">
                <span>
                  Needs: {humanize(tool.permission_resource)} · {humanize(tool.permission_action)}
                </span>
                <span>{tool.credential_configured ? "Credential configured" : "No credential"}</span>
              </div>
            </article>
          ))}
        </div>
      ) : null}
    </div>
  );
}
