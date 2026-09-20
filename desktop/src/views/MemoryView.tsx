import { useCallback, useEffect, useState } from "react";
import { BridgeError, toBridgeError } from "../bridge/bridge";
import type { MemoryItem } from "../bridge/types";
import { EmptyState, NeonButton, Notice, SectionHeader, StatusPill } from "../components/primitives";
import { humanize } from "../lib/format";
import { useSam } from "../state";

function MemoryCard({ item }: { item: MemoryItem }) {
  return (
    <article className="card" aria-label={`Memory ${item.memory_type}`}>
      <div className="card-row">
        <StatusPill tone="info" label={humanize(item.memory_type)} />
        <span className="faint">{humanize(item.source)}</span>
      </div>
      <p style={{ margin: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{item.content}</p>
    </article>
  );
}

export function MemoryView() {
  const { bridge } = useSam();
  const [text, setText] = useState("");
  const [items, setItems] = useState<MemoryItem[] | null>(null);
  const [working, setWorking] = useState<MemoryItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (query?: string) => {
      try {
        const result = await bridge.memorySearch(query);
        setItems(result.items);
        setWorking(result.working);
        setError(result.status === "ok" ? null : (result.message ?? "Memory couldn't be read."));
      } catch (failure) {
        setItems([]);
        setError((failure instanceof BridgeError ? failure : toBridgeError(failure)).message);
      }
    },
    [bridge],
  );

  useEffect(() => {
    void load();
  }, [load]);

  const empty = items !== null && items.length === 0 && working.length === 0;
  return (
    <div className="page-narrow">
      <SectionHeader
        title="Memory"
        description="What Sam has remembered about you. This is separate from Knowledge (your documents)."
      />
      <Notice live={false}>
        Read-only here. Sam doesn't save anything from your conversations automatically, and this view can't add or
        change memories.
      </Notice>
      {error ? <Notice tone="danger">{error}</Notice> : null}
      <form
        className="row panel glass"
        role="search"
        onSubmit={(event) => {
          event.preventDefault();
          void load(text.trim() || undefined);
        }}
      >
        <label className="sr-only" htmlFor="memory-query">
          Search memory
        </label>
        <input
          id="memory-query"
          className="text-input row-grow"
          value={text}
          maxLength={1000}
          placeholder="Search memory…"
          onChange={(event) => setText(event.target.value)}
        />
        <NeonButton type="submit">Search</NeonButton>
      </form>
      {items === null ? (
        <p className="muted" role="status">
          Loading…
        </p>
      ) : empty ? (
        <EmptyState title="Nothing remembered yet" body="When Sam stores a memory it will appear here, labelled by type and source." />
      ) : (
        <>
          {working.length > 0 ? (
            <>
              <h3>This session</h3>
              <div className="grid" style={{ marginBottom: 20 }}>
                {working.map((item) => (
                  <MemoryCard key={item.memory_id} item={item} />
                ))}
              </div>
            </>
          ) : null}
          {items.length > 0 ? (
            <>
              <h3>Long-term</h3>
              <div className="grid">
                {items.map((item) => (
                  <MemoryCard key={item.memory_id} item={item} />
                ))}
              </div>
            </>
          ) : null}
        </>
      )}
    </div>
  );
}
