import type { KnowledgeHit } from "../bridge/types";
import { humanize } from "../lib/format";
import { StatusPill } from "./primitives";

/**
 * A retrieved passage with provenance. Every location field is shown only if
 * the backend supplied it; nothing is inferred or invented. The snippet is
 * untrusted document text and is rendered as plain text.
 */
export function SourceCard({ hit }: { hit: KnowledgeHit }) {
  const { location } = hit;
  const parts: string[] = [];
  if (location.page_number !== null) parts.push(`Page ${location.page_number}`);
  if (location.section_title) parts.push(`Section: ${location.section_title}`);
  if (location.paragraph_index !== null) parts.push(`Paragraph ${location.paragraph_index}`);
  if (location.character_start !== null && location.character_end !== null) {
    parts.push(`Characters ${location.character_start}–${location.character_end}`);
  }
  return (
    <article className="card" aria-label={`Source ${hit.resource_name}`}>
      <div className="card-row">
        <h4>{hit.resource_name}</h4>
        <StatusPill tone="info" label={humanize(hit.resource_type)} />
      </div>
      <p className="snippet">{hit.snippet}</p>
      <div className="card-row faint">
        <span>{parts.length ? parts.join(" · ") : "Location not available"}</span>
        <span>Relevance {hit.score.toFixed(2)}</span>
      </div>
    </article>
  );
}
