import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { StatusResponse, SystemHealth } from "../bridge/types";
import { DEFAULT_PREFS, loadPrefs, savePrefs } from "../lib/prefs";
import { baseStatus, mockBridge, ok, renderApp } from "./helpers";

const subsystems = (overrides: Record<string, { status: SystemHealth["subsystems"][number]["status"]; reason_code?: string | null }> = {}) =>
  ["database", "migrations", "keychain", "model_router", "career"].map((name) => ({
    name,
    status: overrides[name]?.status ?? "ok",
    reason_code: overrides[name]?.reason_code ?? null,
  }));

const healthy: SystemHealth = {
  status: "ready",
  phase: "ready",
  reason_code: null,
  storage_mode: "sqlite",
  schema_version: 1,
  last_backup_at: "2026-09-27T10:00:00+00:00",
  backup_count: 2,
  scheduler: "on_persistent",
  reconciliation_required: 0,
  subsystems: subsystems(),
};

async function openPanel(status: StatusResponse) {
  const user = userEvent.setup();
  renderApp(mockBridge({ status: vi.fn(async () => status) }));
  await screen.findByLabelText(/Connection:/);
  await user.click(screen.getByRole("button", { name: "Show system panel" }));
  return screen.getByRole("complementary", { name: "System panel" });
}

describe("system health", () => {
  it("shows durable storage, schema, backup and scheduling honestly", async () => {
    const panel = await openPanel({ ...baseStatus, health: healthy });
    const health = within(panel).getByRole("region", { name: "Sam health" });
    expect(within(health).getByText("Ready")).toBeInTheDocument();
    expect(within(health).getByText(/Saved on this Mac · schema v1/)).toBeInTheDocument();
    expect(within(health).getByText("2026-09-27")).toBeInTheDocument();
    expect(within(health).getByText("On (stays on after restart)")).toBeInTheDocument();
    expect(within(health).queryByRole("list", { name: "Needs attention" })).toBeNull();
  });

  it("explains a blocked start with reviewed text only", async () => {
    const blocked: SystemHealth = {
      ...healthy,
      status: "blocked",
      phase: "integrity_check",
      reason_code: "database_corrupt",
      last_backup_at: null,
      scheduler: "off",
      subsystems: subsystems({ database: { status: "blocked", reason_code: "database_corrupt" } }),
    };
    const panel = await openPanel({ ...baseStatus, health: blocked });
    const health = within(panel).getByRole("region", { name: "Sam health" });
    expect(within(health).getByText("Blocked")).toBeInTheDocument();
    expect(within(health).getAllByText(/failed its integrity check/).length).toBeGreaterThan(0);
    expect(within(health).getByText("None yet")).toBeInTheDocument();
  });

  it("an unknown reason code is shown generically, never verbatim", async () => {
    const odd: SystemHealth = {
      ...healthy,
      status: "blocked",
      reason_code: "sqlite3.OperationalError: /Users/x/secret.db",
    };
    const panel = await openPanel({ ...baseStatus, health: odd });
    expect(within(panel).getByText("Sam could not start safely.")).toBeInTheDocument();
    expect(within(panel).queryByText(/secret\.db/)).toBeNull();
  });

  it("items awaiting reconciliation are called out", async () => {
    const pending: SystemHealth = {
      ...healthy,
      status: "degraded",
      reconciliation_required: 1,
      subsystems: subsystems({ career: { status: "degraded", reason_code: "reconciliation_required" } }),
    };
    const panel = await openPanel({ ...baseStatus, health: pending });
    expect(within(panel).getByText("Needs attention")).toBeInTheDocument();
    expect(within(panel).getByText(/1 item\(s\) need checking/)).toBeInTheDocument();
  });

  it("works with an older backend that sends no health", async () => {
    const panel = await openPanel(baseStatus);
    expect(within(panel).queryByRole("region", { name: "Sam health" })).toBeNull();
  });
});

describe("persistent scheduling", () => {
  it("is opt-in: the owner must tick the box to keep scheduling on", async () => {
    const scheduler = vi.fn(async (enabled: boolean) => ({ ...ok, scheduler_enabled: enabled, scheduler_persistent: true }));
    const user = userEvent.setup();
    renderApp(mockBridge({ proactiveScheduler: scheduler }));
    await screen.findByLabelText("Connection: Connected");
    await user.click(await screen.findByRole("button", { name: "Automations" }));
    const box = await screen.findByRole("checkbox", { name: "Keep scheduling on after Sam restarts" });
    expect(box).not.toBeChecked();
    await user.click(box);
    await user.click(screen.getByRole("button", { name: "Turn scheduling on" }));
    expect(scheduler).toHaveBeenCalledWith(true, true);
  });
});

describe("browser storage", () => {
  it("never stores a credential, even if one is smuggled into the prefs", () => {
    const key = "sk-ant-api03-" + "Z".repeat(40);
    savePrefs({ ...DEFAULT_PREFS, apiKey: key, token: key } as unknown as typeof DEFAULT_PREFS);
    const stored = JSON.stringify(window.localStorage);
    expect(stored).not.toContain(key);
    expect(Object.keys(loadPrefs()).sort()).toEqual(Object.keys(DEFAULT_PREFS).sort());
    expect(window.sessionStorage.length).toBe(0);
  });
});
