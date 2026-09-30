import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Challenge, ProClaim, ProEvidence, ProfessionalProfile } from "../bridge/types";
import { emptyIngest, emptyProfessional, mockBridge, ok, renderApp } from "./helpers";

const evidence = (over: Partial<ProEvidence> = {}): ProEvidence => ({
  evidence_id: "e1",
  source_id: "s_1",
  source_label: "cv.txt",
  source_type: "master_cv",
  source_family_id: "f_cv",
  nature: "explicit_source",
  page_number: null,
  section_title: "Skills",
  paragraph_index: null,
  character_start: 10,
  character_end: 40,
  reference: "Python, TypeScript <b>bold</b>",
  context_claim_id: null,
  context_statement: null,
  basis_evidence_id: null,
  ...over,
});

const claim = (over: Partial<ProClaim> = {}): ProClaim => ({
  claim_id: "c_python",
  category: "technology",
  statement: "Technology: Python",
  strength: "single_source",
  review: "unreviewed",
  accepted: true,
  inferred: false,
  candidate: false,
  sensitivity: "personal",
  attributes: { skill_id: "python", display: "Python" },
  conflicted_attributes: [],
  source_count: 1,
  source_family_count: 1,
  canonical: true,
  evidence: [evidence()],
  ...over,
});

const source = {
  source_id: "s_1",
  label: "cv.txt",
  source_type: "master_cv" as const,
  source_family_id: "f_cv",
  version: 1,
  privacy_class: "personal" as const,
  freshness: "fresh" as const,
  ingested_at: "2026-09-21T12:00:00Z",
  refreshed_at: "2026-09-21T12:00:00Z",
  resource_type: "txt",
  accepted_claims: 2,
  pending_candidates: 1,
  conflicts: 0,
  candidate_extraction: "off",
};

const populated: ProfessionalProfile = {
  ...emptyProfessional,
  claims: [
    claim(),
    claim({
      claim_id: "c_kube",
      statement: "Technology: Kubernetes",
      strength: "none",
      accepted: false,
      inferred: true,
      source_family_count: 0,
      attributes: { skill_id: "kubernetes", display: "Kubernetes" },
      evidence: [
        evidence({ evidence_id: "e2", nature: "inferred_relationship", source_label: "deploy.yaml", source_type: "github" }),
      ],
    }),
    claim({
      claim_id: "c_zorb",
      statement: "Skill (unmapped): Zorbnetics",
      category: "skill",
      strength: "none",
      accepted: false,
      candidate: true,
      source_family_count: 0,
      canonical: false,
      attributes: {},
      evidence: [evidence({ evidence_id: "e3", nature: "model_candidate" })],
    }),
    claim({
      claim_id: "c_rust",
      statement: "Technology: Rust",
      strength: "none",
      accepted: false,
      candidate: true,
      source_family_count: 0,
      attributes: { skill_id: "rust", display: "Rust" },
      evidence: [evidence({ evidence_id: "e4", nature: "model_candidate" })],
    }),
    claim({
      claim_id: "c_acme",
      category: "employment",
      statement: "Senior Software Engineer at Acme Analytics",
      strength: "corroborated",
      source_count: 2,
      source_family_count: 2,
      conflicted_attributes: ["start"],
      attributes: { employer: "Acme Analytics", title: "Senior Software Engineer" },
    }),
  ],
  publications: [
    {
      claim_id: "c_pub",
      strength: "corroborated",
      review: "unreviewed",
      accepted: true,
      sensitivity: "personal",
      title: "Semantic Embeddings for Health Misinformation Detection",
      authors: ["Jordan Example", "Alex Sample"],
      owner_author_position: 1,
      venue: "Journal of Example AI",
      year: "2023",
      doi: "10.1234/example.2023.001",
      abstract: null,
      methods: null,
      models: [],
      datasets: null,
      findings: null,
      limitations: null,
      topics: ["graph neural networks"],
      owner_contribution: null,
      evidence: [evidence({ source_type: "publication", source_label: "paper.txt" })],
      conflicted_attributes: [],
    },
  ],
  education: [
    {
      claim_id: "c_edu",
      strength: "corroborated",
      review: "unreviewed",
      accepted: true,
      sensitivity: "personal",
      institution: "Example University",
      degree: "MSc",
      subject: "Artificial Intelligence",
      classification: "Distinction",
      start: "2021",
      end: "2022",
      modules: ["Machine Learning"],
      dissertation: null,
      evidence: [evidence()],
      conflicted_attributes: [],
    },
  ],
  experience: {
    entries: [
      {
        claim_id: "c_acme",
        employer: "Acme Analytics",
        title: "Senior Software Engineer",
        start: "2019-03",
        end: "2022-06",
        status: "resolved",
        months: 40,
        approximate: false,
      },
    ],
    total_months: 91,
    total_years: 7,
    remainder_months: 7,
    conservative_months: 91,
    excluded_conflicted: 1,
    excluded_incomplete: 0,
    gaps: [],
  },
  conflicts: [
    {
      conflict_id: "x_1",
      kind: "attribute",
      attribute: "start",
      claim_ids: ["c_acme"],
      options: [
        { option_id: "o_1", value: "2019-03", claim_id: null, source_ids: ["s_1"] },
        { option_id: "o_2", value: "2019-04", claim_id: null, source_ids: ["s_2"] },
      ],
      resolved: false,
      resolved_value: null,
    },
  ],
  gaps: [{ kind: "skill_only_in_cv", detail: "Technology: Python: stated in the CV only, no other source", claim_id: "c_python", source_id: null }],
  sources: [source],
  counts: { technology: 3, employment: 1, skill: 1 },
};

