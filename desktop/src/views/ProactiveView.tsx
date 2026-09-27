import { useCallback, useEffect, useMemo, useState } from "react";
import { BridgeError, toBridgeError } from "../bridge/bridge";
import type {
  OperationResult,
  ProactiveAction,
  ProactiveCreateInput,
  ProactiveDaypart,
  ProactiveFrequency,
  ProactiveLevel,
  ProactiveOverview,
  ProactivePrivacy,
  ProactiveProposed,
  ProactiveScheduleInput,
  ProactiveSemantics,
  ProactiveUpdateInput,
  ProCondition,
  ProNotification,
  ProTask,
} from "../bridge/types";
import { IconTrash } from "../components/Icons";
import { EmptyState, IconButton, NeonButton, Notice, SectionHeader, StatusPill, type Tone } from "../components/primitives";
import { humanize } from "../lib/format";
import { useSam } from "../state";

type Tab = "active" | "scheduled" | "watching" | "notifications" | "history";

const TABS: { id: Tab; label: string }[] = [
  { id: "active", label: "Active" },
  { id: "scheduled", label: "Scheduled" },
  { id: "watching", label: "Watching" },
  { id: "notifications", label: "Notifications" },
  { id: "history", label: "History" },
];

const DAYPARTS: { value: ProactiveDaypart; label: string }[] = [
  { value: "morning", label: "Morning (08:00–12:00)" },
  { value: "afternoon", label: "Afternoon (12:00–17:00)" },
  { value: "evening", label: "Evening (17:00–21:00)" },
];

const LEVELS: { value: ProactiveLevel; label: string }[] = [
  { value: "notify_owner", label: "Notify me" },
  { value: "requires_attention", label: "Needs my attention" },
  { value: "silent", label: "Silent (history only)" },
];

const PRIVACY: { value: ProactivePrivacy; label: string }[] = [
  { value: "public", label: "Public" },
  { value: "personal", label: "Personal" },
  { value: "private", label: "Private" },
];

const PROPOSED_LABEL: Record<ProactiveProposed, string> = {
  none: "",
  review_in_sam: "Review it in Sam",
  open_professional: "Open your Professional profile",
  open_model_settings: "Check AI provider settings",
  review_deadline: "Review the deadline",
};

const REASON_LABEL: Record<string, string> = {
  reminder_due: "A reminder you scheduled is due.",
  reminder_missed: "A reminder was due while Sam wasn't running.",
  summary_ready: "Your scheduled summary is ready.",
  condition_met: "A condition you are watching became true.",
  condition_changed: "A value you are watching changed.",
  condition_still_true: "A condition you are watching is still true.",
};

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function messageFor(error: unknown): string {
  return (error instanceof BridgeError ? error : toBridgeError(error)).message;
}

