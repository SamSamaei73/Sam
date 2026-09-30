import { useCallback, useEffect, useRef, useState } from "react";
import { BridgeError, toBridgeError } from "../bridge/bridge";
import type {
  EvidenceStrength,
  OperationResult,
  ProClaim,
  ProConflict,
  ProEvidence,
  ProfessionalPrivacy,
  ProfessionalProfile,
  ProfessionalSourceType,
  ProRequirement,
  ProSource,
} from "../bridge/types";
import { IconTrash, IconUpload } from "../components/Icons";
import { EmptyState, IconButton, NeonButton, Notice, SectionHeader, StatusPill, type Tone } from "../components/primitives";
import { MAX_UPLOAD_BYTES, humanize, resourceKindFor } from "../lib/format";
import { bytesToBase64 } from "../lib/wav";
import { useSam } from "../state";
import { Loader } from "../components/Loader";

type Tab =
  | "overview"
  | "skills"
  | "experience"
  | "projects"
  | "research"
  | "publications"
  | "education"
  | "evidence"
  | "gaps"
  | "sources";

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "skills", label: "Skills" },
  { id: "experience", label: "Experience" },
  { id: "projects", label: "Projects" },
  { id: "research", label: "Research" },
  { id: "publications", label: "Publications" },
  { id: "education", label: "Education" },
  { id: "evidence", label: "Evidence" },
  { id: "gaps", label: "Profile gaps" },
  { id: "sources", label: "Sources" },
];

const SOURCE_TYPES: { value: ProfessionalSourceType; label: string }[] = [
  { value: "master_cv", label: "Master CV" },
  { value: "publication", label: "Publication" },
  { value: "transcript", label: "Academic transcript" },
  { value: "project_documentation", label: "Project documentation" },
  { value: "github", label: "GitHub material you exported" },
  { value: "linkedin_export", label: "LinkedIn export" },
  { value: "owner_document", label: "Other professional document" },
];

const PRIVACY: { value: ProfessionalPrivacy; label: string }[] = [
  { value: "public", label: "Public" },
  { value: "personal", label: "Personal" },
  { value: "private", label: "Private" },
];

/**
 * Documentary support and your review are separate facts, shown as separate
 * pills. Your confirmation never raises documentary strength.
 */
type Support = "corroborated" | "single_source" | "no_document" | "inferred" | "unverified";

const SUPPORT_LABEL: Record<Support, string> = {
  corroborated: "Corroborated",
  single_source: "Single source",
  no_document: "No documentary evidence",
  inferred: "Inferred",
  unverified: "Unverified",
};
const SUPPORT_TONE: Record<Support, Tone> = {
  corroborated: "ok",
  single_source: "info",
  no_document: "muted",
  inferred: "warn",
  unverified: "muted",
};
const SUPPORT_HELP: Record<Support, string> = {
  corroborated:
    "Stated explicitly by two or more independent source families. Versions and copies of one document count once.",
  single_source: "Stated explicitly by one source family.",
  no_document: "No document states it. It is accepted only because you confirmed it.",
  inferred: "A rule-derived relationship, never an explicit fact.",
  unverified: "A suggestion with no trusted evidence yet.",
};
const CONFIRMED_LABEL = "Confirmed by you";
const CONFIRMED_HELP = "You vouched for it. Your confirmation is kept apart from document evidence and never counts as one.";

function supportOf(item: { strength: EvidenceStrength; accepted: boolean; inferred?: boolean }): Support {
  if (item.strength !== "none") return item.strength;
  if (item.accepted) return "no_document";
  return item.inferred ? "inferred" : "unverified";
}

function StatusPills({
  item,
}: {
  item: { strength: EvidenceStrength; accepted: boolean; inferred?: boolean; review: string };
}) {
  const support = supportOf(item);
  return (
    <>
      <StatusPill tone={SUPPORT_TONE[support]} label={SUPPORT_LABEL[support]} />
      {item.review === "owner_confirmed" ? <StatusPill tone="ok" label={CONFIRMED_LABEL} /> : null}
    </>
  );
}

function messageFor(error: unknown): string {
  return (error instanceof BridgeError ? error : toBridgeError(error)).message;
}

