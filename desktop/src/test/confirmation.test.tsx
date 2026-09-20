import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { BridgeError } from "../bridge/bridge";
import type { Challenge, ResourceInfo } from "../bridge/types";
import { mockBridge, ok, renderApp } from "./helpers";

const doc: ResourceInfo = {
  resource_id: "r1",
  name: "Board <b>notes</b>.txt",
  resource_type: "txt",
  size_bytes: 1200,
  chunk_count: 2,
  created_at: "2026-01-01T00:00:00Z",
};

function challenge(risk: Challenge["risk"]): Challenge {
  return {
    confirmation_id: "conf-1",
    action: "delete",
    resource: "knowledge",
    scope: "default",
    risk,
    target: "Board <b>notes</b>.txt",
    reason: "Removing can't be undone.",
    expires_at: "2026-01-01T00:05:00Z",
  };
}

function setup(risk: Challenge["risk"] = "high") {
  const remove = vi.fn(async (_id: string, confirmationId?: string) =>
    confirmationId
      ? ok
      : {
          ...ok,
          status: "confirmation_required" as const,
          message: "This needs your confirmation.",
          challenge: challenge(risk),
        },
  );
  const bridge = mockBridge({
    knowledgeList: vi.fn(async () => ({ ...ok, resources: [doc] })),
    knowledgeRemove: remove,
  });
  renderApp(bridge);
  return { bridge, remove };
}

async function openDialog() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Knowledge" }));
  await user.click(await screen.findByRole("button", { name: `Remove ${doc.name}` }));
  const dialog = await screen.findByRole("alertdialog");
  return { user, dialog };
}

