import { useCallback, useEffect, useState } from "react";
import { BridgeError, toBridgeError } from "../bridge/bridge";
import type {
  CareerApplication,
  CareerDocument,
  CareerFit,
  CareerLine,
  CareerOpportunity,
  CareerOutreach,
  CareerOverview,
  CareerRole,
  CareerSourceKind,
  OperationResult,
} from "../bridge/types";
import { EmptyState, NeonButton, Notice, SectionHeader, StatusPill, type Tone } from "../components/primitives";
import { humanize } from "../lib/format";
import { useSam } from "../state";

type Tab = "review" | "opportunities" | "applications" | "phd" | "drafts" | "contacts" | "followups";

const TABS: { id: Tab; label: string }[] = [
  { id: "review", label: "Review queue" },
  { id: "opportunities", label: "Opportunities" },
  { id: "applications", label: "Applications" },
  { id: "phd", label: "PhD" },
  { id: "drafts", label: "Drafts" },
  { id: "contacts", label: "Contacts" },
  { id: "followups", label: "Follow-ups" },
];

const SOURCES: { value: CareerSourceKind; label: string }[] = [
  { value: "official_career_page", label: "Official careers page" },
  { value: "official_ats", label: "Official application system" },
  { value: "university_page", label: "University / department page" },
  { value: "funding_page", label: "Funding / programme page" },
  { value: "supervisor_page", label: "Supervisor / group page" },
  { value: "linkedin", label: "LinkedIn (pasted text)" },
  { value: "indeed", label: "Indeed (pasted text)" },
  { value: "glassdoor", label: "Glassdoor (pasted text)" },
  { value: "academic_source", label: "Academic source" },
  { value: "other", label: "Other" },
];

const FIT_TONE: Record<string, Tone> = {
  supported: "ok",
  partially_supported: "warn",
  not_supported: "danger",
  unknown: "muted",
  supported_alignment: "ok",
  partial_alignment: "warn",
  no_evidence: "danger",
};

function messageFor(error: unknown): string {
  return (error instanceof BridgeError ? error : toBridgeError(error)).message;
}

function Lines({ lines }: { lines: CareerLine[] }) {
  return (
    <ul className="evidence-list" aria-label="Draft lines">
      {lines.map((line, index) => (
        <li key={index} data-flagged={line.flagged ? "true" : "false"}>
          {/* Draft text is shown as plain text only. */}
          <span>{line.text}</span>{" "}
          {line.fact ? (
            <StatusPill tone="info" label={`${line.evidence_count} evidence`} />
          ) : null}
          {line.flagged ? <StatusPill tone="warn" label={`Needs review: ${humanize(line.flag_reason ?? "flagged")}`} /> : null}
        </li>
      ))}
    </ul>
  );
}

function OpportunityCard({
  item,
  busy,
  onFit,
  onDraft,
}: {
  item: CareerOpportunity;
  busy: boolean;
  onFit: (item: CareerOpportunity) => void;
  onDraft: (item: CareerOpportunity) => void;
}) {
  return (
    <article className="card" aria-label={`Opportunity ${item.title}`}>
      <div className="card-row">
        <h4>
          {item.title} · {item.organization}
        </h4>
        <span className="row">
          <StatusPill tone={item.status === "active" ? "ok" : "muted"} label={humanize(item.status)} />
          {item.tracked ? <StatusPill tone="info" label="Tracked" /> : null}
        </span>
      </div>
      <p className="faint">
        {item.location ?? "Location unknown"} · {humanize(item.work_mode)} · Source: {humanize(item.source)}
        {item.source_count > 1 ? ` (+${item.source_count - 1} more)` : ""} · Deadline: {item.deadline ?? "unknown"}
      </p>
      <p className="faint">
        {item.type === "phd" ? `Funding: ${item.funding ?? "not stated"}` : `Salary: ${item.compensation ?? "not stated"}`} ·
        Sponsorship: {humanize(item.sponsorship)}
      </p>
      {item.requirements.length > 0 ? (
        <ul>
          {item.requirements.map((r) => (
            <li key={r.text}>
              {r.text}
              {r.preferred ? " (preferred)" : ""}
            </li>
          ))}
        </ul>
      ) : null}
      <div className="row">
        <NeonButton variant="quiet" disabled={busy} onClick={() => onFit(item)}>
          Check evidence for “{item.title}”
        </NeonButton>
        <NeonButton variant="quiet" disabled={busy || item.status !== "active"} onClick={() => onDraft(item)}>
          Draft application for “{item.title}”
        </NeonButton>
      </div>
    </article>
  );
}