function when(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function localTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Plain-language schedule, computed from the stored fields only. */
function describe(task: ProTask): string {
  const s = task.schedule;
  const at = s.time_of_day ? `at ${s.time_of_day}` : s.daypart ? `in the ${s.daypart}` : "";
  const every =
    s.frequency === "none"
      ? `once on ${s.start_date}`
      : s.frequency === "hourly"
        ? `every ${s.interval === 1 ? "hour" : `${s.interval} hours`}`
        : s.frequency === "daily"
          ? `every ${s.interval === 1 ? "day" : `${s.interval} days`}`
          : `every ${s.interval === 1 ? "week" : `${s.interval} weeks`}${s.weekdays.length ? ` on ${s.weekdays.map((d) => WEEKDAYS[d]).join(", ")}` : ""}`;
  const end = s.until ? `, until ${s.until}` : s.max_runs ? `, ${s.max_runs} times` : "";
  return `${every} ${s.frequency === "hourly" ? "" : at}${end}`.replace(/\s+/g, " ").trim();
}

const STATUS_TONE: Record<ProTask["status"], Tone> = {
  active: "ok",
  completed: "muted",
  missed: "warn",
  expired: "muted",
  invalid: "danger",
};

function scheduleInput(task: ProTask, changes: { timeOfDay?: string; timezone?: string }): ProactiveScheduleInput {
  const s = task.schedule;
  return {
    timezone: changes.timezone ?? s.timezone,
    startDate: s.start_date,
    timeOfDay: s.time_of_day === null ? null : (changes.timeOfDay ?? s.time_of_day),
    daypart: s.daypart,
    frequency: s.frequency,
    interval: s.interval,
    weekdays: s.weekdays,
    until: s.until,
    maxRuns: s.max_runs,
  };
}

function EditTask({
  task,
  busy,
  onSave,
  onCancel,
}: {
  task: ProTask;
  busy: boolean;
  onSave: (input: ProactiveUpdateInput) => void;
  onCancel: () => void;
}) {
  const [title, setTitle] = useState(task.title);
  const [timeOfDay, setTimeOfDay] = useState(task.schedule.time_of_day ?? "");
  const [timezone, setTimezone] = useState(task.schedule.timezone);
  const [cooldown, setCooldown] = useState(task.cooldown_hours);
  return (
    <form
      className="row"
      aria-label={`Edit ${task.title}`}
      onSubmit={(event) => {
        event.preventDefault();
        onSave({
          taskId: task.task_id,
          title: title.trim() || undefined,
          schedule: scheduleInput(task, { timeOfDay: timeOfDay || undefined, timezone: timezone.trim() }),
          cooldownHours: task.condition_id ? cooldown : undefined,
        });
      }}
    >
      <label>
        Title <input className="text-input" value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} />
      </label>
      {task.schedule.time_of_day !== null ? (
        <label>
          At <input className="text-input" type="time" value={timeOfDay} onChange={(e) => setTimeOfDay(e.target.value)} />
        </label>
      ) : null}
      <label>
        Timezone <input className="text-input" value={timezone} maxLength={64} onChange={(e) => setTimezone(e.target.value)} />
      </label>
      {task.condition_id ? (
        <label>
          Cooldown (hours){" "}
          <input
            className="text-input"
            type="number"
            min={1}
            max={720}
            value={cooldown}
            onChange={(e) => setCooldown(Number(e.target.value))}
          />
        </label>
      ) : null}
      <NeonButton type="submit" disabled={busy || title.trim() === ""}>
        Save
      </NeonButton>
      <NeonButton variant="quiet" disabled={busy} onClick={onCancel}>
        Cancel
      </NeonButton>
    </form>
  );
}

function TaskCard({
  task,
  busy,
  onToggle,
  onRun,
  onDelete,
  onEdit,
}: {
  task: ProTask;
  busy: boolean;
  onToggle: (task: ProTask) => void;
  onRun: (task: ProTask) => void;
  onDelete: (task: ProTask) => void;
  onEdit: (input: ProactiveUpdateInput) => void;
}) {
  const [editing, setEditing] = useState(false);
  return (
    <article className="card" aria-label={`Automation ${task.title}`}>
      <div className="card-row">
        <h4>{task.title}</h4>
        <span className="row">
          <StatusPill tone={STATUS_TONE[task.status]} label={humanize(task.status)} />
          <StatusPill tone={task.enabled ? "ok" : "muted"} label={task.enabled ? "Enabled" : "Disabled"} />
          {task.running ? <StatusPill tone="info" label="Running" /> : null}
          {task.privacy_class === "private" ? <StatusPill tone="high" label="Private" /> : null}
        </span>
      </div>
      <p className="faint">
        {humanize(task.task_type)} · {humanize(task.action)} · {describe(task)} · {task.schedule.timezone}
      </p>
      {task.condition_id ? (
        <p className="faint">
          Watching: {humanize(task.condition_id)} · notifies when it {humanize(task.semantics ?? "becomes_true").toLowerCase()} ·
          cooldown {task.cooldown_hours} h
        </p>
      ) : null}
      <p className="faint">
        Next run: {when(task.next_run_at)} · Last run: {when(task.last_run_at)}
        {task.last_result ? ` · Last result: ${humanize(task.last_result)}` : ""}
        {task.last_failure ? ` (${humanize(task.last_failure)})` : ""}
      </p>
      <div className="row">
        {task.status === "active" ? (
          <NeonButton variant="quiet" disabled={busy} onClick={() => onToggle(task)}>
            {task.enabled ? `Disable “${task.title}”` : `Enable “${task.title}”`}
          </NeonButton>
        ) : null}
        {task.status === "active" ? (
          <NeonButton variant="quiet" disabled={busy || task.running} onClick={() => onRun(task)}>
            Run “{task.title}” now
          </NeonButton>
        ) : null}
        {task.status === "active" ? (
          <NeonButton variant="quiet" disabled={busy} onClick={() => setEditing((e) => !e)}>
            Edit “{task.title}”
          </NeonButton>
        ) : null}
        <IconButton label={`Delete ${task.title}`} disabled={busy} onClick={() => onDelete(task)}>
          <IconTrash />
        </IconButton>
      </div>
      {editing ? (
        <EditTask
          task={task}
          busy={busy}
          onCancel={() => setEditing(false)}
          onSave={(input) => {
            setEditing(false);
            onEdit(input);
          }}
        />
      ) : null}
    </article>
  );
}