function locationText(e: ProEvidence): string {
  const parts: string[] = [];
  if (e.page_number !== null) parts.push(`Page ${e.page_number}`);
  if (e.section_title) parts.push(`Section: ${e.section_title}`);
  if (e.paragraph_index !== null) parts.push(`Paragraph ${e.paragraph_index}`);
  if (e.character_start !== null && e.character_end !== null) parts.push(`Characters ${e.character_start}–${e.character_end}`);
  return parts.length ? parts.join(" · ") : "Location not available";
}

/** Where a fact came from. Evidence text is untrusted document text: plain text only. */
function EvidenceList({ evidence }: { evidence: ProEvidence[] }) {
  if (evidence.length === 0) return <p className="faint">No evidence recorded.</p>;
  return (
    <ul className="evidence-list" aria-label="Evidence">
      {evidence.map((e) => (
        <li key={e.evidence_id}>
          <div className="card-row">
            <strong>{e.source_label}</strong>
            <span className="faint">
              {humanize(e.source_type)} · {humanize(e.nature)}
            </span>
          </div>
          {e.reference ? <p className="snippet">{e.reference}</p> : null}
          <div className="faint">
            {locationText(e)}
            {e.context_statement ? ` · Within: ${e.context_statement}` : ""}
          </div>
        </li>
      ))}
    </ul>
  );
}

function ClaimCard({
  claim,
  busy,
  onConfirm,
  onReject,
}: {
  claim: ProClaim;
  busy?: boolean;
  onConfirm?: (claim: ProClaim) => void;
  onReject?: (claim: ProClaim) => void;
}) {
  const reviewable = onConfirm && onReject && !claim.accepted;
  const extra = Object.entries(claim.attributes).filter(([name]) => !["skill_id", "display"].includes(name));
  return (
    <article className="card" aria-label={`Claim ${claim.statement}`}>
      <div className="card-row">
        <h4>{claim.statement}</h4>
        <span className="row">
          <StatusPills item={claim} />
          <StatusPill tone={claim.sensitivity === "private" ? "high" : "muted"} label={humanize(claim.sensitivity)} />
        </span>
      </div>
      <p className="faint">{SUPPORT_HELP[supportOf(claim)]}</p>
      {extra.length > 0 ? (
        <p className="faint">{extra.map(([name, value]) => `${humanize(name)}: ${value}`).join(" · ")}</p>
      ) : null}
      {claim.conflicted_attributes.length > 0 ? (
        <Notice tone="warn" live={false}>
          Sources disagree on: {claim.conflicted_attributes.map(humanize).join(", ")}. No value is chosen for you.
        </Notice>
      ) : null}
      <details>
        <summary>
          {claim.evidence.length} {claim.evidence.length === 1 ? "piece" : "pieces"} of evidence · {claim.source_count}{" "}
          {claim.source_count === 1 ? "source" : "sources"} · {claim.source_family_count} independent{" "}
          {claim.source_family_count === 1 ? "family" : "families"}
        </summary>
        <EvidenceList evidence={claim.evidence} />
      </details>
      {reviewable ? (
        <div className="row">
          {claim.canonical ? (
            <NeonButton disabled={busy} onClick={() => onConfirm(claim)}>
              Confirm “{claim.statement}”
            </NeonButton>
          ) : (
            <span className="faint">Not in Sam's trusted skill vocabulary, so it cannot be confirmed.</span>
          )}
          <NeonButton variant="quiet" disabled={busy} onClick={() => onReject(claim)}>
            Reject “{claim.statement}”
          </NeonButton>
        </div>
      ) : null}
    </article>
  );
}

function ClaimList({
  claims,
  empty,
  ...actions
}: {
  claims: ProClaim[];
  empty: string;
  busy?: boolean;
  onConfirm?: (claim: ProClaim) => void;
  onReject?: (claim: ProClaim) => void;
}) {
  if (claims.length === 0) return <EmptyState title="Nothing here yet" body={empty} />;
  return (
    <div className="grid">
      {claims.map((claim) => (
        <ClaimCard key={claim.claim_id} claim={claim} {...actions} />
      ))}
    </div>
  );
}