function FitPanel({ fit }: { fit: CareerFit }) {
  return (
    <section className="panel glass" aria-label="Evidence check">
      <h3>Evidence check</h3>
      <p className="faint">Each requirement is matched to your professional evidence. There is no overall score.</p>
      <ul className="evidence-list">
        {fit.requirements.map((r) => (
          <li key={r.requirement}>
            <div className="card-row">
              <span>{r.requirement}</span>
              <StatusPill tone={FIT_TONE[r.status] ?? "muted"} label={humanize(r.status)} />
            </div>
            {r.claims.length > 0 ? (
              <div className="faint">Evidence: {r.claims.map((c) => c.statement).join("; ")}</div>
            ) : (
              <div className="faint">No evidence in your profile.</div>
            )}
            {r.unsupported.length > 0 ? <div className="faint">Gaps: {r.unsupported.join(", ")}</div> : null}
          </li>
        ))}
      </ul>
      {fit.alignment.length > 0 ? (
        <>
          <h4>Research alignment: {humanize(fit.alignment_status ?? "unknown")}</h4>
          <ul>
            {fit.alignment.map((a) => (
              <li key={a.topic}>
                {a.topic}: {humanize(a.status)}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}

function DocumentCard({
  doc,
  busy,
  onApprove,
}: {
  doc: CareerDocument;
  busy: boolean;
  onApprove: (doc: CareerDocument) => void;
}) {
  return (
    <details className="card">
      <summary>
        {humanize(doc.kind)} v{doc.version} · {doc.approved ? "approved" : "not approved"}
        {doc.unresolved > 0 ? ` · ${doc.unresolved} line(s) need review` : ""}
      </summary>
      <Lines lines={doc.lines} />
      {!doc.approved ? (
        <NeonButton variant="quiet" disabled={busy || doc.unresolved > 0} onClick={() => onApprove(doc)}>
          Approve {humanize(doc.kind).toLowerCase()}
        </NeonButton>
      ) : null}
    </details>
  );
}

function ApplicationCard({
  app,
  title,
  busy,
  onAnswer,
  onApproveDocument,
  onApprove,
  onSubmit,
  onWithdraw,
}: {
  app: CareerApplication;
  title: string;
  busy: boolean;
  onAnswer: (app: CareerApplication, questionId: string, text: string) => void;
  onApproveDocument: (doc: CareerDocument) => void;
  onApprove: (app: CareerApplication) => void;
  onSubmit: (app: CareerApplication) => void;
  onWithdraw: (app: CareerApplication) => void;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  return (
    <article className="card" aria-label={`Application ${title}`}>
      <div className="card-row">
        <h4>{title}</h4>
        <StatusPill
          tone={app.state === "submitted" ? "ok" : app.state === "blocked" ? "danger" : app.state === "outcome_unknown" ? "warn" : "info"}
          label={humanize(app.state)}
        />
      </div>
      {app.state === "outcome_unknown" ? (
        <p role="alert">
          Sam could not confirm whether this application reached the employer. It will not retry on its own. Please check with them before
          doing anything else.
        </p>
      ) : null}
      <p className="faint">
        Version {app.version}
        {app.last_failure ? ` · Last problem: ${humanize(app.last_failure)}` : ""}
        {app.submitted_at ? ` · Submitted (verified) ${app.submitted_at}` : ""}
      </p>
      {app.documents.map((doc) => (
        <DocumentCard key={doc.document_id} doc={doc} busy={busy} onApprove={onApproveDocument} />
      ))}
      {app.questions.length > 0 ? <h4>Questions</h4> : null}
      {app.questions.map((q) => (
        <div key={q.question_id} className="row">
          <span>
            {q.text}{" "}
            {q.classification === "owner_review_required" ? <StatusPill tone="warn" label="Only you can answer this" /> : null}
          </span>
          {q.answered ? (
            <span className="faint">Answered</span>
          ) : (
            <form
              className="row"
              onSubmit={(event) => {
                event.preventDefault();
                onAnswer(app, q.question_id, answers[q.question_id] ?? "");
              }}
            >
              <label className="sr-only" htmlFor={`answer-${app.draft_id}-${q.question_id}`}>
                Your answer to {q.text}
              </label>
              <input
                id={`answer-${app.draft_id}-${q.question_id}`}
                className="text-input"
                maxLength={2000}
                value={answers[q.question_id] ?? ""}
                onChange={(e) => setAnswers((a) => ({ ...a, [q.question_id]: e.target.value }))}
              />
              <NeonButton type="submit" variant="quiet" disabled={busy || !(answers[q.question_id] ?? "").trim()}>
                Save answer
              </NeonButton>
            </form>
          )}
        </div>
      ))}
      <div className="row">
        {app.state === "ready_for_owner_review" ? (
          <NeonButton disabled={busy} onClick={() => onApprove(app)}>
            Approve this package
          </NeonButton>
        ) : null}
        {app.state === "approved_for_submission" ? (
          <NeonButton disabled={busy} onClick={() => onSubmit(app)}>
            Submit (asks for your confirmation)
          </NeonButton>
        ) : null}
        {!["withdrawn", "expired", "submitting", "outcome_unknown"].includes(app.state) ? (
          <NeonButton variant="quiet" disabled={busy} onClick={() => onWithdraw(app)}>
            Withdraw
          </NeonButton>
        ) : null}
      </div>
    </article>
  );
}

function OutreachCard({
  draft,
  busy,
  onApprove,
  onSend,
}: {
  draft: CareerOutreach;
  busy: boolean;
  onApprove: (draft: CareerOutreach) => void;
  onSend: (draft: CareerOutreach) => void;
}) {
  return (
    <article className="card" aria-label={`Draft ${humanize(draft.kind)} ${draft.outreach_id}`}>
      <div className="card-row">
        <h4>
          {humanize(draft.kind)} · {humanize(draft.channel)}
        </h4>
        <StatusPill tone={draft.state === "sent" ? "ok" : draft.state === "outcome_unknown" ? "warn" : "info"} label={humanize(draft.state)} />
      </div>
      {draft.state === "outcome_unknown" ? (
        <p role="alert">Sam could not confirm whether this message was delivered. It will not resend it on its own.</p>
      ) : null}
      {draft.subject ? <p className="faint">Subject: {draft.subject}</p> : null}
      <Lines lines={draft.lines} />
      {!draft.sendable ? <p className="faint">Draft only: copy it and send it yourself. Sam never sends LinkedIn or platform messages.</p> : null}
      <div className="row">
        {draft.state === "ready_for_owner_review" ? (
          <NeonButton variant="quiet" disabled={busy || draft.unresolved > 0} onClick={() => onApprove(draft)}>
            Approve draft
          </NeonButton>
        ) : null}
        {draft.state === "approved" && draft.sendable ? (
          <NeonButton disabled={busy} onClick={() => onSend(draft)}>
            Send (asks for your confirmation)
          </NeonButton>
        ) : null}
      </div>
    </article>
  );
}

export function CareerView() {
  const { bridge, confirmable } = useSam();
  const [data, setData] = useState<CareerOverview | null>(null);
  const [tab, setTab] = useState<Tab>("review");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone?: "danger" | "warn"; text: string } | null>(null);
  const [fit, setFit] = useState<CareerFit | null>(null);
  const [listing, setListing] = useState({ text: "", url: "", source: "official_career_page" as CareerSourceKind, type: "job" as "job" | "phd" });
  const [questions, setQuestions] = useState("");
  const [contact, setContact] = useState({ name: "", role: "recruiter" as CareerRole, organization: "", url: "", quote: "", email: "" });

  const report = (result: OperationResult, success?: string) => {
    if (result.status === "ok") setNotice(success ? { text: success } : null);
    else setNotice({ tone: result.status === "denied" ? "warn" : "danger", text: result.message ?? "That couldn't be completed." });
  };

  const load = useCallback(async () => {
    try {
      const result = await bridge.careerOverview();
      if (result.status === "ok") setData(result);
      else {
        setData(null);
        setNotice({ tone: "warn", text: result.message ?? "Career data couldn't be loaded." });
      }
    } catch (error) {
      setData(null);
      setNotice({ tone: "danger", text: messageFor(error) });
    }
  }, [bridge]);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (run: () => Promise<OperationResult>, success: string) => {
    setBusy(true);
    setNotice(null);
    try {
      report(await run(), success);
      await load();
    } catch (error) {
      setNotice({ tone: "danger", text: messageFor(error) });
    } finally {
      setBusy(false);
    }
  };

  const titles = new Map((data?.opportunities ?? []).map((o) => [o.opportunity_id, `${o.title} · ${o.organization}`]));
  const jobs = (data?.opportunities ?? []).filter((o) => o.type === "job");
  const phds = (data?.opportunities ?? []).filter((o) => o.type === "phd");

  const checkFit = async (item: CareerOpportunity) => {
    setBusy(true);
    try {
      const result = await bridge.careerFit(item.opportunity_id);
      if (result.status === "ok") setFit(result);
      else report(result);
    } catch (error) {
      setNotice({ tone: "danger", text: messageFor(error) });
    } finally {
      setBusy(false);
    }
  };
  const draft = (item: CareerOpportunity) =>
    void act(
      () =>
        bridge.careerDraft({
          action: "create",
          opportunityId: item.opportunity_id,
          questions: questions.split("\n").map((q) => q.trim()).filter(Boolean),
        }),
      `Drafted an application for “${item.title}”. Review it in Applications. Nothing was submitted.`,
    );

  const cards = (items: CareerOpportunity[], empty: string) =>
    items.length === 0 ? (
      <EmptyState title="Nothing here yet" body={empty} />
    ) : (
      <div className="grid">
        {items.map((item) => (
          <OpportunityCard key={item.opportunity_id} item={item} busy={busy} onFit={(i) => void checkFit(i)} onDraft={draft} />
        ))}
      </div>
    );

  return (
    <div className="page-narrow">
      <SectionHeader
        title="Career"
        description="Jobs and PhD opportunities, evidence checks and application drafts. Review first: Sam drafts locally and never submits or sends without your confirmation of that exact package or message. Kept in memory for this session only."
      />
      {data && !data.submission_available ? (
        <Notice tone="warn" live={false}>
          Sam can't submit applications or send e-mail yet. Review the drafts here, then apply or send yourself.
        </Notice>
      ) : null}
      {notice ? <Notice tone={notice.tone}>{notice.text}</Notice> : null}

      <div role="tablist" aria-label="Career sections" className="chips">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`career-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`career-panel-${t.id}`}
            className="chip"
            data-active={tab === t.id ? "true" : "false"}
            onClick={() => setTab(t.id)}
          >
            {t.label}
            {t.id === "review" && (data?.review_queue.length ?? 0) > 0 ? ` (${data?.review_queue.length})` : ""}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`career-panel-${tab}`} aria-labelledby={`career-tab-${tab}`}>
        {data === null ? (
          <p className="muted" role="status">
            Loading…
          </p>
        ) : null}

        {data !== null && tab === "review" ? (
          data.review_queue.length === 0 ? (
            <EmptyState title="Nothing needs you" body="Answers, approvals, deadlines and follow-ups that need you appear here." />
          ) : (
            <ul className="evidence-list" aria-label="Review queue">
              {data.review_queue.map((item, index) => (
                <li key={`${item.kind}-${index}`}>
                  <strong>{item.label}</strong>
                </li>
              ))}
            </ul>
          )
        ) : null}

        {data !== null && (tab === "opportunities" || tab === "phd") ? (
          <>
            <form
              className="panel glass"
              aria-label="Add an opportunity"
              onSubmit={(event) => {
                event.preventDefault();
                void act(
                  () =>
                    bridge.careerOpportunity({
                      action: "import",
                      text: listing.text,
                      url: listing.url.trim(),
                      sourceKind: listing.source,
                      opportunityType: tab === "phd" ? "phd" : listing.type,
                    }),
                  "Added. Only facts the listing states explicitly were recorded.",
                ).then(() => setListing((l) => ({ ...l, text: "", url: "" })));
              }}
            >
              <h3>Add {tab === "phd" ? "a PhD opportunity" : "an opportunity"}</h3>
              <p className="faint">Paste the listing text and its https link. The text is treated as data, never as instructions.</p>
              <label>
                Link{" "}
                <input className="text-input" value={listing.url} maxLength={2000} onChange={(e) => setListing({ ...listing, url: e.target.value })} />
              </label>
              <label>
                Source{" "}
                <select className="text-input" value={listing.source} onChange={(e) => setListing({ ...listing, source: e.target.value as CareerSourceKind })}>
                  {SOURCES.map((s) => (
                    <option key={s.value} value={s.value}>
                      {s.label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Listing text{" "}
                <textarea className="text-input" rows={4} maxLength={40000} value={listing.text} onChange={(e) => setListing({ ...listing, text: e.target.value })} />
              </label>
              <NeonButton type="submit" disabled={busy || !listing.text.trim() || !listing.url.trim()}>
                Add
              </NeonButton>
            </form>
            <label>
              Application questions for new drafts (one per line){" "}
              <textarea className="text-input" rows={2} value={questions} maxLength={4000} onChange={(e) => setQuestions(e.target.value)} />
            </label>
            {fit ? <FitPanel fit={fit} /> : null}
            {tab === "phd"
              ? cards(phds, "Add a funded PhD from an official university or funding page.")
              : cards(jobs, "Add a job from an official careers page, or paste a listing.")}
          </>
        ) : null}

        {data !== null && tab === "applications" ? (
          data.applications.length === 0 ? (
            <EmptyState title="No applications" body="Draft an application from an opportunity." />
          ) : (
            <div className="grid">
              {data.applications.map((app) => (
                <ApplicationCard
                  key={app.draft_id}
                  app={app}
                  title={titles.get(app.opportunity_id) ?? app.opportunity_id}
                  busy={busy}
                  onAnswer={(a, questionId, text) =>
                    void act(() => bridge.careerDraft({ action: "answer", draftId: a.draft_id, questionId, text }), "Saved your answer for this application only.")
                  }
                  onApproveDocument={(doc) =>
                    void act(() => bridge.careerDraft({ action: "approve_document", documentId: doc.document_id }), "Approved.")
                  }
                  onApprove={(a) =>
                    void act(() => bridge.careerDraft({ action: "approve_submission", draftId: a.draft_id }), "Package approved. Nothing was submitted.")
                  }
                  onSubmit={(a) =>
                    void act(() => confirmable((confirmationId) => bridge.careerSubmit(a.draft_id, confirmationId)), "Submitted and verified.")
                  }
                  onWithdraw={(a) =>
                    void act(
                      () => confirmable((confirmationId) => bridge.careerDraft({ action: "withdraw", draftId: a.draft_id, confirmationId })),
                      "Withdrawn locally.",
                    )
                  }
                />
              ))}
            </div>
          )
        ) : null}

        {data !== null && tab === "drafts" ? (
          data.outreach.length === 0 ? (
            <EmptyState title="No message drafts" body="Draft outreach from a contact in Contacts." />
          ) : (
            <div className="grid">
              {data.outreach.map((d) => (
                <OutreachCard
                  key={d.outreach_id}
                  draft={d}
                  busy={busy}
                  onApprove={(x) => void act(() => bridge.careerOutreach({ action: "approve", outreachId: x.outreach_id }), "Draft approved. Nothing was sent.")}
                  onSend={(x) => void act(() => confirmable((confirmationId) => bridge.careerSend(x.outreach_id, confirmationId)), "Sent.")}
                />
              ))}
            </div>
          )
        ) : null}

        {data !== null && tab === "contacts" ? (
          <>
            <form
              className="panel glass"
              aria-label="Add a contact"
              onSubmit={(event) => {
                event.preventDefault();
                void act(
                  () =>
                    bridge.careerContact({
                      name: contact.name,
                      role: contact.role,
                      organization: contact.organization,
                      sourceKind: "other",
                      url: contact.url.trim(),
                      quote: contact.quote,
                      email: contact.email.trim() || undefined,
                    }),
                  "Contact added with its source.",
                );
              }}
            >
              <h3>Add a contact from a source</h3>
              <p className="faint">Paste the exact text that names the person. An e-mail address is kept only if that text contains it; Sam never guesses one.</p>
              <label>
                Name <input className="text-input" value={contact.name} onChange={(e) => setContact({ ...contact, name: e.target.value })} />
              </label>
              <label>
                Role{" "}
                <select className="text-input" value={contact.role} onChange={(e) => setContact({ ...contact, role: e.target.value as CareerRole })}>
                  {["recruiter", "hiring_manager", "team_lead", "professor", "supervisor", "research_group"].map((r) => (
                    <option key={r} value={r}>
                      {humanize(r)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Organization <input className="text-input" value={contact.organization} onChange={(e) => setContact({ ...contact, organization: e.target.value })} />
              </label>
              <label>
                Source link <input className="text-input" value={contact.url} onChange={(e) => setContact({ ...contact, url: e.target.value })} />
              </label>
              <label>
                Quoted text <textarea className="text-input" rows={2} maxLength={500} value={contact.quote} onChange={(e) => setContact({ ...contact, quote: e.target.value })} />
              </label>
              <label>
                E-mail (only if in the quote) <input className="text-input" value={contact.email} onChange={(e) => setContact({ ...contact, email: e.target.value })} />
              </label>
              <NeonButton type="submit" disabled={busy || !contact.name.trim() || !contact.quote.trim()}>
                Add contact
              </NeonButton>
            </form>
            {data.contacts.length === 0 ? (
              <EmptyState title="No contacts" body="Contacts always keep the source that names them." />
            ) : (
              <div className="grid">
                {data.contacts.map((c) => (
                  <article className="card" key={c.contact_id} aria-label={`Contact ${c.name}`}>
                    <div className="card-row">
                      <h4>
                        {c.name} · {humanize(c.role)}
                      </h4>
                      <span className="faint">{c.organization}</span>
                    </div>
                    <p className="faint">
                      {c.email ?? "No e-mail in the source"} · Source: {c.source_url}
                    </p>
                    <div className="row">
                      {(["email", "linkedin_connection_note"] as const).map((channel) => (
                        <NeonButton
                          key={channel}
                          variant="quiet"
                          disabled={busy}
                          onClick={() =>
                            void act(
                              () =>
                                bridge.careerOutreach({
                                  action: "create",
                                  contactId: c.contact_id,
                                  kind: c.role === "supervisor" || c.role === "professor" ? "supervisor" : "recruiter",
                                  channel,
                                  opportunityId: c.opportunity_id ?? undefined,
                                }),
                              "Draft created. Nothing was sent.",
                            )
                          }
                        >
                          Draft {humanize(channel).toLowerCase()} to {c.name}
                        </NeonButton>
                      ))}
                    </div>
                  </article>
                ))}
              </div>
            )}
          </>
        ) : null}

        {data !== null && tab === "followups" ? (
          data.follow_ups.length === 0 ? (
            <EmptyState title="No follow-ups" body="Follow-ups start after a verified submission or a sent message." />
          ) : (
            <ul className="evidence-list" aria-label="Follow-ups">
              {data.follow_ups.map((f) => (
                <li key={f.follow_up_id} className="card-row">
                  <span>
                    {titles.get(f.opportunity_id) ?? f.opportunity_id} · {humanize(f.state)} · due {f.due_at}
                  </span>
                  {f.state === "follow_up_due" ? (
                    <NeonButton
                      variant="quiet"
                      disabled={busy}
                      onClick={() => void act(() => bridge.careerOutreach({ action: "follow_up", followUpId: f.follow_up_id }), "Follow-up drafted. Nothing was sent.")}
                    >
                      Draft follow-up
                    </NeonButton>
                  ) : null}
                </li>
              ))}
            </ul>
          )
        ) : null}
      </div>
    </div>
  );
}
