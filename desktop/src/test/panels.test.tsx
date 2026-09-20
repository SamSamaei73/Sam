import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { GrantInfo } from "../bridge/types";
import { baseStatus, mockBridge, ok, renderApp } from "./helpers";

const grant = (over: Partial<GrantInfo> = {}): GrantInfo => ({
  grant_id: "g1",
  resource: "knowledge",
  action: "read",
  scope: "default:retrieve",
  status: "active",
  expires_at: null,
  requires_confirmation: false,
  origin: "desktop_bootstrap",
  ...over,
});

async function goto(name: string) {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name }));
  return user;
}

describe("Tools", () => {
  it("is honest that the framework is ready but nothing is connected", async () => {
    renderApp(mockBridge());
    await goto("Tools");
    expect(await screen.findByText("No tools connected")).toBeInTheDocument();
    expect(screen.getByText("Foundation ready")).toBeInTheDocument();
  });

  it("is read-only: no register/enable/edit controls", async () => {
    renderApp(
      mockBridge({
        tools: vi.fn(async () => ({
          state: "configured" as const,
          servers: [{ server_id: "mail", display_name: "Mail" }],
          tools: [
            {
              tool_id: "mail.read_message",
              server_id: "mail",
              description: "<b>Read</b> one message",
              permission_resource: "gmail",
              permission_action: "read",
              enabled: true,
              verification_required: true,
              credential_configured: true,
            },
          ],
        })),
      }),
    );
    await goto("Tools");
    expect(await screen.findByText("mail.read_message")).toBeInTheDocument();
    expect(screen.getByText("<b>Read</b> one message")).toBeInTheDocument();
    const main = screen.getByRole("main");
    expect(within(main).queryByRole("button", { name: /register|add|enable|disable|edit|install|connect/i })).toBeNull();
  });
});

describe("Permissions", () => {
  it("lists grants and allows revoke only", async () => {
    const bridge = mockBridge({ permissions: vi.fn(async () => ({ grants: [grant()] })) });
    renderApp(bridge);
    const user = await goto("Permissions");
    expect(await screen.findByText("default:retrieve")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(bridge.revokeGrant).toHaveBeenCalledWith("g1"));
  });

  it("offers no way to create or widen a permission", async () => {
    renderApp(mockBridge({ permissions: vi.fn(async () => ({ grants: [grant()] })) }));
    await goto("Permissions");
    await screen.findByText("default:retrieve");
    const main = screen.getByRole("main");
    expect(within(main).queryByRole("button", { name: /grant|allow|approve|add|create|enable/i })).toBeNull();
    expect(within(main).queryByRole("textbox")).toBeNull();
    expect(within(main).queryByRole("checkbox")).toBeNull();
    expect(screen.getByText(/can't grant, widen or bypass/i)).toBeInTheDocument();
  });

  it("does not show Revoke for an already-revoked grant", async () => {
    renderApp(mockBridge({ permissions: vi.fn(async () => ({ grants: [grant({ status: "revoked" })] })) }));
    await goto("Permissions");
    await screen.findByText("Revoked");
    expect(screen.queryByRole("button", { name: "Revoke" })).toBeNull();
  });

  it("shows the backend's message when a revoke fails", async () => {
    renderApp(
      mockBridge({
        permissions: vi.fn(async () => ({ grants: [grant()] })),
        revokeGrant: vi.fn(async () => ({ ...ok, status: "rejected" as const, message: "That item no longer exists." })),
      }),
    );
    const user = await goto("Permissions");
    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    expect(await screen.findByText("That item no longer exists.")).toBeInTheDocument();
  });
});

describe("Activity", () => {
  it("renders labelled, content-free events with risk", async () => {
    renderApp(
      mockBridge({
        activity: vi.fn(async () => ({
          scope: "current_session" as const,
          items: [
            { timestamp: "2026-01-01T10:00:00Z", kind: "permission", label: "Permission checked: Knowledge read", outcome: "allowed", risk: "low" },
            { timestamp: "2026-01-01T10:01:00Z", kind: "knowledge", label: "<i>Knowledge removal</i>", outcome: "denied", risk: "high" },
          ],
        })),
      }),
    );
    await goto("Activity");
    expect(await screen.findByText("Permission checked: Knowledge read")).toBeInTheDocument();
    expect(screen.getByText("<i>Knowledge removal</i>")).toBeInTheDocument();
    expect(document.querySelector("i")).toBeNull();
    expect(screen.getByText(/never message text/i)).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Risk" })).toBeInTheDocument();
  });

  it("shows an empty state", async () => {
    renderApp(mockBridge());
    await goto("Activity");
    expect(await screen.findByText("No activity yet")).toBeInTheDocument();
  });
});

describe("Settings", () => {
  it("reports capability states honestly", async () => {
    renderApp(mockBridge());
    await goto("Settings");
    expect((await screen.findAllByText("Not configured")).length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText("Foundation ready")).toBeInTheDocument();
  });

  it("has no field for a backend URL, token, API key or endpoint", async () => {
    renderApp(mockBridge({ status: vi.fn(async () => ({ ...baseStatus, speech_output: "configured" as const, speech_profiles: [{ profile_id: "sam_default", languages: ["en" as const] }] })) }));
    await goto("Settings");
    await screen.findByText("Capabilities");
    const main = screen.getByRole("main");
    expect(within(main).queryByRole("textbox")).toBeNull();
    expect(within(main).queryByLabelText(/url|token|key|endpoint|secret|password/i)).toBeNull();
    const select = within(main).getByRole("combobox", { name: /read-aloud voice/i });
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["sam_default"]);
  });

  it("stores only allowlisted UI preferences", async () => {
    renderApp(mockBridge());
    const user = await goto("Settings");
    await user.click(await screen.findByLabelText("Reduce motion"));
    expect(JSON.parse(window.localStorage.getItem("sam.ui.prefs.v1") ?? "{}").reducedMotion).toBe(true);
    expect(document.documentElement.dataset.reducedMotion).toBe("true");
  });
});
