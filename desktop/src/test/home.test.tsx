import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CareerOverview } from "../bridge/types";
import { baseIdentity, baseStatus, emptyCareer, mockBridge, ok, renderApp } from "./helpers";

const presence = () => document.querySelector(".presence") as HTMLElement;

const reviewQueue = {
  ...emptyCareer,
  review_queue: [{ item_id: "ap_1" }],
} as unknown as CareerOverview;

describe("Action Required", () => {
  it("is hidden when nothing needs the owner", async () => {
    renderApp(mockBridge());
    await screen.findByLabelText("Connection: Connected");
    expect(screen.queryByRole("button", { name: /Action required/ })).toBeNull();
    expect(presence().dataset.attention).toBe("false");
    await userEvent.setup().click(screen.getByRole("button", { name: "Show system panel" }));
    expect(screen.getByText("Nothing needs you right now.")).toBeInTheDocument();
  });

  it("surfaces a real review item compactly and links to where it is resolved", async () => {
    const bridge = mockBridge({ careerOverview: vi.fn(async () => reviewQueue) });
    renderApp(bridge);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Action required: 1" }));
    expect(presence().dataset.attention).toBe("true");
    const panel = screen.getByRole("complementary", { name: "System panel" });
    const list = within(panel).getByRole("region", { name: "Action required" });
    await user.click(within(list).getByRole("button", { name: "1 career item(s) need your review" }));
    // Navigating only: nothing was approved, submitted or sent.
    expect(bridge.careerSubmit).not.toHaveBeenCalled();
    expect(bridge.careerSend).not.toHaveBeenCalled();
    expect(bridge.decideConfirmation).not.toHaveBeenCalled();
    expect(screen.getByRole("main", { name: "Career" })).toBeInTheDocument();
  });

  it("does not read the owner's career or automations while a guest is using Sam", async () => {
    const bridge = mockBridge({
      identityStatus: vi.fn(async () => ({
        ...baseIdentity,
        available: true,
        enrolled: true,
        guest: { active: true, seconds_remaining: 300 },
      })),
      careerOverview: vi.fn(async () => reviewQueue),
    });
    renderApp(bridge);
    await waitFor(() => expect(presence().dataset.guest).toBe("true"));
    await userEvent.setup().click(screen.getByRole("button", { name: "Show system panel" }));
    const identity = screen.getByRole("region", { name: "Voice identity" });
    expect(within(identity).getByText("Guest Mode")).toBeInTheDocument();
    expect(bridge.careerOverview).not.toHaveBeenCalled();
    expect(bridge.proactiveOverview).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /Action required/ })).toBeNull();
  });

  it("shows identity as a label only, never a score", async () => {
    renderApp(
      mockBridge({
        identityStatus: vi.fn(async () => ({
          ...baseIdentity,
          available: true,
          enrolled: true,
          last_verification: "verified" as const,
        })),
      }),
    );
    await screen.findByLabelText("Connection: Connected");
    await userEvent.setup().click(screen.getByRole("button", { name: "Show system panel" }));
    const identity = await screen.findByRole("region", { name: "Voice identity" });
    await within(identity).findByText("Owner recognized");
    expect(identity.textContent).not.toMatch(/\d/);
  });
});

describe("permission state", () => {
  it("the presence waits while a confirmation is open and never auto-confirms", async () => {
    const bridge = mockBridge({
      status: vi.fn(async () => ({
        ...baseStatus,
        speech_output: "configured" as const,
        speech_profiles: [{ profile_id: "sam_default", languages: ["en" as const] }],
      })),
      speak: vi.fn(async () => ({
        ...ok,
        status: "confirmation_required" as const,
        challenge: {
          confirmation_id: "c-speak",
          action: "speak",
          resource: "speech",
          scope: "default",
          risk: "medium" as const,
          target: null,
          reason: null,
          expires_at: "2026-01-01T00:05:00Z",
        },
        audio_base64: null,
        audio_format: null,
        byte_length: null,
      })),
      decideConfirmation: vi.fn(async (id: string) => ({ status: "denied" as const, confirmation_id: id })),
    });
    renderApp(bridge);
    const user = userEvent.setup();
    await screen.findByLabelText("Connection: Connected");
    await user.click(screen.getByRole("button", { name: "Type to Sam" }));
    await user.type(screen.getByLabelText("Message Sam"), "hi");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await user.click(await screen.findByRole("button", { name: "Speak reply" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(screen.getByRole("img", { name: "Sam is waiting for your permission" })).toBeInTheDocument();
    expect(bridge.decideConfirmation).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(presence().dataset.state).not.toBe("permission"));
    expect(bridge.decideConfirmation.mock.calls[0]?.slice(0, 2)).toEqual(["c-speak", false]);
    expect(bridge.speak).toHaveBeenCalledTimes(1);
  });
});

describe("loading", () => {
  it("uses one centred loader for a loading view", async () => {
    renderApp(mockBridge({ knowledgeList: vi.fn(() => new Promise(() => undefined)) as never }));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Knowledge" }));
    const loader = await screen.findByRole("status", { name: "" });
    expect(document.querySelectorAll(".loader-wrap")).toHaveLength(1);
    expect(loader).toHaveClass("loader-wrap");
    expect(loader).toHaveTextContent("Loading…");
    expect(within(loader).queryByRole("button")).toBeNull();
  });
});

describe("build identity", () => {
  it("is in Settings › About (not on Home) and names the shell build", async () => {
    renderApp(mockBridge({ status: vi.fn(async () => ({ ...baseStatus, desktop_build: "release" as const })) }));
    await screen.findByLabelText("Connection: Connected");
    expect(screen.queryByText("About Sam")).toBeNull();
    await userEvent.setup().click(screen.getByRole("button", { name: "Settings" }));
    const about = screen.getByRole("region", { name: "About Sam" });
    expect(within(about).getByText("0.11.0")).toBeInTheDocument();
    expect(within(about).getByText("Release (starts its own backend)")).toBeInTheDocument();
    expect(within(about).getByText(/^\d{4}-\d{2}-\d{2}T/)).toBeInTheDocument();
  });

  it("says plainly when a development shell is running", async () => {
    renderApp(mockBridge({ status: vi.fn(async () => ({ ...baseStatus, desktop_build: "development" as const })) }));
    await screen.findByLabelText("Connection: Connected");
    await userEvent.setup().click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.getByText(/Development build/)).toBeInTheDocument();
  });
});