async function openProfessional(bridge: ReturnType<typeof mockBridge>) {
  renderApp(bridge);
  const user = userEvent.setup();
  await screen.findByLabelText("Connection: Connected");
  await user.click(await screen.findByRole("button", { name: "Professional" }));
  await screen.findByRole("heading", { name: "Professional" });
  return user;
}

describe("Professional view", () => {
  it("is honest when empty: no prepopulated claims", async () => {
    await openProfessional(mockBridge());
    expect(await screen.findByText("No professional sources yet")).toBeInTheDocument();
    expect(screen.getByText(/never from memory or from what it is told/i)).toBeInTheDocument();
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual([
      "Overview",
      "Skills",
      "Experience",
      "Projects",
      "Research",
      "Publications",
      "Education",
      "Evidence",
      "Profile gaps",
      "Sources",
    ]);
  });

  it("shows evidence strength and provenance for every skill, as plain text", async () => {
    const view = renderApp(mockBridge({ professionalProfile: vi.fn(async () => populated) }));
    const user = userEvent.setup();
    await screen.findByLabelText("Connection: Connected");
    await user.click(await screen.findByRole("button", { name: "Professional" }));
    await user.click(await screen.findByRole("tab", { name: "Skills" }));
    const python = await screen.findByLabelText("Claim Technology: Python");
    expect(within(python).getByText("Single source")).toBeInTheDocument();
    expect(within(python).getByText("Personal")).toBeInTheDocument();
    await user.click(within(python).getByText(/1 piece of evidence/));
    expect(within(python).getByText("cv.txt")).toBeInTheDocument();
    expect(within(python).getByText(/Section: Skills · Characters 10–40/)).toBeInTheDocument();
    // Untrusted document text is rendered as text, never as markup.
    expect(within(python).getByText("Python, TypeScript <b>bold</b>")).toBeInTheDocument();
    expect(view.container.querySelector("b")).toBeNull();
    const inferred = screen.getByLabelText("Claim Technology: Kubernetes");
    expect(within(inferred).getByText("Inferred")).toBeInTheDocument();
    expect(within(inferred).getByText(/never an explicit fact/i)).toBeInTheDocument();
  });

  it("shows your confirmation apart from documentary strength, never as a document", async () => {
    const attested = claim({
      claim_id: "c_mentor",
      category: "responsibility",
      statement: "Mentored a small team",
      strength: "none",
      review: "owner_confirmed",
      accepted: true,
      candidate: true,
      source_family_count: 0,
      attributes: {},
      evidence: [
        evidence({ evidence_id: "e5", nature: "model_candidate" }),
        evidence({ evidence_id: "e6", nature: "owner_attestation", basis_evidence_id: "e5" }),
      ],
    });
    const documented = claim({ review: "owner_confirmed" });
    const profile = { ...populated, claims: [documented, attested] };
    await openProfessional(mockBridge({ professionalProfile: vi.fn(async () => profile) }));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Skills" }));
    const python = await screen.findByLabelText("Claim Technology: Python");
    expect(within(python).getByText("Single source")).toBeInTheDocument();
    expect(within(python).getByText("Confirmed by you")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Experience" }));
    const mentor = await screen.findByLabelText("Claim Mentored a small team");
    expect(within(mentor).getByText("No documentary evidence")).toBeInTheDocument();
    expect(within(mentor).getByText("Confirmed by you")).toBeInTheDocument();
    expect(within(mentor).queryByText("Single source")).toBeNull();
    expect(within(mentor).queryByText("Corroborated")).toBeNull();
  });

  it("offers Confirm and Reject only for unverified/inferred items, and never a 'verify' action", async () => {
    const review = vi.fn(async () => ok);
    const bridge = mockBridge({ professionalProfile: vi.fn(async () => populated), professionalReview: review });
    const user = await openProfessional(bridge);
    await user.click(await screen.findByRole("tab", { name: "Skills" }));
    expect(screen.queryByRole("button", { name: /Confirm “Technology: Python”/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /verify/i })).toBeNull();
    await user.click(await screen.findByRole("button", { name: "Confirm “Technology: Rust”" }));
    expect(review).toHaveBeenCalledWith({ action: "confirm", claimId: "c_rust" });
    await user.click(screen.getByRole("button", { name: "Reject “Technology: Kubernetes”" }));
    expect(review).toHaveBeenCalledWith({ action: "reject", claimId: "c_kube" });
  });

  it("an unknown skill cannot be confirmed from the UI either", async () => {
    await openProfessional(mockBridge({ professionalProfile: vi.fn(async () => populated) }));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Skills" }));
    const unknown = screen.getByLabelText("Claim Skill (unmapped): Zorbnetics");
    expect(within(unknown).queryByRole("button", { name: /Confirm/ })).toBeNull();
    expect(within(unknown).getByText(/cannot be confirmed/i)).toBeInTheDocument();
    expect(within(unknown).getByRole("button", { name: /Reject/ })).toBeInTheDocument();
  });

  it("shows experience computed from resolved dates and what was excluded", async () => {
    await openProfessional(mockBridge({ professionalProfile: vi.fn(async () => populated) }));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Experience" }));
    expect(await screen.findByText(/Total from resolved dates/)).toHaveTextContent(/7 years 7 months/);
    const role = screen.getByLabelText("Role Senior Software Engineer at Acme Analytics");
    expect(within(role).getByText("40 months")).toBeInTheDocument();
    expect(within(role).getByText(/2019-03 to 2022-06/)).toBeInTheDocument();
  });

  it("shows publications without inventing a contribution, and education without identifiers", async () => {
    const view = renderApp(mockBridge({ professionalProfile: vi.fn(async () => populated) }));
    const user = userEvent.setup();
    await screen.findByLabelText("Connection: Connected");
    await user.click(await screen.findByRole("button", { name: "Professional" }));
    await user.click(await screen.findByRole("tab", { name: "Publications" }));
    const paper = await screen.findByLabelText(/Publication Semantic Embeddings/);
    expect(within(paper).getByText("You are author #1.")).toBeInTheDocument();
    expect(within(paper).getByText(/Authorship alone does not imply one/)).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Education" }));
    const degree = await screen.findByLabelText("Education MSc Artificial Intelligence");
    expect(within(degree).getByText(/Example University · Distinction/)).toBeInTheDocument();
    expect(view.container.textContent).not.toMatch(/student|account number/i);
  });

  it("lists conflicts, keeps both values, and records only the owner's choice", async () => {
    const review = vi.fn(async () => ok);
    const bridge = mockBridge({ professionalProfile: vi.fn(async () => populated), professionalReview: review });
    const user = await openProfessional(bridge);
    await user.click(await screen.findByRole("tab", { name: "Profile gaps" }));
    const conflict = await screen.findByLabelText("Conflict on start");
    expect(within(conflict).getByText(/Sam never picks the more favourable one/)).toBeInTheDocument();
    expect(within(conflict).getByRole("button", { name: "Use “2019-03”" })).toBeInTheDocument();
    await user.click(within(conflict).getByRole("button", { name: "Use “2019-04”" }));
    expect(review).toHaveBeenCalledWith({ action: "resolve", conflictId: "x_1", optionId: "o_2" });
    expect(screen.getByText(/These describe your evidence, not you/)).toBeInTheDocument();
    expect(screen.getByText(/stated in the CV only/)).toBeInTheDocument();
  });

  it("adds a source with the owner's chosen type and privacy, never a path", async () => {
    const ingest = vi.fn(async () => ({ ...emptyIngest, ingest_status: "ingested", claims_created: 5 }));
    const bridge = mockBridge({ professionalIngest: ingest });
    const user = await openProfessional(bridge);
    await user.selectOptions(screen.getByLabelText("Source type"), "publication");
    await user.selectOptions(screen.getByLabelText("Privacy"), "private");
    const file = new File(["Skills: Python"], "notes.txt", { type: "text/plain" });
    await user.upload(screen.getByLabelText("Choose a professional document"), file);
    await waitFor(() => expect(ingest).toHaveBeenCalledTimes(1));
    const call = (ingest.mock.calls as unknown as [Record<string, unknown>][])[0]![0];
    expect(call).toMatchObject({
      name: "notes.txt",
      sourceType: "publication",
      privacyClass: "private",
      resourceType: "txt",
      useCandidates: false,
    });
    expect(Object.keys(call).sort()).toEqual(
      ["contentBase64", "name", "privacyClass", "resourceType", "sourceType", "useCandidates"].sort(),
    );
    expect(await screen.findByText(/Added “notes.txt”. 5 new/)).toBeInTheDocument();
  });

  it("rejects unsupported and empty files before anything is sent", async () => {
    const ingest = vi.fn();
    renderApp(mockBridge({ professionalIngest: ingest }));
    const user = userEvent.setup({ applyAccept: false });
    await screen.findByLabelText("Connection: Connected");
    await user.click(await screen.findByRole("button", { name: "Professional" }));
    const input = await screen.findByLabelText("Choose a professional document");
    await user.upload(input, new File(["x"], "resume.docx"));
    expect(await screen.findByText(/file type isn't supported/i)).toBeInTheDocument();
    expect(ingest).not.toHaveBeenCalled();
  });

  it("makes model suggestions opt-in and says they stay unverified", async () => {
    const ingest = vi.fn(async () => ({ ...emptyIngest, ingest_status: "ingested", candidates_accepted: 2 }));
    const user = await openProfessional(mockBridge({ professionalIngest: ingest }));
    const box = screen.getByRole("checkbox", { name: /ask a model to suggest/i });
    expect(box).not.toBeChecked();
    expect(screen.getByText(/stay unverified until you confirm them/i)).toBeInTheDocument();
    await user.click(box);
    await user.upload(
      screen.getByLabelText("Choose a professional document"),
      new File(["Python"], "cv.txt", { type: "text/plain" }),
    );
    await waitFor(() => expect(ingest).toHaveBeenCalledTimes(1));
    expect(ingest).toHaveBeenCalledWith(expect.objectContaining({ useCandidates: true }));
    expect(await screen.findByText(/2 model suggestion\(s\) await your review \(unverified\)/)).toBeInTheDocument();
  });

  it("shows a backend refusal, such as a secret, without exposing content", async () => {
    const ingest = vi.fn(async () => ({
      ...emptyIngest,
      status: "rejected" as const,
      reason_code: "secret_detected",
      message: "That looked like it contained a secret, so it was withheld.",
    }));
    const user = await openProfessional(mockBridge({ professionalIngest: ingest }));
    await user.upload(
      screen.getByLabelText("Choose a professional document"),
      new File(["password: hunter2hunter2"], "cv.txt", { type: "text/plain" }),
    );
    expect(await screen.findByText(/looked like it contained a secret/)).toBeInTheDocument();
    expect(screen.queryByText(/hunter2/)).toBeNull();
  });

  it("removing a source asks for confirmation and says other sources keep their evidence", async () => {
    const challenge: Challenge = {
      confirmation_id: "conf-9",
      action: "delete",
      resource: "professional",
      scope: "profile/s_1",
      risk: "high",
      target: null,
      reason: null,
      expires_at: "2026-09-21T12:05:00Z",
    };
    const remove = vi.fn(async (_id: string, confirmationId?: string) =>
      confirmationId
        ? ok
        : { ...ok, status: "confirmation_required" as const, reason_code: "confirmation_required", challenge },
    );
    const bridge = mockBridge({
      professionalProfile: vi.fn(async () => populated),
      professionalRemove: remove,
    });
    const user = await openProfessional(bridge);
    await user.click(await screen.findByRole("tab", { name: "Sources" }));
    await user.click(await screen.findByRole("button", { name: "Remove cv.txt" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("High risk")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(remove).toHaveBeenCalledTimes(2));
    expect(remove).toHaveBeenLastCalledWith("s_1", "conf-9");
    expect(await screen.findByText(/only its own evidence/)).toBeInTheDocument();
  });

  it("lists sources with freshness, counts and a privacy control", async () => {
    const review = vi.fn(async () => ok);
    const bridge = mockBridge({ professionalProfile: vi.fn(async () => populated), professionalReview: review });
    const user = await openProfessional(bridge);
    await user.click(await screen.findByRole("tab", { name: "Sources" }));
    const card = await screen.findByLabelText("Source cv.txt");
    expect(within(card).getByText("Fresh")).toBeInTheDocument();
    expect(within(card).getByText(/2 accepted item\(s\) · 1 awaiting review/)).toBeInTheDocument();
    await user.selectOptions(within(card).getByLabelText("Privacy for cv.txt"), "private");
    expect(review).toHaveBeenCalledWith({ action: "set_privacy", sourceId: "s_1", privacyClass: "private" });
    expect(within(card).queryByText(/password|token|api key/i)).toBeNull();
  });

  it("checks a requirement against the evidence and shows what is unsupported", async () => {
    const query = vi.fn(async (mode: "search" | "evidence_for") => ({
      ...ok,
      mode,
      claims: [],
      requirement: {
        status: "partially_supported" as const,
        normalized_requirement: "Retrieval-Augmented Generation; production use",
        skills: [claim({ claim_id: "c_rag", statement: "Skill: Retrieval-Augmented Generation" })],
        inferred_skills: [],
        projects: [],
        employment: [],
        research: [],
        education: [],
        unsupported_aspects: ["production use: no employment evidence"],
        notes: [],
      },
    }));
    const bridge = mockBridge({ professionalProfile: vi.fn(async () => populated), professionalQuery: query });
    const user = await openProfessional(bridge);
    await user.type(await screen.findByLabelText("Requirement to check"), "production RAG");
    await user.click(screen.getByRole("button", { name: "Check evidence" }));
    const result = await screen.findByLabelText("Requirement result");
    expect(within(result).getByText("Partially supported")).toBeInTheDocument();
    expect(within(result).getByText(/no employment evidence/)).toBeInTheDocument();
    expect(query).toHaveBeenCalledWith("evidence_for", "production RAG");
  });

  it("explains every evidence state on the Evidence tab and searches only text", async () => {
    const query = vi.fn(async (mode: "search" | "evidence_for") => ({
      ...ok,
      mode,
      claims: [claim()],
      requirement: null,
    }));
    const bridge = mockBridge({ professionalProfile: vi.fn(async () => populated), professionalQuery: query });
    const user = await openProfessional(bridge);
    await user.click(await screen.findByRole("tab", { name: "Evidence" }));
    for (const label of [
      "Corroborated",
      "Single source",
      "No documentary evidence",
      "Inferred",
      "Unverified",
      "Confirmed by you",
    ]) {
      expect(screen.getByText(label, { selector: "strong" })).toBeInTheDocument();
    }
    await user.type(screen.getByLabelText("Search your evidence"), "/etc/passwd; DROP TABLE");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(query).toHaveBeenCalledWith("search", "/etc/passwd; DROP TABLE");
  });

  it("shows a guest-mode refusal as a message, not data", async () => {
    const refused = vi.fn(async () => {
      const { BridgeError } = await import("../bridge/bridge");
      throw new BridgeError("guest_mode_active", "Guest Mode is active. End Guest Mode to use this.");
    });
    await openProfessional(mockBridge({ professionalProfile: refused }));
    expect(await screen.findByText(/Guest Mode is active/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Claim /)).toBeNull();
  });
});
