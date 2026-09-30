import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Challenge, ProactiveOverview, ProNotification, ProTask } from "../bridge/types";
import { emptyProactive, mockBridge, ok, renderApp } from "./helpers";

const task = (over: Partial<ProTask> = {}): ProTask => ({
  task_id: "t1",
  title: "Call the lab",
  task_type: "recurring",
  timing_mode: "exact_schedule",
  action: "reminder",
  schedule: {
    timezone: "Europe/London",
    start_date: "2026-01-06",
    time_of_day: "10:00",
    daypart: null,
    frequency: "daily",
    interval: 1,
    weekdays: [],
    until: null,
    max_runs: null,
  },
  condition_id: null,
  condition_params: {},
  semantics: null,
  instruction: "Ask about the results",
  privacy_class: "personal",
  notification_level: "notify_owner",
  proposed_action: "none",
  cooldown_hours: 24,
  enabled: true,
  status: "active",
  expires_at: null,
  created_at: "2026-01-05T09:00:00Z",
  next_run_at: "2026-01-06T10:00:00Z",
  last_run_at: null,
  last_result: null,
  last_failure: null,
  last_reason: null,
  running: false,
  version: 1,
  ...over,
});

const watchTask = task({
  task_id: "t2",
  title: "Thesis deadline",
  task_type: "condition_watch",
  timing_mode: "condition_watch",
  action: "watch",
  condition_id: "deadline_approaching",
  condition_params: { date: "2026-02-01" },
  semantics: "becomes_true",
  privacy_class: "private",
  cooldown_hours: 12,
  schedule: { ...task().schedule, frequency: "hourly", time_of_day: "00:00" },
});

const note = (over: Partial<ProNotification> = {}): ProNotification => ({
  notification_id: "n1",
  task_id: "t2",
  title: "Thesis deadline",
  summary: "Deadline in about 20 hour(s). <img src=x onerror=alert(1)>",
  created_at: "2026-01-31T04:00:00Z",
  reason_code: "condition_met",
  importance: "attention",
  source_label: "deadline",
  proposed_action: "review_deadline",
  privacy_class: "private",
  read: false,
  ...over,
});

const populated: ProactiveOverview = {
  ...emptyProactive,
  tasks: [task(), watchTask],
  notifications: [note()],
  history: [
    {
      record_id: "r1",
      task_id: "t1",
      kind: "run",
      trigger: "scheduled",
      started_at: "2026-01-05T10:00:00Z",
      completed_at: "2026-01-05T10:00:01Z",
      result: "error_recorded",
      failure: "permission_denied",
      change: null,
      reason_code: "permission_denied",
      notification_created: false,
      provider_id: null,
    },
  ],
};

async function openAutomations(bridge: ReturnType<typeof mockBridge>) {
  renderApp(bridge);
  const user = userEvent.setup();
  await screen.findByLabelText("Connection: Connected");
  await user.click(await screen.findByRole("button", { name: "Automations" }));
  await screen.findByRole("heading", { name: "Automations" });
  return user;
}

