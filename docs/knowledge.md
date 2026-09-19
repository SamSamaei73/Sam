# Sam Knowledge Layer (Phase 7)

> **Phase 7 is a provider-independent Knowledge Layer, not a vector
> database, not Memory, and not a wired-in Claude tool.**

## Purpose

The Knowledge Layer lets Sam ingest user-provided reference material —
PDFs, books, research papers, documentation, Markdown, TXT, JSON, CSV —
preserve exactly where each piece of extracted text came from, and
retrieve it later with full source attribution. It is deliberately
separate from `sam.memory`: a document is external reference material,
not a personal fact about the user, and ingesting one never creates a
Memory entry.

```
caller
   │
   ▼
KnowledgeOperationRequest   (one of five closed, typed operations)
   │
   ▼
sam.knowledge.policy.build_request()   (pure mapping — no decision)
   │
   ▼
sam.permissions.engine.PermissionEngine.evaluate()   (the only authority)
   │
   ├─ DENY ─────────────────────────► stop, store/index/parser never called
   ├─ CONFIRM_REQUIRED ───────────────► stop, store/index/parser never called
   └─ ALLOW
        │
        ▼
   KnowledgeEngine dispatch
        │
        ├─ ingest → sam.knowledge.ingestion (validate → checksum → parse
        │           → validate content → chunk) → KnowledgeStore →
        │           KnowledgeIndex
        └─ retrieve → sam.knowledge.retrieval → KnowledgeIndex → join
                       against KnowledgeStore → RetrievalResult
```

## Package layout

```
src/sam/knowledge/
    models.py        domain models: resource, metadata, parsed
                      document/segment, chunk, chunk location,
                      retrieval query/result, per-method result
                      envelopes, the closed KnowledgeOperation enum,
                      the five request models, the audit event
    errors.py         typed domain errors, never leaked to a caller
                      as raw exception text
    policy.py          KnowledgeOperation → PermissionRequest mapping,
                        risk lookup, collection-scope helpers
    sources.py          declared-vs-actual resource type validation
    parser.py            DocumentParser Protocol + TextParser /
                        MarkdownParser / JSONParser / CSVParser /
                        PDFParser
    chunker.py            deterministic, bounded chunking
    metadata.py            secret protection (filename / content /
                          metadata) — reuses one pure function from
                          sam.memory.sanitization, nothing else
    store.py                KnowledgeStore Protocol +
                          InMemoryKnowledgeStore
    index.py                  KnowledgeIndex Protocol +
                          InMemoryLexicalIndex (deterministic term
                          overlap — no vector database)
    embeddings.py              EmbeddingProvider Protocol +
                          FakeEmbeddingProvider — an unwired future
                          boundary
    retrieval.py                KnowledgeRetriever: joins index hits
                          back against the store for full source
                          attribution
    ingestion.py                the pure validate → checksum → parse →
                          validate-content → chunk pipeline
    engine.py                    KnowledgeEngine: the sole
                          orchestration entry point
    audit.py                      KnowledgeAuditSink Protocol +
                          InMemoryKnowledgeAuditSink
```

## Memory vs. Knowledge

| | Memory (`sam.memory`) | Knowledge (`sam.knowledge`) |
|---|---|---|
| What it holds | Personal/contextual facts about the user or Sam's own state | External or user-provided reference material |
| Example | "User prefers Python." | A PDF textbook chapter |
| Who writes it | `MemoryEngine`, via an explicit, policy-gated `MemoryCandidate` | `KnowledgeEngine.ingest`, via an explicit caller-initiated request |
| Storage | `sam.memory.store` | `sam.knowledge.store` — its own, independent abstraction |
| Cross-import | — | Imports exactly one pure function, `sam.memory.sanitization.looks_like_secret`, for secret detection. Nothing else. Never imports `sam.memory.engine`. |