function NotificationCard({
  note,
  busy,
  onRead,
  onDismiss,
}: {
  note: ProNotification;
  busy: boolean;
  onRead: (note: ProNotification) => void;
  onDismiss: (note: ProNotification) => void;
}) {
  return (
    <article className="card" aria-label={`Notification ${note.title}`} data-read={note.read ? "true" : "false"}>
      <div className="card-row">
        <h4>{note.title}</h4>
        <span className="row">
          {note.importance === "attention" ? <StatusPill tone="warn" label="Needs attention" /> : null}
          {!note.read ? <StatusPill tone="info" label="New" /> : null}
        </span>
      </div>
      {/* Summaries are untrusted text: rendered as plain text only. */}
      <p className="snippet">{note.summary}</p>
      <p className="faint">
        {when(note.created_at)} · {REASON_LABEL[note.reason_code] ?? humanize(note.reason_code)} · Source: {note.source_label}
      </p>
      {note.proposed_action !== "none" ? (
        <p className="faint">
          Suggested next step: {PROPOSED_LABEL[note.proposed_action]}. Sam won't do this for you; it's only a suggestion.
        </p>
      ) : null}
      <div className="row">
        {!note.read ? (
          <NeonButton variant="quiet" disabled={busy} onClick={() => onRead(note)}>
            Mark read
          </NeonButton>
        ) : null}
        <NeonButton variant="quiet" disabled={busy} onClick={() => onDismiss(note)}>
          Dismiss
        </NeonButton>
      </div>
    </article>
  );
}

interface Draft {
  title: string;
  kind: ProactiveAction;
  repeat: boolean;
  flexible: boolean;
  startDate: string;
  timeOfDay: string;
  daypart: ProactiveDaypart;
  frequency: Exclude<ProactiveFrequency, "none">;
  interval: number;
  timezone: string;
  conditionId: string;
  params: Record<string, string>;
  semantics: ProactiveSemantics;
  cooldownHours: number;
  level: ProactiveLevel;
  privacy: ProactivePrivacy;
  instruction: string;
}

const freshDraft = (): Draft => ({
  title: "",
  kind: "reminder",
  repeat: false,
  flexible: false,
  startDate: today(),
  timeOfDay: "09:00",
  daypart: "morning",
  frequency: "daily",
  interval: 1,
  timezone: localTimezone(),
  conditionId: "",
  params: {},
  semantics: "becomes_true",
  cooldownHours: 24,
  level: "notify_owner",
  privacy: "personal",
  instruction: "",
});

function toInput(draft: Draft): ProactiveCreateInput {
  const watch = draft.kind === "watch";
  const flexible = !watch && draft.flexible;
  const frequency: ProactiveFrequency = watch ? (draft.frequency === "hourly" ? "hourly" : "daily") : draft.repeat ? draft.frequency : "none";
  const schedule: ProactiveScheduleInput = {
    timezone: draft.timezone.trim(),
    startDate: draft.startDate,
    timeOfDay: flexible ? null : draft.timeOfDay,
    daypart: flexible ? draft.daypart : null,
    frequency,
    interval: frequency === "none" ? 1 : Math.max(1, Math.floor(draft.interval)),
    weekdays: [],
    until: null,
    maxRuns: null,
  };
  return {
    title: draft.title.trim(),
    taskType: watch ? "condition_watch" : draft.repeat ? "recurring" : "one_time",
    timingMode: watch ? "condition_watch" : flexible ? "flexible_schedule" : "exact_schedule",
    action: draft.kind,
    schedule,
    conditionId: watch ? draft.conditionId : null,
    conditionParams: watch
      ? Object.fromEntries(Object.entries(draft.params).filter(([, value]) => value.trim() !== ""))
      : {},
    semantics: draft.semantics,
    instruction: draft.instruction,
    privacyClass: draft.privacy,
    notificationLevel: draft.level,
    proposedAction: "none",
    cooldownHours: draft.cooldownHours,
    enabled: true,
  };
}