const MATCH_LABEL = {
  matched: "Matched",
  partially_supported: "Partially supported",
  not_supported: "Not supported",
  unknown: "Unknown",
} as const;
const MATCH_TONE: Record<keyof typeof MATCH_LABEL, Tone> = {
  matched: "ok",
  partially_supported: "warn",
  not_supported: "danger",
  unknown: "muted",
};

function RequirementResult({ result }: { result: ProRequirement }) {
  const groups: [string, ProClaim[]][] = [
    ["Skills", result.skills],
    ["Projects", result.projects],
    ["Employment", result.employment],
    ["Research and publications", result.research],
    ["Education", result.education],
  ];
  return (
    <section aria-label="Requirement result" className="panel glass">
      <div className="card-row">
        <h3>{result.normalized_requirement || "Requirement"}</h3>
        <StatusPill tone={MATCH_TONE[result.status]} label={MATCH_LABEL[result.status]} />
      </div>
      {result.unsupported_aspects.length > 0 ? (
        <>
          <h4>Not supported by your evidence</h4>
          <ul>
            {result.unsupported_aspects.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
        </>
      ) : null}
      {result.notes.map((n) => (
        <p key={n} className="faint">
          {n}
        </p>
      ))}
      {groups.map(([title, items]) =>
        items.length > 0 ? (
          <div key={title}>
            <h4>{title}</h4>
            <ClaimList claims={items} empty="" />
          </div>
        ) : null,
      )}
    </section>
  );
}

function ConflictCard({
  conflict,
  busy,
  onResolve,
}: {
  conflict: ProConflict;
  busy: boolean;
  onResolve: (conflict: ProConflict, optionId: string) => void;
}) {
  return (
    <article className="card" aria-label={`Conflict on ${conflict.attribute}`}>
      <div className="card-row">
        <h4>
          {conflict.kind === "role_overlap" ? "Different job titles for the same period" : `Sources disagree on ${humanize(conflict.attribute)}`}
        </h4>
        <StatusPill tone={conflict.resolved ? "ok" : "warn"} label={conflict.resolved ? "Resolved by you" : "Needs your decision"} />
      </div>
      <p className="faint">Both versions are kept. Only you can choose; Sam never picks the more favourable one.</p>
      <div className="row">
        {conflict.options.map((option) => (
          <NeonButton
            key={option.option_id}
            variant={conflict.resolved_value === option.value ? "primary" : "quiet"}
            disabled={busy || conflict.resolved}
            onClick={() => onResolve(conflict, option.option_id)}
          >
            Use “{option.value}”
          </NeonButton>
        ))}
      </div>
    </article>
  );
}

const CATEGORY_LABELS: Record<string, string> = {
  skill: "Skills",
  technology: "Technologies",
  employment: "Employment",
  education: "Education",
  project: "Projects",
  publication: "Publications",
  research: "Research topics",
  certification: "Certifications",
  achievement: "Achievements",
  responsibility: "Responsibilities",
  domain_experience: "Domains",
  language: "Languages",
  portfolio: "Portfolio",
  career_preference: "Career preferences",
};

export function ProfessionalView() {
  const { bridge, confirmable } = useSam();
  const [profile, setProfile] = useState<ProfessionalProfile | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone?: "danger" | "warn"; text: string } | null>(null);
  const [sourceType, setSourceType] = useState<ProfessionalSourceType>("master_cv");
  const [privacy, setPrivacy] = useState<ProfessionalPrivacy>("personal");
  const [useCandidates, setUseCandidates] = useState(false);
  const [requirement, setRequirement] = useState("");
  const [requirementResult, setRequirementResult] = useState<ProRequirement | null>(null);
  const [search, setSearch] = useState("");
  const [found, setFound] = useState<ProClaim[] | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const report = (result: OperationResult, success?: string) => {
    if (result.status === "ok") setNotice(success ? { text: success } : null);
    else setNotice({ tone: result.status === "denied" ? "warn" : "danger", text: result.message ?? "That couldn't be completed." });
  };

  const load = useCallback(async () => {
    try {
      const result = await bridge.professionalProfile();
      if (result.status === "ok") setProfile(result);
      else {
        setProfile(null);
        setNotice({ tone: "warn", text: result.message ?? "Your professional profile couldn't be loaded." });
      }
    } catch (error) {
      setProfile(null);
      setNotice({ tone: "danger", text: messageFor(error) });
    }
  }, [bridge]);

  useEffect(() => {
    void load();
  }, [load]);

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
      const contentBase64 = bytesToBase64(new Uint8Array(await file.arrayBuffer()));
      const result = await bridge.professionalIngest({
        name: file.name,
        sourceType,
        privacyClass: privacy,
        resourceType: kind,
        contentBase64,
        useCandidates,
      });
      if (result.status === "ok") {
        const state =
          result.ingest_status === "unchanged"
            ? "It was already in your profile, so nothing changed."
            : `${result.claims_created} new and ${result.claims_updated} updated items` +
              (result.conflicts_open > 0 ? `; ${result.conflicts_open} conflict(s) need your decision` : "") +
              (result.candidates_accepted > 0 ? `; ${result.candidates_accepted} model suggestion(s) await your review (unverified)` : "") +
              ".";
        setNotice({ text: `Added “${file.name}”. ${state}` });
      } else report(result);
      await load();
    } catch (error) {
      setNotice({ tone: "danger", text: messageFor(error) });
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const review = async (input: Parameters<typeof bridge.professionalReview>[0], success: string) => {
    setBusy(true);
    setNotice(null);
    try {
      report(await bridge.professionalReview(input), success);
      await load();
    } catch (error) {
      setNotice({ tone: "danger", text: messageFor(error) });
    } finally {
      setBusy(false);
    }
  };

  const remove = async (source: ProSource) => {
    setBusy(true);
    setNotice(null);
    try {
      const result = await confirmable((confirmationId) => bridge.professionalRemove(source.source_id, confirmationId));
      report(result, `Removed “${source.label}” and only its own evidence. Other sources keep theirs.`);
      await load();
    } catch (error) {
      setNotice({ tone: "danger", text: messageFor(error) });
    } finally {
      setBusy(false);
    }
  };

  const check = async () => {
    const text = requirement.trim();
    if (!text) return;
    setBusy(true);
    setNotice(null);
    try {
      const result = await bridge.professionalQuery("evidence_for", text);
      if (result.status === "ok") setRequirementResult(result.requirement);
      else {
        setRequirementResult(null);
        report(result);
      }
    } catch (error) {
      setNotice({ tone: "danger", text: messageFor(error) });
    } finally {
      setBusy(false);
    }
  };

  const runSearch = async () => {
    const text = search.trim();
    if (!text) return;
    setBusy(true);
    setNotice(null);
    try {
      const result = await bridge.professionalQuery("search", text);
      setFound(result.status === "ok" ? result.claims : []);
      if (result.status !== "ok") report(result);
    } catch (error) {
      setNotice({ tone: "danger", text: messageFor(error) });
    } finally {
      setBusy(false);
    }
  };

  const claims = profile?.claims ?? [];
  const byCategory = (...categories: string[]) => claims.filter((c) => categories.includes(c.category));
  const confirm = (claim: ProClaim) =>
    void review({ action: "confirm", claimId: claim.claim_id }, `Confirmed “${claim.statement}”. Recorded as your confirmation; no document evidence was added or changed.`);
  const reject = (claim: ProClaim) =>
    void review({ action: "reject", claimId: claim.claim_id }, `Rejected “${claim.statement}”. It won't be suggested again.`);
  const experience = profile?.experience ?? null;

  return (
    <div className="page-narrow">
      <SectionHeader
        title="Professional"
        description="Your evidence-backed professional profile. Every fact shows where it came from and how strongly it is supported. Stored only on this Mac, in Sam's owner-only data folder (a development build keeps it for this session only)."
        actions={
          <NeonButton disabled={busy} onClick={() => fileRef.current?.click()}>
            <IconUpload /> Add source
          </NeonButton>
        }
      />
      <input
        ref={fileRef}
        type="file"
        className="sr-only"
        tabIndex={-1}
        aria-label="Choose a professional document"
        accept=".pdf,.txt,.md,.markdown,.json,.csv"
        onChange={(event) => void upload(event.target.files?.[0])}
      />
      <fieldset className="row panel glass" aria-label="How to add the next source">
        <label>
          Source type{" "}
          <select
            className="text-input"
            value={sourceType}
            onChange={(event) => setSourceType(event.target.value as ProfessionalSourceType)}
          >
            {SOURCE_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Privacy{" "}
          <select
            className="text-input"
            value={privacy}
            onChange={(event) => setPrivacy(event.target.value as ProfessionalPrivacy)}
          >
            {PRIVACY.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
        <label className="row">
          <input type="checkbox" checked={useCandidates} onChange={(event) => setUseCandidates(event.target.checked)} />
          Also ask a model to suggest extra items (they stay unverified until you confirm them)
        </label>
      </fieldset>
      {notice ? <Notice tone={notice.tone}>{notice.text}</Notice> : null}

      <div role="tablist" aria-label="Professional sections" className="chips">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`pro-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`pro-panel-${t.id}`}
            className="chip"
            data-active={tab === t.id ? "true" : "false"}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`pro-panel-${tab}`} aria-labelledby={`pro-tab-${tab}`}>
        {profile === null ? (
          <Loader />
        ) : null}

        {profile !== null && tab === "overview" ? (
          claims.length === 0 ? (
            <EmptyState
              title="No professional sources yet"
              body="Add your CV, a publication or a transcript. Sam builds your profile only from documents you choose, and never from memory or from what it is told."
            />
          ) : (
            <>
              <section aria-label="Profile summary" className="grid">
                {Object.entries(profile.counts).map(([category, count]) => (
                  <article className="card" key={category} aria-label={`${CATEGORY_LABELS[category] ?? humanize(category)} count`}>
                    <div className="card-row">
                      <h4>{CATEGORY_LABELS[category] ?? humanize(category)}</h4>
                      <strong>{count}</strong>
                    </div>
                  </article>
                ))}
              </section>
              {experience && experience.total_months > 0 ? (
                <p>
                  Experience from resolved dates: <strong>{experience.total_years} years {experience.remainder_months} months</strong>
                  {experience.excluded_conflicted + experience.excluded_incomplete > 0
                    ? ` (${experience.excluded_conflicted + experience.excluded_incomplete} role(s) excluded until their dates are resolved)`
                    : ""}
                  . Computed by code, never estimated.
                </p>
              ) : null}
              <p className="faint">
                {profile.sources.length} source(s) · {profile.conflicts.filter((c) => !c.resolved).length} open conflict(s) ·{" "}
                {profile.gaps.length} evidence gap(s)
              </p>
              <form
                className="row panel glass"
                role="search"
                onSubmit={(event) => {
                  event.preventDefault();
                  void check();
                }}
              >
                <label className="sr-only" htmlFor="pro-requirement">
                  Requirement to check
                </label>
                <input
                  id="pro-requirement"
                  className="text-input row-grow"
                  value={requirement}
                  maxLength={300}
                  placeholder="Check a requirement, e.g. “production RAG systems”…"
                  onChange={(event) => setRequirement(event.target.value)}
                />
                <NeonButton type="submit" disabled={busy || requirement.trim() === ""}>
                  Check evidence
                </NeonButton>
              </form>
              {requirementResult ? <RequirementResult result={requirementResult} /> : null}
            </>
          )
        ) : null}

        {profile !== null && tab === "skills" ? (
          <ClaimList
            claims={byCategory("skill", "technology")}
            empty="No skills yet. Skills appear only when a source you added states them."
            busy={busy}
            onConfirm={confirm}
            onReject={reject}
          />
        ) : null}

        {profile !== null && tab === "experience" ? (
          experience && experience.entries.length > 0 ? (
            <>
              <p>
                Total from resolved dates (overlaps counted once):{" "}
                <strong>
                  {experience.total_years} years {experience.remainder_months} months
                </strong>{" "}
                · conservative lower bound {Math.floor(experience.conservative_months / 12)} years {experience.conservative_months % 12} months
              </p>
              <div className="grid">
                {experience.entries.map((e) => (
                  <article className="card" key={e.claim_id} aria-label={`Role ${e.title} at ${e.employer}`}>
                    <div className="card-row">
                      <h4>
                        {e.title} at {e.employer}
                      </h4>
                      <StatusPill
                        tone={e.status === "resolved" ? "ok" : "warn"}
                        label={e.status === "resolved" ? `${e.months} months` : humanize(e.status)}
                      />
                    </div>
                    <p className="faint">
                      {e.start ?? "?"} to {e.end ?? "?"}
                      {e.approximate ? " · approximate (year only)" : ""}
                    </p>
                  </article>
                ))}
              </div>
              {experience.gaps.map((g) => (
                <p key={g} className="faint">
                  No employment evidence from {g}
                </p>
              ))}
              <h3>Responsibilities and achievements</h3>
              <ClaimList
                claims={byCategory("responsibility", "achievement")}
                empty="No responsibilities or achievements were found in your sources."
              />
            </>
          ) : (
            <EmptyState title="No experience yet" body="Add a CV or a LinkedIn export that lists your roles with dates." />
          )
        ) : null}

        {profile !== null && tab === "projects" ? (
          <ClaimList claims={byCategory("project")} empty="No projects yet. Add a CV, project documentation or repository README." />
        ) : null}

        {profile !== null && tab === "research" ? (
          <ClaimList
            claims={byCategory("research", "domain_experience")}
            empty="No research topics or domains yet. Topics from a paper you co-authored appear as inferred, never as explicit facts."
          />
        ) : null}

        {profile !== null && tab === "publications" ? (
          profile.publications.length === 0 ? (
            <EmptyState title="No publications yet" body="Add a paper, or a CV that lists your publications." />
          ) : (
            <div className="grid">
              {profile.publications.map((p) => (
                <article className="card" key={p.claim_id} aria-label={`Publication ${p.title}`}>
                  <div className="card-row">
                    <h4>{p.title}</h4>
                    <span className="row">
                      <StatusPills item={p} />
                    </span>
                  </div>
                  <p className="faint">
                    {[p.venue, p.year, p.doi ? `DOI ${p.doi}` : null].filter(Boolean).join(" · ") || "Venue not stated"}
                  </p>
                  {p.authors.length > 0 ? <p>Authors: {p.authors.join(", ")}</p> : null}
                  {p.owner_author_position !== null ? <p>You are author #{p.owner_author_position}.</p> : null}
                  <p className="faint">
                    {p.owner_contribution
                      ? `Your contribution (stated by the paper): ${p.owner_contribution}`
                      : "No specific contribution by you is recorded. Authorship alone does not imply one."}
                  </p>
                  {p.abstract ? <p className="snippet">{p.abstract}</p> : null}
                  {p.topics.length > 0 ? <p className="faint">Topics: {p.topics.join(", ")}</p> : null}
                  {p.methods ? <p className="faint">Methods: {p.methods}</p> : null}
                  {p.datasets ? <p className="faint">Datasets: {p.datasets}</p> : null}
                  {p.findings ? <p className="faint">Findings: {p.findings}</p> : null}
                  {p.limitations ? <p className="faint">Limitations: {p.limitations}</p> : null}
                  <details>
                    <summary>{p.evidence.length} piece(s) of evidence</summary>
                    <EvidenceList evidence={p.evidence} />
                  </details>
                </article>
              ))}
            </div>
          )
        ) : null}

        {profile !== null && tab === "education" ? (
          profile.education.length === 0 ? (
            <EmptyState title="No education yet" body="Add a CV or an academic transcript." />
          ) : (
            <div className="grid">
              {profile.education.map((e) => (
                <article className="card" key={e.claim_id} aria-label={`Education ${e.degree} ${e.subject}`}>
                  <div className="card-row">
                    <h4>
                      {e.degree} in {e.subject}
                    </h4>
                    <span className="row">
                      <StatusPills item={e} />
                    </span>
                  </div>
                  <p className="faint">
                    {[e.institution, e.classification, e.start || e.end ? `${e.start ?? "?"} to ${e.end ?? "?"}` : null]
                      .filter(Boolean)
                      .join(" · ")}
                  </p>
                  {e.dissertation ? <p>Dissertation: {e.dissertation}</p> : null}
                  {e.modules.length > 0 ? <p className="faint">Modules: {e.modules.join(", ")}</p> : null}
                  {e.conflicted_attributes.length > 0 ? (
                    <Notice tone="warn" live={false}>
                      Sources disagree on: {e.conflicted_attributes.map(humanize).join(", ")}.
                    </Notice>
                  ) : null}
                  <details>
                    <summary>{e.evidence.length} piece(s) of evidence</summary>
                    <EvidenceList evidence={e.evidence} />
                  </details>
                </article>
              ))}
            </div>
          )
        ) : null}

        {profile !== null && tab === "evidence" ? (
          <>
            <form
              className="row panel glass"
              role="search"
              onSubmit={(event) => {
                event.preventDefault();
                void runSearch();
              }}
            >
              <label className="sr-only" htmlFor="pro-search">
                Search your evidence
              </label>
              <input
                id="pro-search"
                className="text-input row-grow"
                value={search}
                maxLength={300}
                placeholder="Search skills, projects, publications…"
                onChange={(event) => setSearch(event.target.value)}
              />
              <NeonButton type="submit" disabled={busy || search.trim() === ""}>
                Search
              </NeonButton>
            </form>
            {found !== null ? <ClaimList claims={found} empty="Nothing in your evidence matched that search." /> : null}
            <h3>Evidence strength</h3>
            <ul>
              {(Object.keys(SUPPORT_LABEL) as Support[]).map((s) => (
                <li key={s}>
                  <strong>{SUPPORT_LABEL[s]}</strong>: {SUPPORT_HELP[s]}
                </li>
              ))}
              <li>
                <strong>{CONFIRMED_LABEL}</strong>: {CONFIRMED_HELP}
              </li>
            </ul>
          </>
        ) : null}

        {profile !== null && tab === "gaps" ? (
          <>
            <h3>Conflicts</h3>
            {profile.conflicts.length === 0 ? (
              <p className="faint">No conflicting evidence.</p>
            ) : (
              <div className="grid">
                {profile.conflicts.map((c) => (
                  <ConflictCard
                    key={c.conflict_id}
                    conflict={c}
                    busy={busy}
                    onResolve={(conflict, optionId) =>
                      void review(
                        { action: "resolve", conflictId: conflict.conflict_id, optionId },
                        "Recorded your choice.",
                      )
                    }
                  />
                ))}
              </div>
            )}
            <h3>Evidence gaps</h3>
            <p className="faint">These describe your evidence, not you. Sam does not rate or score you.</p>
            {profile.gaps.length === 0 ? (
              <p className="faint">No gaps found.</p>
            ) : (
              <ul aria-label="Evidence gaps">
                {profile.gaps.map((g, index) => (
                  <li key={`${g.kind}-${index}`}>
                    <strong>{humanize(g.kind)}</strong>: {g.detail}
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : null}

        {profile !== null && tab === "sources" ? (
          profile.sources.length === 0 ? (
            <EmptyState title="No sources yet" body="Add a document to start building your profile." />
          ) : (
            <div className="grid">
              {profile.sources.map((s) => (
                <article className="card" key={s.source_id} aria-label={`Source ${s.label}`}>
                  <div className="card-row">
                    <h4>{s.label}</h4>
                    <StatusPill tone={s.freshness === "fresh" ? "ok" : s.freshness === "aging" ? "warn" : "danger"} label={humanize(s.freshness)} />
                  </div>
                  <p className="faint">
                    {humanize(s.source_type)} · {s.accepted_claims} accepted item(s) · {s.pending_candidates} awaiting review ·{" "}
                    {s.conflicts} conflict(s)
                    {s.candidate_extraction !== "off" ? ` · model suggestions: ${s.candidate_extraction}` : ""}
                  </p>
                  <div className="card-row">
                    <label>
                      Privacy{" "}
                      <select
                        className="text-input"
                        aria-label={`Privacy for ${s.label}`}
                        value={s.privacy_class}
                        disabled={busy}
                        onChange={(event) =>
                          void review(
                            { action: "set_privacy", sourceId: s.source_id, privacyClass: event.target.value as ProfessionalPrivacy },
                            `Set “${s.label}” to ${event.target.value}.`,
                          )
                        }
                      >
                        {PRIVACY.map((p) => (
                          <option key={p.value} value={p.value}>
                            {p.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <IconButton label={`Remove ${s.label}`} disabled={busy} onClick={() => void remove(s)}>
                      <IconTrash />
                    </IconButton>
                  </div>
                </article>
              ))}
            </div>
          )
        ) : null}
      </div>
    </div>
  );
}