Ingesting a document **never** writes to Memory, automatically or
otherwise. `sam.knowledge.engine` has no code path that constructs a
`MemoryCandidate` or calls anything in `sam.memory.engine` — see
`tests/test_knowledge_engine.py::TestMemoryIsolation` and
`tests/test_knowledge_security.py::test_23_...` for the tests that
enforce this at both the behavioral and structural (source-scan) level.
A future explicit workflow ("Remember this fact from the PDF I just
gave you") may bridge the two — that is a distinct, separately-designed
action, not part of ingestion.

## Supported formats

| Format | Parser | Source-location fields it can supply |
|---|---|---|
| TXT | `TextParser` | `character_start` / `character_end` only |
| Markdown | `MarkdownParser` | `section_title` (split on ATX `#`..`######` headings) + character offsets within the section |
| JSON | `JSONParser` | `paragraph_index` (array element index, or `0` for an object/scalar) + character offsets |
| CSV | `CSVParser` | `paragraph_index` (data row index) + character offsets |
| PDF | `PDFParser` | `page_number` + character offsets within the page |

A field is only ever populated when the format genuinely provides that
information. No parser invents a page number, a section title, or a
paragraph index — see "Source and citation tracking" below.

### PDF: what `PDFParser` actually does

No PDF library is installed, and none was added for this phase. The
task's own guidance is explicit: *"If a format cannot safely be parsed
with currently installed dependencies, implement the abstraction and a
safe unsupported-format response rather than adding a large dependency
unnecessarily."* Rather than stop at a placeholder, `PDFParser` is a
real, bounded, standard-library-only implementation:

1. Validates the `%PDF-` signature.
2. Parses PDF objects with a regex-based `N 0 obj ... endobj` scan.
3. Resolves page order by walking `/Root` → `/Pages` → `/Kids`
   (falling back to ascending object-number order for a PDF whose
   catalog cannot be resolved — a best-effort approximation, not a
   guarantee of true reading order for every producer).
4. Decompresses `FlateDecode` content streams with `zlib`, under a hard
   output-size bound (`_safe_inflate`) — this is the specific defense
   against a decompression-bomb PDF (a small file that decompresses to
   gigabytes).
5. Extracts literal text from `Tj`/`TJ` text-showing operators, with PDF
   string-escape decoding (`\(`, `\)`, `\\`, octal escapes).

**What it does not do**: encryption, CID/Type0 font decoding
(glyph-to-Unicode mapping for many non-Latin/embedded-subset fonts), or
any layout reconstruction. Text from a PDF using those features may come
out incomplete, garbled, or out of order rather than perfectly faithful
— this is a best-effort extractor, not a full PDF renderer, and this
limitation is deliberate and documented rather than hidden.

**Scanned / image-only PDFs**: a page with no `Tj`/`TJ` operators at all
contributes no segment. If a whole document produces zero non-blank
segments, ingestion is **rejected** (`EXTRACTION_ERROR`) — it is never
silently marked as successfully learned with zero chunks. No OCR is
implemented.

## Ingestion lifecycle

```
IngestResourceRequest
   │
   ▼
KnowledgeEngine.ingest()
   │
   ├─ 1. build_request() + PermissionEngine.evaluate()
   │      DENY / CONFIRM_REQUIRED → stop here, nothing persisted
   │
   ├─ 2. (only on ALLOW) checksum the content, check for an existing
   │      resource with the same checksum in this collection
   │      → match found: return DUPLICATE, nothing new persisted
   │
   ├─ 3. run_ingestion_pipeline() — pure, no store/index touched yet:
   │      validate declared-vs-actual type
   │        → reject a restricted filename or restricted metadata
   │        → select and run the matching parser
   │        → reject if no extractable text
   │        → reject if extracted text exceeds the size bound
   │        → reject if any segment, or the segments taken together
   │          across their boundaries, looks like a secret
   │        → chunk deterministically (bounded chunk count/size)
   │      → any rejection here: nothing persisted, typed error category returned
   │
   └─ 4. (only if the pipeline accepted) persist:
          store.save_resource() → store.save_chunks() → index.index_chunks()
          Any failure here triggers a compensating cleanup — see
          "Failure handling and rollback" below.
```

### Failure handling and rollback

Persistence is three separate provider calls, so it is **not atomic**.
What the engine provides is a *compensating rollback*:

1. Any exception from `save_resource`, `save_chunks`, or `index_chunks`
   is caught. The primary failure category is `STORAGE_ERROR` (store
   stage) or `INDEXING_ERROR` (index stage) and is always the one
   reported.
2. `KnowledgeEngine._compensate` then cleans up **both** providers, index
   first (`index.remove_resource`) so the resource stops being
   searchable before its store record goes (`store.delete_resource`).
   Each step is guarded independently — one failing never skips the
   other — and the store step is verified with a read-back. It uses only
   the `KnowledgeStore` / `KnowledgeIndex` protocols; no concrete
   provider is referenced.
3. If cleanup fully succeeds, the result is `FAILED` with
   `rollback_incomplete=False`.
4. If cleanup itself fails, the result is still `FAILED` with the
   original category and `rollback_incomplete=True` (also recorded on
   the content-free audit event). The engine does **not** claim the
   rollback worked. The resource id is added to a per-engine-instance
   quarantine set that `get_resource`, `list_resources`, and `retrieve`
   all respect, so remnants left in the store or index are never
   returned to a caller.
5. A retry with the same content first re-attempts cleanup of the
   quarantined remnant. Only if that succeeds does the new attempt
   proceed; otherwise it fails with `rollback_incomplete=True` rather
   than stacking a second copy or reporting the remnant as a
   `DUPLICATE`. `remove_resource` on a quarantined id is the explicit
   manual cleanup path and clears the quarantine on success.

The quarantine lives in engine instance memory, so it does not survive a
process restart; a persistent store would need to persist an ingestion
state marker (see technical debt).

`IngestionResult.status` is one of `SUCCESS` / `DUPLICATE` / `REJECTED`
/ `FAILED`. A structural Pydantic validator enforces that `SUCCESS`
always carries a persisted resource and at least one chunk, and every
other status carries neither — see `IngestionResult._validate_shape` in
`sam.knowledge.models`.

## Chunking

`sam.knowledge.chunker.chunk_document` is pure and deterministic: given
the same `ParsedDocument` and `ChunkerConfig`, it always produces
byte-identical chunk text, offsets, and ids. Rules:

- A chunk never spans more than one `ParsedSegment` — a PDF chunk never
  crosses a page boundary, a Markdown chunk never crosses a section
  boundary — so a segment's provenance always applies to the chunk as a
  whole.
- `overlap_characters` must be strictly less than `max_chunk_characters`
  (otherwise a window could never advance); `overlap_characters=0` is a
  valid, explicit "no overlap" configuration.
- A window whose text is entirely whitespace is skipped — chunks are
  never empty.
- `chunk_id` is deterministic: `f"{resource_id}:{sequence_index:06d}"`,
  never a random UUID.
- If chunking a document would exceed `MAX_CHUNKS_PER_RESOURCE`, the
  whole ingestion is rejected (`ChunkingError`) rather than silently
  truncated.

## Source and citation tracking

Every `DocumentChunk` carries a `ChunkLocation` with whatever provenance
its source format actually supplies:

```
DocumentChunk(
    resource_id="paper-123",
    location=ChunkLocation(page_number=7, section_title=None, ...),
    text="...",
)
```

A `RetrievalResult` always carries the full chunk, its score, the
complete `KnowledgeResource` (name, source, resource type), and the
chunk's `ChunkLocation` — never bare text. A citation is assembled from
exactly what is present:

```
Deep Learning.pdf
Page 214
```

If `section_title` is `None` (plain TXT, or Markdown content before the
first heading), it is simply omitted — never fabricated as e.g.
`"Section 1"`. No code path in this package invents a page number, a
section title, or a paragraph index that the parser did not actually
observe.

## Retrieval

`InMemoryLexicalIndex` is a deterministic term-frequency index: for each
distinct query token, it sums how many times that token occurs in a
chunk, and ranks by that score. Ties break deterministically by
`(resource_id, sequence_index)` ascending — repeated identical queries
against identical data always return identical ordering (see
`tests/test_knowledge_index.py::test_repeated_search_is_stable`).

Retrieval supports both a single-collection query
(`RetrievalQuery.collection_id="research"`) and a cross-collection query
(`collection_id=None`, "search everywhere"). The two are **not** the
same authorization: a grant scoped to one collection's `retrieve`
segment never satisfies a global, all-collections search — that
requires its own, separately granted, broader authorization scoped to
the reserved `GLOBAL_COLLECTION_ID` (`"__all__"`) segment. See
`sam.knowledge.policy`'s module docstring.

No embedding-based (semantic) retrieval exists in this phase — see
"Embeddings" below.

## Embeddings — the unwired future boundary

`sam.knowledge.embeddings.EmbeddingProvider` is a one-method `Protocol`
(`embed(texts) -> Sequence[Embedding]`). `FakeEmbeddingProvider` is a
deterministic, content-derived (character-hash-bucket) vector generator
that exists solely to prove the architectural boundary compiles and is
testable — its vectors carry **no real semantic meaning** and are never
presented as if they did. No code in `sam.knowledge.retrieval` or
`sam.knowledge.engine` currently constructs or calls an
`EmbeddingProvider` at all. No external embedding API (OpenAI,
Anthropic, Voyage, Cohere, ...) is called anywhere in this package.

## Permission Engine integration

One new resource, `PermissionResource.KNOWLEDGE`, was added to
`sam.permissions.models` (a one-line, additive change), with four rows
added to `sam.permissions.policy._POLICY`:

| Action | Risk | Confirmation |
|---|---|---|
| `READ` | LOW | no |
| `WRITE` | MEDIUM | no |
| `DELETE` | HIGH | **yes, always** |
| `EXECUTE` | HIGH | yes (reserved — see below) |

`KnowledgeOperation` (the closed set of five operations, one per
`KnowledgeEngine` public method) maps onto these:

| `KnowledgeOperation` | Action | Scope |
|---|---|---|
| `INGEST_RESOURCE` | WRITE | `{collection_id}:ingest` |
| `GET_RESOURCE` | READ | `{collection_id}/{resource_id}` |
| `LIST_RESOURCES` | READ | `{collection_id}:list` |
| `RETRIEVE` | READ | `{collection_id}:retrieve`, or `__all__:retrieve` for a cross-collection query |
| `REMOVE_RESOURCE` | DELETE | `{collection_id}/{resource_id}` |

`EXECUTE` has no `KnowledgeOperation` mapped onto it in Phase 7 — it is
added pre-emptively for a future bulk/administrative operation (for
example a full reindex), the same way several existing rows in
`sam.permissions.policy` (e.g. `BROWSER`, `MCP`) exist ahead of any
caller. `KnowledgeEngine._gate` (the shared DENY/CONFIRM_REQUIRED
handler for `get_resource`/`list_resources`/`retrieve`/`remove_resource`,
plus `ingest`'s equivalent inline logic) is the only place a decision is
interpreted; `PermissionEngine.evaluate` remains the only place a
decision is *made*. No second permission system exists anywhere in this
package.

## Security boundaries

- **Untrusted input.** Every uploaded resource is treated as untrusted:
  bounded size (`MAX_RESOURCE_SIZE_BYTES`), bounded extracted text
  (`MAX_EXTRACTED_TEXT_CHARS`), bounded chunk count/size, bounded
  metadata, declared-vs-actual type cross-checking, and format-specific
  guards (JSON nesting depth scanned *before* `json.loads` is ever
  called; CSV field length; PDF decompression bound; PDF object count
  bound).
- **No execution path.** No `eval`/`exec`/`os.system`/`subprocess`/
  `shell=True` anywhere in `sam.knowledge`. Document content is never
  imported or run as Python/JavaScript — see
  `tests/test_knowledge_security.py::test_21_...`/`test_22_...`.
- **No network calls.** No `urllib`/`requests`/raw sockets anywhere in
  this package; the only I/O `sam.knowledge` performs is the in-memory
  `InMemoryKnowledgeStore`/`InMemoryLexicalIndex` operating on data
  already handed to it.
- **Secret protection.** Filename patterns (`.env`, `*.pem`, `*.key`,
  `credentials.*`, `secret(s).*`, SSH private key names — reusing
  `sam.coding.policy`'s pattern set) and content patterns (reusing
  `sam.memory.sanitization.looks_like_secret`) are checked against the
  resource name/source label, every parsed segment's text, and every
  metadata value (title, author, custom entries). A match **rejects the
  entire ingestion** — Phase 7 never stores a redacted copy, unlike
  Phase 6's read-time redaction of an already-authorized file read.
- **Secrets split across segments.** A token cut by a PDF page break or
  a Markdown section boundary is invisible to a per-segment scan, so
  `ingestion._contains_secret` also scans the segment texts joined
  once with no separator (a token split mid-string) and once with a
  newline (a `password is` / value phrase split by a break), in addition
  to every individual segment. This is bounded: the extracted-text size
  limit (`MAX_EXTRACTED_TEXT_CHARS`) is enforced *before* the scan, so
  the joined text is at most that size plus one separator per segment.
  The detector is unchanged, only a boolean is returned, and no matched
  text is logged. It is deliberately conservative and can over-reject
  adjacent segments that happen to form a secret-shaped string. JSON
  elements and CSV rows are emitted with their own delimiters, so
  distinct values are not treated as one split secret; deliberately
  obfuscating a secret with inserted separators is out of scope for a
  pattern-based detector.
- **Audit is content-free.** `KnowledgeAuditEvent` has no field capable
  of holding document text, chunk text, query text, or a raw metadata
  value — only closed enums, ids, and bounded counts. This is
  structural (no such field exists to populate), not merely a
  convention.

## AgentCore boundary

`AgentCore` is not modified. Claude cannot call `KnowledgeStore` or
`KnowledgeIndex` directly, and no new API endpoint was added. The
intended future integration:

```
Claude / Agent
      │
      ▼
Tool Proposal
      │
      ▼
Permission Engine
      │
      ▼
KnowledgeEngine.retrieve(...)
      │
      ▼
Store / Index
```

LLM output is a proposal, never an authorization — the same discipline
every prior phase applies.

## Cloud-ready storage (not implemented yet)

`KnowledgeStore` and `KnowledgeIndex` are `Protocol`s.
`InMemoryKnowledgeStore`/`InMemoryLexicalIndex` are the only
implementations in Phase 7. The intended future substitution:

```
KnowledgeEngine
      ↓
KnowledgeStore Protocol
      ↓
PostgresKnowledgeStore
      ↓
PostgreSQL + pgvector
```

No PostgreSQL, Supabase, Redis, or vector database client is imported
anywhere in this package. Swapping in a real backend requires no change
to `KnowledgeEngine`, `KnowledgeRetriever`, or the Permission Engine
integration — only a new class implementing the same two `Protocol`s.

## Known limitations / technical debt

- `PDFParser` cannot handle encrypted PDFs or CID/Type0 font encodings;
  text from such a PDF may be incomplete or garbled rather than
  perfectly faithful (see "PDF" above).
- No OCR — a scanned/image-only PDF is honestly rejected, never
  silently "learned" as empty.
- `InMemoryLexicalIndex` is a simple deterministic term-overlap
  scorer, not semantic search — see "Embeddings" above for the reserved
  future boundary.
- No persistence across process restarts (same as every prior phase's
  in-memory reference implementation).
- No authentication yet (same as every prior phase).
- Secret detection is best-effort and documented as non-exhaustive
  (inherited from `sam.memory.sanitization`'s own documented scope).
- Ingestion rollback is compensating, not transactional, and the
  quarantine of an unrecoverable failed ingestion is in-memory only. A
  persistent backend should record an explicit ingestion-state marker
  (e.g. `pending`/`committed`) so an interrupted ingestion is never
  visible after a restart.
- `KnowledgeEngine.remove_resource` removes the index entry before the
  store entry; because both are in-memory, single-process operations
  that essentially cannot fail independently, this ordering is a
  reasonable approximation of a single transaction, but it is not one —
  revisit when a real distributed store/index backend is introduced.
- No collection-management API beyond implicit creation on first
  ingest; there is no `create_collection`/`rename_collection`.

## Future extension points

- A real `EmbeddingProvider` and a vector-backed `KnowledgeIndex`,
  slotting in behind the existing `Protocol` boundaries without
  changing `KnowledgeEngine`.
- A `PostgresKnowledgeStore` (PostgreSQL + pgvector).
- Wiring `KnowledgeEngine.retrieve` into `AgentCore` as an authorized
  tool, once a tool-proposal/confirmation flow exists for it.
- An explicit "remember this from the document" workflow that
  deliberately bridges Knowledge → Memory as a distinct, separately
  authorized action.