function NewAutomation({
  conditions,
  busy,
  onCreate,
}: {
  conditions: ProCondition[];
  busy: boolean;
  onCreate: (input: ProactiveCreateInput) => void;
}) {
  const [draft, setDraft] = useState<Draft>(freshDraft);
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => ({ ...d, [key]: value }));
  const condition = conditions.find((c) => c.condition_id === draft.conditionId);
  const watch = draft.kind === "watch";
  const ready = draft.title.trim() !== "" && (!watch || condition !== undefined) && (draft.kind !== "summary" || draft.instruction.trim() !== "");

  return (
    <form
      className="panel glass"
      aria-label="New automation"
      onSubmit={(event) => {
        event.preventDefault();
        if (ready) onCreate(toInput(draft));
      }}
    >
      <h3>New automation</h3>
      <p className="faint">
        Sam only notifies you. It never sends, buys, deletes or applies for anything on a schedule, and it asks
        for permission again every time an automation runs.
      </p>
      <div className="row">
        <label>
          Title{" "}
          <input className="text-input" value={draft.title} maxLength={120} onChange={(e) => set("title", e.target.value)} />
        </label>
        <label>
          What should it do?{" "}
          <select className="text-input" value={draft.kind} onChange={(e) => set("kind", e.target.value as ProactiveAction)}>
            <option value="reminder">Remind me</option>
            <option value="summary">Write me a summary</option>
            <option value="watch">Watch a condition</option>
          </select>
        </label>
      </div>
      {watch ? (
        <div className="row">
          <label>
            Condition{" "}
            <select
              className="text-input"
              value={draft.conditionId}
              onChange={(e) => setDraft((d) => ({ ...d, conditionId: e.target.value, params: {} }))}
            >
              <option value="">Choose…</option>
              {conditions.map((c) => (
                <option key={c.condition_id} value={c.condition_id}>
                  {humanize(c.condition_id)}
                </option>
              ))}
            </select>
          </label>
          {condition
            ? [...condition.required_params, ...condition.optional_params].map((name) => (
                <label key={name}>
                  {humanize(name)}
                  {condition.required_params.includes(name) ? "" : " (optional)"}{" "}
                  <input
                    className="text-input"
                    value={draft.params[name] ?? ""}
                    maxLength={200}
                    onChange={(e) => setDraft((d) => ({ ...d, params: { ...d.params, [name]: e.target.value } }))}
                  />
                </label>
              ))
            : null}
          <label>
            Notify when it{" "}
            <select
              className="text-input"
              value={draft.semantics}
              onChange={(e) => set("semantics", e.target.value as ProactiveSemantics)}
            >
              <option value="becomes_true">becomes true</option>
              <option value="on_change">changes</option>
              <option value="repeat_while_true">is true (repeat, after the cooldown)</option>
            </select>
          </label>
          <label>
            Cooldown (hours){" "}
            <input
              className="text-input"
              type="number"
              min={1}
              max={720}
              value={draft.cooldownHours}
              onChange={(e) => set("cooldownHours", Number(e.target.value))}
            />
          </label>
        </div>
      ) : null}
      <div className="row">
        {!watch ? (
          <label className="row">
            <input type="checkbox" checked={draft.repeat} onChange={(e) => set("repeat", e.target.checked)} />
            Repeat
          </label>
        ) : null}
        {!watch ? (
          <label className="row">
            <input type="checkbox" checked={draft.flexible} onChange={(e) => set("flexible", e.target.checked)} />
            Any time in a part of the day
          </label>
        ) : null}
        <label>
          {draft.repeat || watch ? "Starting" : "On"}{" "}
          <input className="text-input" type="date" value={draft.startDate} onChange={(e) => set("startDate", e.target.value)} />
        </label>
        {!watch && draft.flexible ? (
          <label>
            Part of the day{" "}
            <select className="text-input" value={draft.daypart} onChange={(e) => set("daypart", e.target.value as ProactiveDaypart)}>
              {DAYPARTS.map((d) => (
                <option key={d.value} value={d.value}>
                  {d.label}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <label>
            {watch ? "First check at" : "At"}{" "}
            <input className="text-input" type="time" value={draft.timeOfDay} onChange={(e) => set("timeOfDay", e.target.value)} />
          </label>
        )}
        {draft.repeat || watch ? (
          <label>
            Every{" "}
            <input
              className="text-input"
              type="number"
              min={1}
              max={365}
              value={draft.interval}
              onChange={(e) => set("interval", Number(e.target.value))}
            />{" "}
            <select
              className="text-input"
              aria-label="Repeat unit"
              value={draft.frequency}
              onChange={(e) => set("frequency", e.target.value as Draft["frequency"])}
            >
              {watch || !draft.flexible ? <option value="hourly">hour(s)</option> : null}
              <option value="daily">day(s)</option>
              {!watch ? <option value="weekly">week(s)</option> : null}
            </select>
          </label>
        ) : null}
        <label>
          Timezone{" "}
          <input className="text-input" value={draft.timezone} maxLength={64} onChange={(e) => set("timezone", e.target.value)} />
        </label>
      </div>
      <div className="row">
        <label>
          Notifications{" "}
          <select className="text-input" value={draft.level} onChange={(e) => set("level", e.target.value as ProactiveLevel)}>
            {LEVELS.map((l) => (
              <option key={l.value} value={l.value}>
                {l.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Privacy{" "}
          <select className="text-input" value={draft.privacy} onChange={(e) => set("privacy", e.target.value as ProactivePrivacy)}>
            {PRIVACY.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label>
        {draft.kind === "summary" ? "What should the summary cover?" : "Note (optional)"}{" "}
        <textarea
          className="text-input"
          value={draft.instruction}
          maxLength={1000}
          rows={2}
          onChange={(e) => set("instruction", e.target.value)}
        />
      </label>
      {draft.kind === "summary" ? (
        <p className="faint">
          Summaries go through your AI provider settings. Private automations never use the Gemini free tier, and
          nothing that looks like a secret is ever sent.
        </p>
      ) : null}
      <NeonButton type="submit" disabled={busy || !ready}>
        Create automation
      </NeonButton>
    </form>
  );
}

export function ProactiveView() {
  const { bridge, confirmable } = useSam();
  const [data, setData] = useState<ProactiveOverview | null>(null);
  const [tab, setTab] = useState<Tab>("active");
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState<{ tone?: "danger" | "warn"; text: string } | null>(null);

  const report = (result: OperationResult, success?: string) => {
    if (result.status === "ok") setNotice(success ? { text: success } : null);
    else setNotice({ tone: result.status === "denied" ? "warn" : "danger", text: result.message ?? "That couldn't be completed." });
  };

  const load = useCallback(async () => {
    try {
      const result = await bridge.proactiveOverview();
      if (result.status === "ok") setData(result);
      else {
        setData(null);
        setNotice({ tone: "warn", text: result.message ?? "Your automations couldn't be loaded." });
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

  const tasks = useMemo(() => data?.tasks ?? [], [data]);
  const titles = useMemo(() => new Map(tasks.map((t) => [t.task_id, t.title])), [tasks]);
  const unread = (data?.notifications ?? []).filter((n) => !n.read).length;
  const shown: Record<Exclude<Tab, "notifications" | "history">, ProTask[]> = {
    active: tasks.filter((t) => t.enabled && t.status === "active"),
    scheduled: tasks.filter((t) => t.task_type !== "condition_watch"),
    watching: tasks.filter((t) => t.task_type === "condition_watch"),
  };

  const toggle = (task: ProTask) =>
    void act(
      () => bridge.proactiveUpdate({ taskId: task.task_id, enabled: !task.enabled }),
      task.enabled ? `Disabled “${task.title}”. It won't run until you enable it.` : `Enabled “${task.title}”.`,
    );
  const edit = (input: ProactiveUpdateInput) =>
    void act(() => bridge.proactiveUpdate(input), "Saved. The next run was recalculated from now.");
  const runNow = (task: ProTask) =>
    void act(() => bridge.proactiveRun(task.task_id), `Started “${task.title}”. Its result appears in History.`);
  const remove = (task: ProTask) =>
    void act(
      () => confirmable((confirmationId) => bridge.proactiveDelete(task.task_id, confirmationId)),
      `Deleted “${task.title}”. It will not run again.`,
    );
  const create = (input: ProactiveCreateInput) =>
    void act(async () => {
      const result = await bridge.proactiveCreate(input);
      if (result.status === "ok") setCreating(false);
      return result;
    }, `Created “${input.title}”.`);
  const scheduling = data?.scheduler_enabled ?? false;
  const switchScheduling = () =>
    void act(
      () => bridge.proactiveScheduler(!scheduling),
      scheduling
        ? "Scheduling is off. No automation will run until you turn it back on."
        : "Scheduling is on. Each run still asks for permission, and turning this on granted nothing new.",
    );

  return (
    <div className="page-narrow">
      <SectionHeader
        title="Automations"
        description="Reminders, summaries and watches that Sam runs for you on a schedule. Sam only notifies you: it asks for permission on every run and never acts on your behalf. Kept in memory for this session only."
        actions={
          <span className="row">
            <NeonButton variant="quiet" disabled={busy || data === null} onClick={switchScheduling}>
              {scheduling ? "Turn scheduling off" : "Turn scheduling on"}
            </NeonButton>
            <NeonButton disabled={busy} onClick={() => setCreating((c) => !c)}>
              {creating ? "Close" : "New automation"}
            </NeonButton>
          </span>
        }
      />
      {data !== null && !scheduling ? (
        <Notice tone="warn" live={false}>
          Scheduling is off, so no automation runs (not even “Run now”). It starts off every time Sam starts.
        </Notice>
      ) : null}
      {notice ? <Notice tone={notice.tone}>{notice.text}</Notice> : null}
      {creating && data ? <NewAutomation conditions={data.conditions} busy={busy} onCreate={create} /> : null}

      <div role="tablist" aria-label="Automation sections" className="chips">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`auto-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`auto-panel-${t.id}`}
            className="chip"
            data-active={tab === t.id ? "true" : "false"}
            onClick={() => setTab(t.id)}
          >
            {t.label}
            {t.id === "notifications" && unread > 0 ? ` (${unread})` : ""}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`auto-panel-${tab}`} aria-labelledby={`auto-tab-${tab}`}>
        {data === null ? (
          <p className="muted" role="status">
            Loading…
          </p>
        ) : null}

        {data !== null && (tab === "active" || tab === "scheduled" || tab === "watching") ? (
          shown[tab].length === 0 ? (
            <EmptyState
              title="Nothing here yet"
              body={
                tab === "watching"
                  ? "Watch a trusted condition, such as a deadline approaching."
                  : "Create a reminder or a summary with “New automation”."
              }
            />
          ) : (
            <div className="grid">
              {shown[tab].map((task) => (
                <TaskCard
                  key={task.task_id}
                  task={task}
                  busy={busy}
                  onToggle={toggle}
                  onRun={runNow}
                  onDelete={remove}
                  onEdit={edit}
                />
              ))}
            </div>
          )
        ) : null}

        {data !== null && tab === "notifications" ? (
          data.notifications.length === 0 ? (
            <EmptyState title="No notifications" body="When an automation finds something worth telling you, it appears here." />
          ) : (
            <div className="grid">
              {data.notifications.map((note) => (
                <NotificationCard
                  key={note.notification_id}
                  note={note}
                  busy={busy}
                  onRead={(n) => void act(() => bridge.proactiveNotification(n.notification_id, "read"), "Marked as read.")}
                  onDismiss={(n) => void act(() => bridge.proactiveNotification(n.notification_id, "dismiss"), "Dismissed.")}
                />
              ))}
            </div>
          )
        ) : null}

        {data !== null && tab === "history" ? (
          data.history.length === 0 ? (
            <EmptyState title="No runs yet" body="Each run, skip and failure is recorded here without any of your text." />
          ) : (
            <ul className="evidence-list" aria-label="Automation history">
              {data.history.map((h) => (
                <li key={h.record_id}>
                  <div className="card-row">
                    <strong>{titles.get(h.task_id) ?? "Deleted automation"}</strong>
                    <span className="faint">{when(h.started_at)}</span>
                  </div>
                  <div className="faint">
                    {humanize(h.kind)}
                    {h.trigger ? ` · ${humanize(h.trigger)}` : ""}
                    {h.result ? ` · ${humanize(h.result)}` : ""}
                    {h.failure ? ` · ${humanize(h.failure)}` : ""}
                    {h.change ? ` · ${humanize(h.change)}` : ""}
                    {h.reason_code ? ` · ${humanize(h.reason_code)}` : ""}
                  </div>
                </li>
              ))}
            </ul>
          )
        ) : null}
      </div>
    </div>
  );
}
