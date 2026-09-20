import { useCallback, useEffect, useState } from "react";
import { BridgeError, toBridgeError } from "../bridge/bridge";
import type { ActivityItem } from "../bridge/types";
import { EmptyState, NeonButton, Notice, SectionHeader, StatusPill, type Tone } from "../components/primitives";
import { formatTime, humanize } from "../lib/format";
import { useSam } from "../state";

const RISK_TONES = new Set(["low", "medium", "high", "critical"]);

function outcomeTone(outcome: string): Tone {
  if (/denied|failed|rejected/.test(outcome)) return "danger";
  if (/confirm/.test(outcome)) return "warn";
  return "ok";
}

export function ActivityView() {
  const { bridge } = useSam();
  const [items, setItems] = useState<ActivityItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setItems((await bridge.activity()).items);
      setError(null);
    } catch (failure) {
      setError((failure instanceof BridgeError ? failure : toBridgeError(failure)).message);
    }
  }, [bridge]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="page-narrow">
      <SectionHeader
        title="Activity"
        description="What Sam did during this session."
        actions={<NeonButton variant="quiet" onClick={() => void load()}>Refresh</NeonButton>}
      />
      <Notice live={false}>
        Shows this session only. It records what kind of action happened and how it was decided — never message text,
        documents or credentials.
      </Notice>
      {error ? <Notice tone="danger">{error}</Notice> : null}
      {items === null && !error ? (
        <p className="muted" role="status">
          Loading…
        </p>
      ) : null}
      {items && items.length === 0 ? (
        <EmptyState title="No activity yet" body="Actions Sam takes will be listed here as they happen." />
      ) : null}
      {items && items.length > 0 ? (
        <div className="glass panel">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Event</th>
                <th scope="col">Outcome</th>
                <th scope="col">Risk</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => (
                <tr key={`${item.timestamp}-${index}`}>
                  <td className="mono">{formatTime(item.timestamp)}</td>
                  <td>{item.label}</td>
                  <td>
                    <StatusPill tone={outcomeTone(item.outcome)} label={humanize(item.outcome)} />
                  </td>
                  <td>
                    {item.risk && RISK_TONES.has(item.risk) ? (
                      <StatusPill tone={item.risk as Tone} label={humanize(item.risk)} />
                    ) : (
                      <span className="faint">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