describe("confirmation dialog", () => {
  it("shows the backend's challenge as plain text with a risk label", async () => {
    setup("high");
    const { dialog } = await openDialog();
    expect(within(dialog).getByText("High risk")).toBeInTheDocument();
    expect(within(dialog).getByText("Board <b>notes</b>.txt")).toBeInTheDocument();
    expect(dialog.querySelector("b")).toBeNull();
    expect(within(dialog).getByText("default")).toBeInTheDocument();
  });

  it("focuses Deny first and denies on Escape without retrying", async () => {
    const { bridge, remove } = setup();
    const { user, dialog } = await openDialog();
    expect(within(dialog).getByRole("button", { name: "Deny" })).toHaveFocus();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(bridge.decideConfirmation).toHaveBeenCalledWith("conf-1", false, undefined));
    expect(remove).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(await screen.findByText(/nothing was changed/i)).toBeInTheDocument();
  });

  it("traps focus inside the dialog", async () => {
    setup();
    const { user, dialog } = await openDialog();
    const approve = within(dialog).getByRole("button", { name: "Approve" });
    const deny = within(dialog).getByRole("button", { name: "Deny" });
    await user.tab();
    expect(approve).toHaveFocus();
    await user.tab();
    expect(deny).toHaveFocus();
    await user.tab({ shift: true });
    expect(approve).toHaveFocus();
  });

  it("approve records the decision and then retries with the confirmation id only", async () => {
    const { bridge, remove } = setup();
    const { user, dialog } = await openDialog();
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(remove).toHaveBeenCalledTimes(2));
    expect(bridge.decideConfirmation).toHaveBeenCalledWith("conf-1", true, undefined);
    expect(remove).toHaveBeenLastCalledWith("r1", "conf-1");
  });

  it("does not treat approval as success: a backend denial on retry is shown", async () => {
    const remove = vi.fn(async (_id: string, confirmationId?: string) =>
      confirmationId
        ? { ...ok, status: "denied" as const, message: "Sam isn't permitted to do that." }
        : { ...ok, status: "confirmation_required" as const, challenge: challenge("high") },
    );
    renderApp(
      mockBridge({ knowledgeList: vi.fn(async () => ({ ...ok, resources: [doc] })), knowledgeRemove: remove }),
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Knowledge" }));
    await user.click(await screen.findByRole("button", { name: `Remove ${doc.name}` }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("Sam isn't permitted to do that.")).toBeInTheDocument();
    expect(screen.queryByText(/Removed/)).toBeNull();
  });

  it("requires a typed step-up secret (not a checkbox) to approve a CRITICAL action", async () => {
    const { bridge } = setup("critical");
    const { user, dialog } = await openDialog();
    const approve = within(dialog).getByRole("button", { name: "Approve" });
    expect(within(dialog).getByText("Critical risk")).toBeInTheDocument();
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
    expect(approve).toBeDisabled();
    const secret = within(dialog).getByLabelText(/step-up secret/i);
    expect(secret).toHaveAttribute("type", "password");
    expect(secret).toHaveAttribute("autocomplete", "off");
    await user.type(secret, "typed-step-up-secret");
    expect(approve).toBeEnabled();
    await user.click(approve);
    await waitFor(() =>
      expect(bridge.decideConfirmation).toHaveBeenCalledWith("conf-1", true, "typed-step-up-secret"),
    );
    // After submit the dialog (and the field holding the secret) is gone, and
    // the secret is neither in the DOM nor persisted anywhere in the browser.
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(document.body.innerHTML).not.toContain("typed-step-up-secret");
    expect(document.querySelector("input[type=password]")).toBeNull();
    expect(JSON.stringify({ ...window.localStorage })).not.toContain("typed-step-up");
    expect(JSON.stringify({ ...window.sessionStorage })).not.toContain("typed-step-up");
    expect(document.cookie).not.toContain("typed-step-up");
  });

  it("re-asks after a wrong step-up secret and does not run the operation", async () => {
    const decide = vi
      .fn()
      .mockRejectedValueOnce(new BridgeError("step_up_failed", "That step-up secret wasn't accepted."))
      .mockResolvedValue({ status: "approved", confirmation_id: "conf-1" });
    const remove = vi.fn(async (_id: string, cid?: string) =>
      cid ? ok : { ...ok, status: "confirmation_required" as const, challenge: challenge("critical") },
    );
    renderApp(
      mockBridge({
        knowledgeList: vi.fn(async () => ({ ...ok, resources: [doc] })),
        knowledgeRemove: remove,
        decideConfirmation: decide,
      }),
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Knowledge" }));
    await user.click(await screen.findByRole("button", { name: `Remove ${doc.name}` }));
    let dialog = await screen.findByRole("alertdialog");
    await user.type(within(dialog).getByLabelText(/step-up secret/i), "wrong-secret-1234");
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("That step-up secret wasn't accepted.")).toBeInTheDocument();
    expect(remove).toHaveBeenCalledTimes(1);
    dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByLabelText(/step-up secret/i)).toHaveValue("");
    await user.type(within(dialog).getByLabelText(/step-up secret/i), "right-secret-5678");
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(remove).toHaveBeenCalledTimes(2));
  });

  it.each([
    ["step_up_unavailable", "Critical actions can't be approved from this window. Nothing was changed."],
    ["step_up_locked", "Too many wrong attempts. The request was denied."],
  ])("%s ends as a denial without retrying the operation", async (code, message) => {
    const remove = vi.fn(async () => ({
      ...ok,
      status: "confirmation_required" as const,
      challenge: challenge("critical"),
    }));
    renderApp(
      mockBridge({
        knowledgeList: vi.fn(async () => ({ ...ok, resources: [doc] })),
        knowledgeRemove: remove,
        decideConfirmation: vi.fn(async () => Promise.reject(new BridgeError(code, message))),
      }),
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Knowledge" }));
    await user.click(await screen.findByRole("button", { name: `Remove ${doc.name}` }));
    const dialog = await screen.findByRole("alertdialog");
    await user.type(within(dialog).getByLabelText(/step-up secret/i), "some-secret-value");
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(remove).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("offers only Approve and Deny (no way to edit scope or risk)", async () => {
    setup();
    const { dialog } = await openDialog();
    expect(within(dialog).getAllByRole("button").map((b) => b.textContent)).toEqual(["Deny", "Approve"]);
    expect(within(dialog).queryByRole("textbox")).toBeNull();
    expect(within(dialog).queryByLabelText(/step-up/i)).toBeNull();
    expect(within(dialog).queryByRole("combobox")).toBeNull();
  });
});
