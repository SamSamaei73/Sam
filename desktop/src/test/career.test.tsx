import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CareerApplication, CareerOpportunity, CareerOverview, CareerOutreach, Challenge } from "../bridge/types";
import { emptyCareer, mockBridge, ok, renderApp } from "./helpers";

const job: CareerOpportunity = {
  opportunity_id: "op_1",
  type: "job",
  title: "Senior Machine Learning Engineer",
  organization: "Nimbus Robotics",
  location: "London, UK",
  work_mode: "hybrid",
  source: "official_career_page",
  canonical_url: "https://careers.nimbus-robotics.example/jobs/ml-123",
  source_count: 2,
  deadline: "2026-10-30",
  status: "active",
  compensation: null,
  funding: null,
  sponsorship: "unknown",
  requirements: [{ text: "Experience with FastAPI", preferred: false }],
  research_topics: [],
  tracked: true,
  has_official_application_url: true,
  last_verified: "2026-09-21T12:00:00Z",
};

const application: CareerApplication = {
  draft_id: "ap_1",
  opportunity_id: "op_1",
  state: "approved_for_submission",
  version: 2,
  documents: [
    {
      document_id: "cv-1",
      kind: "cv",
      version: 1,
      approved: true,
      sha256: "a".repeat(64),
      unresolved: 0,
      lines: [{ text: "Data Scientist, Globex Health (2022-07 to present)", fact: true, evidence_count: 1, flagged: false, flag_reason: null }],
    },
    {
      document_id: "letter-1",
      kind: "cover_letter",
      version: 2,
      approved: false,
      sha256: "b".repeat(64),
      unresolved: 1,
      lines: [
        { text: "I have 12 years of Rust <script>x</script>", fact: false, evidence_count: 0, flagged: true, flag_reason: "no_evidence_for_claim" },
      ],
    },
  ],
  questions: [
    {
      question_id: "q1",
      text: "What are your salary expectations?",
      kind: "salary",
      classification: "owner_review_required",
      answered: false,
      answer: null,
      answer_source: null,
    },
  ],
  last_failure: null,
  submitted_at: null,
  approved: true,
};

const linkedin: CareerOutreach = {
  outreach_id: "or_1",
  kind: "recruiter",
  channel: "linkedin_connection_note",
  contact_id: "ct_1",
  opportunity_id: "op_1",
  subject: "",
  state: "approved",
  lines: [{ text: "Dear Dana Recruiter,", fact: false, evidence_count: 0, flagged: false, flag_reason: null }],
  unresolved: 0,
  sendable: false,
};

const populated: CareerOverview = {
  ...emptyCareer,
  opportunities: [job],
  applications: [application],
  outreach: [linkedin],
  review_queue: [
    { kind: "needs_answer", label: "Needs your answer: salary (Senior Machine Learning Engineer)", opportunity_id: "op_1", item_id: "ap_1" },
    { kind: "deadline_soon", label: "Deadline: Senior Machine Learning Engineer closes in 3 day(s)", opportunity_id: "op_1", item_id: "ap_1" },
  ],
};

async function openCareer(bridge: ReturnType<typeof mockBridge>) {
  renderApp(bridge);
  const user = userEvent.setup();
  await screen.findByLabelText("Connection: Connected");
  await user.click(await screen.findByRole("button", { name: "Career" }));
  await screen.findByRole("heading", { name: "Career" });
  return user;
}