describe("Automations view", () => {
  it("is honest when empty and says Sam only notifies", async () => {
    await openAutomations(mockBridge());
    expect(await screen.findByText("Nothing here yet")).toBeInTheDocument();
    expect(screen.getByText(/Sam only notifies you: it asks for permission on every run/)).toBeInTheDocument();
  });

  it("shows each task's type, schedule, timezone, next and last run, and state", async () => {
    await openAutomations(mockBridge({ proactiveOverview: vi.fn(async () => populated) }));
    const card = await screen.findByLabelText("Automation Call the lab");
    expect(within(card).getByText(/Recurring · Reminder · every day at 10:00 · Europe\/London/)).toBeInTheDocument();
    expect(within(card).getByText(/Next run:/)).toBeInTheDocument();
    expect(within(card).getByText("Enabled")).toBeInTheDocument();
  });

  it("shows condition watches with their cooldown and privacy", async () => {
    const user = await openAutomations(mockBridge({ proactiveOverview: vi.fn(async () => populated) }));
    await user.click(screen.getByRole("tab", { name: "Watching" }));
    const card = await screen.findByLabelText("Automation Thesis deadline");
    expect(within(card).getByText(/cooldown 12 h/)).toBeInTheDocument();
    expect(within(card).getByText("Private")).toBeInTheDocument();
    expect(screen.queryByLabelText("Automation Call the lab")).toBeNull();
  });

  it("disables, runs now and edits through the bridge only", async () => {
    const update = vi.fn(async () => ({ ...ok, task: null }));
    const run = vi.fn(async () => ok);
    const user = await openAutomations(
      mockBridge({ proactiveOverview: vi.fn(async () => populated), proactiveUpdate: update, proactiveRun: run }),
    );
    await user.click(await screen.findByRole("button", { name: "Disable “Call the lab”" }));
    expect(update).toHaveBeenCalledWith({ taskId: "t1", enabled: false });
    await user.click(await screen.findByRole("button", { name: "Run “Call the lab” now" }));
    expect(run).toHaveBeenCalledWith("t1");
    await user.click(await screen.findByRole("button", { name: "Edit “Call the lab”" }));
    const form = await screen.findByRole("form", { name: "Edit Call the lab" });
    const tz = within(form).getByLabelText("Timezone");
    await user.clear(tz);
    await user.type(tz, "Asia/Tokyo");
    await user.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(2));
    const edited = (update.mock.calls[1] as unknown as [{ schedule: { timezone: string; timeOfDay: string } }])[0];
    expect(edited.schedule.timezone).toBe("Asia/Tokyo");
    expect(edited.schedule.timeOfDay).toBe("10:00");
  });

  it("deleting a task asks for confirmation first", async () => {
    const challenge: Challenge = {
      confirmation_id: "conf-7",
      action: "delete",
      resource: "proactive",
      scope: "proactive/tasks/t1",
      risk: "high",
      target: null,
      reason: null,
      expires_at: "2026-01-05T09:05:00Z",
    };
    const remove = vi.fn(async (_id: string, confirmationId?: string) =>
      confirmationId ? ok : { ...ok, status: "confirmation_required" as const, reason_code: "confirmation_required", challenge },
    );
    const user = await openAutomations(
      mockBridge({ proactiveOverview: vi.fn(async () => populated), proactiveDelete: remove }),
    );
    await user.click(await screen.findByRole("button", { name: "Delete Call the lab" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("High risk")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(remove).toHaveBeenCalledTimes(2));
    expect(remove).toHaveBeenLastCalledWith("t1", "conf-7");
    expect(await screen.findByText(/It will not run again/)).toBeInTheDocument();
  });

  it("the inbox shows why Sam surfaced something and never runs the suggestion", async () => {
    const notify = vi.fn(async () => ok);
    const bridge = mockBridge({ proactiveOverview: vi.fn(async () => populated), proactiveNotification: notify });
    const user = await openAutomations(bridge);
    await user.click(screen.getByRole("tab", { name: "Notifications (1)" }));
    const card = await screen.findByLabelText("Notification Thesis deadline");
    expect(within(card).getByText("Needs attention")).toBeInTheDocument();
    expect(within(card).getByText(/A condition you are watching became true/)).toBeInTheDocument();
    expect(within(card).getByText(/Review the deadline\. Sam won't do this for you/)).toBeInTheDocument();
    // Untrusted summary text is rendered as text, never as markup.
    expect(within(card).getByText(/<img src=x onerror=alert\(1\)>/)).toBeInTheDocument();
    expect(card.querySelector("img")).toBeNull();
    expect(within(card).queryByRole("button", { name: /review the deadline|run|approve|send/i })).toBeNull();
    await user.click(within(card).getByRole("button", { name: "Mark read" }));
    expect(notify).toHaveBeenCalledWith("n1", "read");
    await user.click(within(card).getByRole("button", { name: "Dismiss" }));
    expect(notify).toHaveBeenCalledWith("n1", "dismiss");
    expect(bridge.proactiveRun).not.toHaveBeenCalled();
  });

  it("history shows outcomes without task text", async () => {
    const user = await openAutomations(mockBridge({ proactiveOverview: vi.fn(async () => populated) }));
    await user.click(screen.getByRole("tab", { name: "History" }));
    const list = await screen.findByRole("list", { name: "Automation history" });
    expect(within(list).getByText(/Run · Scheduled · Error recorded · Permission denied/)).toBeInTheDocument();
    expect(within(list).queryByText(/Ask about the results/)).toBeNull();
  });

  it("creates a reminder with an explicit timezone and no hidden fields", async () => {
    const create = vi.fn(async () => ({ ...ok, task: task() }));
    const user = await openAutomations(mockBridge({ proactiveCreate: create }));
    await user.click(screen.getByRole("button", { name: "New automation" }));
    const form = await screen.findByRole("form", { name: "New automation" });
    await user.type(within(form).getByLabelText("Title"), "Call the lab");
    const tz = within(form).getByLabelText("Timezone");
    await user.clear(tz);
    await user.type(tz, "Europe/London");
    await user.click(within(form).getByRole("button", { name: "Create automation" }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    const input = (create.mock.calls[0] as unknown as [Record<string, unknown>])[0];
    expect(input.taskType).toBe("one_time");
    expect(input.timingMode).toBe("exact_schedule");
    expect((input.schedule as { timezone: string }).timezone).toBe("Europe/London");
    expect((input.schedule as { frequency: string }).frequency).toBe("none");
    for (const key of Object.keys(input)) {
      expect(key).not.toMatch(/provider|permission|confirmation|command|code|principal/i);
    }
  });

  it("a watch must name a trusted condition before it can be created", async () => {
    const create = vi.fn(async () => ({ ...ok, task: watchTask }));
    const user = await openAutomations(mockBridge({ proactiveCreate: create }));
    await user.click(screen.getByRole("button", { name: "New automation" }));
    const form = await screen.findByRole("form", { name: "New automation" });
    await user.type(within(form).getByLabelText("Title"), "Thesis deadline");
    await user.selectOptions(within(form).getByLabelText("What should it do?"), "watch");
    const submit = within(form).getByRole("button", { name: "Create automation" });
    expect(submit).toBeDisabled();
    await user.selectOptions(within(form).getByLabelText("Condition"), "deadline_approaching");
    await user.type(within(form).getByLabelText("Date"), "2026-02-01");
    await user.click(submit);
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    const input = (create.mock.calls[0] as unknown as [Record<string, unknown>])[0];
    expect(input.taskType).toBe("condition_watch");
    expect(input.conditionId).toBe("deadline_approaching");
    expect(input.conditionParams).toEqual({ date: "2026-02-01" });
  });

  it("scheduling starts off, says so, and only the owner's switch turns it on", async () => {
    const scheduler = vi.fn(async (enabled: boolean) => ({ ...ok, scheduler_enabled: enabled }));
    const user = await openAutomations(mockBridge({ proactiveScheduler: scheduler }));
    expect(await screen.findByText(/Scheduling is off, so no automation runs/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Turn scheduling on" }));
    expect(scheduler).toHaveBeenCalledWith(true);
    expect(await screen.findByText(/turning this on granted nothing new/)).toBeInTheDocument();
  });

  it("with scheduling on there is no off-warning and the switch turns it off", async () => {
    const scheduler = vi.fn(async (enabled: boolean) => ({ ...ok, scheduler_enabled: enabled }));
    const user = await openAutomations(
      mockBridge({
        proactiveOverview: vi.fn(async () => ({ ...populated, scheduler_enabled: true })),
        proactiveScheduler: scheduler,
      }),
    );
    await screen.findByLabelText("Automation Call the lab");
    expect(screen.queryByText(/Scheduling is off/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Turn scheduling off" }));
    expect(scheduler).toHaveBeenCalledWith(false);
  });

  it("a notification rejected by Sam's gate shows only the generic text", async () => {
    const withheld = note({
      summary: "Sam found an update for this automation but withheld its details because they looked like they contained a secret.",
    });
    const user = await openAutomations(
      mockBridge({ proactiveOverview: vi.fn(async () => ({ ...populated, notifications: [withheld] })) }),
    );
    await user.click(screen.getByRole("tab", { name: "Notifications (1)" }));
    const card = await screen.findByLabelText("Notification Thesis deadline");
    expect(within(card).getByText(/withheld its details/)).toBeInTheDocument();
  });

  it("shows a guest-mode refusal as a message, not data", async () => {
    const refused = vi.fn(async () => {
      const { BridgeError } = await import("../bridge/bridge");
      throw new BridgeError("guest_mode_active", "Guest Mode is active. End Guest Mode to use this.");
    });
    await openAutomations(mockBridge({ proactiveOverview: refused }));
    expect(await screen.findByText(/Guest Mode is active/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^Automation (?!sections)/)).toBeNull();
    expect(screen.queryByLabelText(/Notification /)).toBeNull();
  });
});
