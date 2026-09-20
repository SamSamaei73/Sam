import { useCallback, useEffect, useRef, useState } from "react";
import { BridgeError, toBridgeError } from "../bridge/bridge";
import type { KnowledgeHit, OperationResult, ResourceInfo } from "../bridge/types";
import { IconTrash, IconUpload } from "../components/Icons";
import { EmptyState, IconButton, NeonButton, Notice, SectionHeader, StatusPill } from "../components/primitives";
import { SourceCard } from "../components/SourceCard";
import { MAX_UPLOAD_BYTES, formatBytes, humanize, resourceKindFor } from "../lib/format";
import { bytesToBase64 } from "../lib/wav";
import { useSam } from "../state";

function messageFor(error: unknown): string {
  return (error instanceof BridgeError ? error : toBridgeError(error)).message;
}

export function KnowledgeView() {
  const { bridge, confirmable } = useSam();
  const [resources, setResources] = useState<ResourceInfo[] | null>(null);
  const [hits, setHits] = useState<KnowledgeHit[] | null>(null);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone?: "danger" | "warn"; text: string } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const report = (result: OperationResult, success?: string) => {
    if (result.status === "ok") setNotice(success ? { text: success } : null);
    else setNotice({ tone: result.status === "denied" ? "warn" : "danger", text: result.message ?? "That couldn't be completed." });
  };

  const load = useCallback(async () => {
    try {
      const result = await bridge.knowledgeList();
      if (result.status === "ok") setResources(result.resources);
      else {
        setResources([]);
        setNotice({ tone: "warn", text: result.message ?? "Knowledge couldn't be listed." });
      }
    } catch (error) {
      setResources([]);
      setNotice({ tone: "danger", text: messageFor(error) });
    }
  }, [bridge]);

  useEffect(() => {
    void load();
  }, [load]);

  const search = async () => {
    const text = query.trim();
    if (!text) return;
    setBusy(true);
    setNotice(null);
    try {
      const result = await bridge.knowledgeQuery(text);
      setHits(result.status === "ok" ? result.hits : []);
      if (result.status !== "ok") report(result);
    } catch (error) {
      setNotice({ tone: "danger", text: messageFor(error) });
    } finally {
      setBusy(false);
    }
  };

  const upload = async (file: File | undefined) => {
    if (!file) return;
    const kind = resourceKindFor(file.name);
    if (!kind) {
      setNotice({ tone: "danger", text: "That file type isn't supported. Use PDF, TXT, Markdown, JSON or CSV." });
      return;
    }
    if (file.size === 0 || file.size > MAX_UPLOAD_BYTES) {
      setNotice({ tone: "danger", text: file.size === 0 ? "That file is empty." : "That file is too large (20 MB maximum)." });
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      const contentBase64 = bytesToBase64(bytes);
      const result = await confirmable((confirmationId) =>
        bridge.knowledgeIngest({ name: file.name, resourceType: kind, contentBase64, confirmationId }),
      );
      report(result, `Added “${file.name}” to Knowledge.`);
      await load();
    } catch (error) {
      setNotice({ tone: "danger", text: messageFor(error) });
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const remove = async (resource: ResourceInfo) => {
    setBusy(true);
    setNotice(null);
    try {
      const result = await confirmable((confirmationId) => bridge.knowledgeRemove(resource.resource_id, confirmationId));
      report(result, `Removed “${resource.name}”.`);
      setHits(null);
      await load();
    } catch (error) {
      setNotice({ tone: "danger", text: messageFor(error) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page-narrow">
      <SectionHeader
        title="Knowledge"
        description="Documents Sam can search and cite. Kept in memory for this session only."
        actions={
          <>
            <input
              ref={fileRef}
              type="file"
              className="sr-only"
              tabIndex={-1}
              aria-label="Choose a document"
              accept=".pdf,.txt,.md,.markdown,.json,.csv"
              onChange={(event) => void upload(event.target.files?.[0])}
            />
            <NeonButton disabled={busy} onClick={() => fileRef.current?.click()}>
              <IconUpload /> Add document
            </NeonButton>
          </>
        }
      />
      {notice ? <Notice tone={notice.tone}>{notice.text}</Notice> : null}

      <form
        className="row panel glass"
        role="search"
        onSubmit={(event) => {
          event.preventDefault();
          void search();
        }}
      >
        <label className="sr-only" htmlFor="knowledge-query">
          Search Knowledge
        </label>
        <input
          id="knowledge-query"
          className="text-input row-grow"
          value={query}
          maxLength={1000}
          placeholder="Search your documents…"
          onChange={(event) => setQuery(event.target.value)}
        />
        <NeonButton type="submit" disabled={busy || query.trim() === ""}>
          Search
        </NeonButton>
      </form>

      {hits !== null ? (
        <section aria-label="Search results" style={{ marginBottom: 22 }}>
          <h3 style={{ margin: "0 0 10px" }}>Results</h3>
          {hits.length === 0 ? (
            <EmptyState title="No matching passages" body="Nothing in your documents matched that search." />
          ) : (
            <div className="grid">
              {hits.map((hit) => (
                <SourceCard key={hit.chunk_id} hit={hit} />
              ))}
            </div>
          )}
        </section>
      ) : null}

      <h3 style={{ margin: "0 0 10px" }}>Documents</h3>
      {resources === null ? (
        <p className="muted" role="status">
          Loading…
        </p>
      ) : resources.length === 0 ? (
        <EmptyState
          title="No documents yet"
          body="Add a PDF, text, Markdown, JSON or CSV file. Sam will only read what you add here."
        />
      ) : (
        <div className="grid">
          {resources.map((resource) => (
            <article className="card" key={resource.resource_id} aria-label={`Document ${resource.name}`}>
              <div className="card-row">
                <h4>{resource.name}</h4>
                <StatusPill tone="info" label={humanize(resource.resource_type)} />
              </div>
              <div className="card-row faint">
                <span>
                  {formatBytes(resource.size_bytes)} · {resource.chunk_count} passages
                </span>
                <IconButton label={`Remove ${resource.name}`} disabled={busy} onClick={() => void remove(resource)}>
                  <IconTrash />
                </IconButton>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