describe("Career view", () => {
  it("is honest when empty and says it is review first", async () => {
    await openCareer(mockBridge());
    expect(await screen.findByText("Nothing needs you")).toBeInTheDocument();
    expect(screen.getByText(/never submits or sends without your confirmation/)).toBeInTheDocument();
    expect(screen.getByText(/can't submit applications or send e-mail yet/)).toBeInTheDocument();
  });

  it("shows the review queue with consequential items spelled out", async () => {
    await openCareer(mockBridge({ careerOverview: vi.fn(async () => populated) }));
    const queue = await screen.findByRole("list", { name: "Review queue" });
    expect(within(queue).getByText(/Needs your answer: salary/)).toBeInTheDocument();
    expect(within(queue).getByText(/closes in 3 day/)).toBeInTheDocument();
  });

  it("shows unknown values as unknown, never invented", async () => {
    const user = await openCareer(mockBridge({ careerOverview: vi.fn(async () => populated) }));
    await user.click(screen.getByRole("tab", { name: "Opportunities" }));
    const card = await screen.findByLabelText("Opportunity Senior Machine Learning Engineer");
    expect(within(card).getByText(/Salary: not stated/)).toBeInTheDocument();
    expect(within(card).getByText(/Sponsorship: Unknown/)).toBeInTheDocument();
  });

  it("an evidence check lists per-requirement evidence and gaps, not a score", async () => {
    const fit = vi.fn(async () => ({
      ...ok,
      requirements: [
        { requirement: "Experience with FastAPI", preferred: false, status: "supported", claims: [{ claim_id: "c1", category: "technology", statement: "Technology: FastAPI", evidence_count: 1 }], unsupported: [] },
        { requirement: "10 years of Rust", preferred: false, status: "not_supported", claims: [], unsupported: ["years"] },
      ],
      alignment_status: null,
      alignment: [],
    }));
    const user = await openCareer(mockBridge({ careerOverview: vi.fn(async () => populated), careerFit: fit }));
    await user.click(screen.getByRole("tab", { name: "Opportunities" }));
    await user.click(await screen.findByRole("button", { name: /Check evidence for/ }));
    const panel = await screen.findByLabelText("Evidence check");
    expect(within(panel).getByText("Not supported")).toBeInTheDocument();
    expect(within(panel).getByText(/No evidence in your profile/)).toBeInTheDocument();
    expect(within(panel).getByText(/There is no overall score/)).toBeInTheDocument();
    expect(within(panel).queryByText(/\d+\s*(%|\/\s*100)/)).toBeNull();
  });

  it("flags unsupported lines, marks sensitive questions and renders draft text as text", async () => {
    const user = await openCareer(mockBridge({ careerOverview: vi.fn(async () => populated) }));
    await user.click(screen.getByRole("tab", { name: "Applications" }));
    const card = await screen.findByLabelText(/Application Senior Machine Learning Engineer/);
    expect(within(card).getByText("Only you can answer this")).toBeInTheDocument();
    await user.click(within(card).getByText(/Cover letter v2/));
    expect(within(card).getByText(/Needs review: No evidence for claim/)).toBeInTheDocument();
    expect(card.querySelector("script")).toBeNull();
    expect(within(card).getByRole("button", { name: "Approve cover letter" })).toBeDisabled();
  });

  it("submitting always goes through the confirmation dialog", async () => {
    const challenge: Challenge = {
      confirmation_id: "conf-5",
      action: "submit",
      resource: "career",
      scope: "career/opportunities/op_1/drafts/ap_1/v2",
      risk: "high",
      target: "binding:abc",
      reason: null,
      expires_at: "2026-09-21T12:05:00Z",
    };
    const submit = vi.fn(async (_id: string, confirmationId?: string) =>
      confirmationId
        ? { ...ok, item_id: "ap_1", state: "submitted" }
        : { ...ok, status: "confirmation_required" as const, reason_code: "confirmation_required", challenge, item_id: "ap_1", state: null },
    );
    const user = await openCareer(mockBridge({ careerOverview: vi.fn(async () => populated), careerSubmit: submit }));
    await user.click(screen.getByRole("tab", { name: "Applications" }));
    await user.click(await screen.findByRole("button", { name: /Submit \(asks for your confirmation\)/ }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("High risk")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    expect(submit).toHaveBeenLastCalledWith("ap_1", "conf-5");
  });

  it("an unknown submission outcome is surfaced and offers no retry", async () => {
    const unknown: CareerOverview = {
      ...populated,
      applications: [{ ...application, state: "outcome_unknown", last_failure: "adapter_error", approved: false }],
    };
    const submit = vi.fn();
    const user = await openCareer(mockBridge({ careerOverview: vi.fn(async () => unknown), careerSubmit: submit }));
    await user.click(screen.getByRole("tab", { name: "Applications" }));
    const card = await screen.findByLabelText(/Application Senior Machine Learning Engineer/);
    expect(within(card).getByRole("alert")).toHaveTextContent(/will not retry on its own/);
    expect(within(card).queryByRole("button", { name: /Submit/ })).toBeNull();
    expect(within(card).queryByRole("button", { name: "Withdraw" })).toBeNull();
    expect(submit).not.toHaveBeenCalled();
  });

  it("career links are shown as text, never as links the app could open", async () => {
    const { readFileSync } = await import("node:fs");
    const source = readFileSync("src/views/CareerView.tsx", "utf8");
    for (const banned of ["href", "<a ", "window.open", "location.assign", "location.href", "@tauri-apps/plugin"]) {
      expect(source).not.toContain(banned);
    }
  });

  it("a LinkedIn draft offers no send button", async () => {
    const send = vi.fn(async () => ({ ...ok, item_id: "or_1", state: null }));
    const user = await openCareer(mockBridge({ careerOverview: vi.fn(async () => populated), careerSend: send }));
    await user.click(screen.getByRole("tab", { name: "Drafts" }));
    await screen.findByText(/Draft only: copy it and send it yourself/);
    expect(screen.queryByRole("button", { name: /^Send/ })).toBeNull();
    expect(send).not.toHaveBeenCalled();
  });

  it("shows a guest-mode refusal as a message, not data", async () => {
    const refused = vi.fn(async () => {
      const { BridgeError } = await import("../bridge/bridge");
      throw new BridgeError("guest_mode_active", "Guest Mode is active. End Guest Mode to use this.");
    });
    await openCareer(mockBridge({ careerOverview: refused }));
    expect(await screen.findByText(/Guest Mode is active/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^Application /)).toBeNull();
  });
});
