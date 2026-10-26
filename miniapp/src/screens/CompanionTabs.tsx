// The companion's tabs other than the conversation: what it owns, each read from its own endpoint so
// a tab nobody opens costs nothing. Goals are the one thing written here as well as read — the
// companion makes them, and the operator can add, pause, finish or delete one without asking it.

import { useState } from "react";
import { api, type BrowserGroup } from "../api";
import { BrowserTab } from "../browser/BrowserPanel";
import { ACCESSORIES, CompanionAvatar, EYES, SHAPES, SWATCHES, avatarOf, type AvatarParams } from "../companionavatar";
import { shortDateTime } from "../format";
import { t } from "../i18n";
import { Icon } from "../icons";
import { pathFor, sessionPath } from "../router";
import { invalidate, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { Dropdown, OverflowMenu, Sheet, go, useMedia } from "../ui/index";
import type { MenuItem } from "../ui/dialogs";
import { timeAgo } from "../ui/components";

// Four, because four fit across a phone. What the companion runs on its clock is the second half of
// Goals — a goal's cadence and its own schedules are the same question, "what will it do next" — and
// its tools and notes are looked at rarely enough to live in the header's menu as sheets.
export const TABS = ["conversation", "goals", "activity", "computer"] as const;
export type CompanionTabId = (typeof TABS)[number];

/** `GET /api/companion`: the shape every client codes against. */
export type CompanionProfile = {
  session_id: string;
  name: string;
  avatar: Partial<AvatarParams>;
  paused: boolean;
  state: "idle" | "listening" | "thinking" | "working";
  running_agents: number;
  enabled?: boolean;
};

type Goal = {
  id: string;
  title: string;
  brief: string;
  status: "active" | "paused" | "done" | "dropped";
  cadence: string;
  next_check_at: string | null;
  last_check_at: string | null;
  last_report: string;
  last_report_at: string | null;
  sessions: string[];
};

export function CompanionTab({ tab, toast, onOpen }: { tab: CompanionTabId; toast: (t: string) => void; onOpen: (id: string) => void }) {
  return (
    <div className="screen wide companion-pane">
      {tab === "goals" && <Goals toast={toast} />}
      {tab === "activity" && <Activity onOpen={onOpen} />}
      {tab === "computer" && <Computer toast={toast} />}
    </div>
  );
}

/** Tools and Memory: what it may do and what it keeps, opened from the header's menu. */
export function CompanionSheet({ kind, sessionId, toast, onClose }: { kind: "tools" | "memory"; sessionId: string; toast: (t: string) => void; onClose: () => void }) {
  return (
    <Sheet title={t(`companion.sheet.${kind}`)} onClose={onClose} size="narrow" className="companion-sheet">
      {kind === "tools" ? <Tools /> : <Memory sessionId={sessionId} toast={toast} />}
    </Sheet>
  );
}

// ── goals ──────────────────────────────────────────────────────────────────────────────────

// The cadences a person picks from, in the words the server already reads. Anything else — a cron,
// "every 3h" — is typed into the custom field.
const CADENCES = ["", "hourly", "every 6h", "daily", "weekly"] as const;

/** A cadence as the server writes it back ("every 1d", "0 7 * * *"), in the reader's words. */
export function cadenceLabel(text: string): string {
  if (!text) return t("companion.cadence.none");
  const match = /^every (\d+)([mhd])$/.exec(text);
  if (!match) return (CADENCES as readonly string[]).includes(text) ? t(`companion.cadence.${text}`) : text;
  const n = Number(match[1]);
  const unit = match[2];
  if (unit === "h" && n === 1) return t("companion.cadence.hourly");
  if (unit === "d" && n === 1) return t("companion.cadence.daily");
  if (unit === "d" && n === 7) return t("companion.cadence.weekly");
  return t(`companion.cadence.every.${unit}`, { n });
}

function Goals({ toast }: { toast: (t: string) => void }) {
  const [closed, setClosed] = useState(false);
  const [adding, setAdding] = useState(false);
  const { data } = useQuery<{ goals: Goal[] }>(`/api/companion/goals${closed ? "?closed=true" : ""}`, { staleMs: 3000, pollMs: 30000 });
  // Both lists (open, and with the finished ones) are stale after any write, and a read already on
  // its way answers from before it: invalidating drops that read rather than joining it.
  const refresh = async () => invalidate("/api/companion/goals");
  const goals = data?.goals ?? [];

  async function change(goal: Goal, status: Goal["status"]) {
    try {
      await api.patch(`/api/companion/goals/${encodeURIComponent(goal.id)}`, { status });
      await refresh();
    } catch (err) {
      toast(errorText(err));
    }
  }
  async function remove(goal: Goal) {
    if (!(await confirmAsync(t("companion.goals.delete.confirm"), { danger: true, action: t("companion.goals.delete") }))) return;
    try {
      await api.delete(`/api/companion/goals/${encodeURIComponent(goal.id)}`);
      await refresh();
    } catch (err) {
      toast(errorText(err));
    }
  }

  return (
    <>
      <div className="companion-head-row">
        <div className="section-title">{t("companion.tab.goals")}</div>
        <button className="btn small" onClick={() => setAdding(true)}>
          <Icon name="plus" size={14} /> {t("companion.goals.add")}
        </button>
      </div>
      {adding && <GoalForm onClose={() => setAdding(false)} onSaved={refresh} toast={toast} />}
      {goals.length === 0 && data && <div className="companion-empty sub">{t("companion.goals.empty")}</div>}
      <div className="companion-list">
        {goals.map((goal) => {
          const open = goal.status === "active" || goal.status === "paused";
          const menu: MenuItem[] = [
            ...(goal.status !== "done" ? [{ label: t("companion.goals.done"), icon: "check" as const, onSelect: () => void change(goal, "done") }] : []),
            ...(goal.status !== "dropped" ? [{ label: t("companion.goals.drop"), icon: "close" as const, onSelect: () => void change(goal, "dropped") }] : []),
            { label: t("companion.goals.delete"), icon: "trash", danger: true, onSelect: () => void remove(goal) },
          ];
          const when = goal.status === "active" && goal.next_check_at ? t("companion.goals.next", { when: shortDateTime(goal.next_check_at) }) : "";
          return (
            <div key={goal.id} className={`card companion-goal ${goal.status}`}>
              <div className="companion-goal-top">
                <b className="grow">{goal.title}</b>
                {goal.status !== "active" && <span className="chip quiet">{t(`companion.goals.status.${goal.status}`)}</span>}
                <OverflowMenu items={menu} small label={t("companion.goals.menu")} />
              </div>
              {goal.brief && <div className="sub clamp-2">{goal.brief}</div>}
              <div className="companion-goal-meta sub num">
                <Icon name="clock" size={13} />
                <span>{[cadenceLabel(goal.cadence), when, goal.sessions.length ? t("companion.goals.agents", { n: goal.sessions.length }) : ""].filter(Boolean).join(" · ")}</span>
              </div>
              {goal.last_report && (
                <div className="companion-goal-report">
                  <div className="sub">{t("companion.goals.report", { when: timeAgo(goal.last_report_at) })}</div>
                  <div className="clamp-3">{goal.last_report}</div>
                </div>
              )}
              {open && (
                <div className="companion-goal-actions">
                  {goal.status === "active"
                    ? <button className="btn ghost small" onClick={() => void change(goal, "paused")}><Icon name="pause" size={14} /> {t("companion.goals.pause")}</button>
                    : <button className="btn ghost small" onClick={() => void change(goal, "active")}><Icon name="play" size={14} /> {t("companion.goals.resume")}</button>}
                </div>
              )}
            </div>
          );
        })}
      </div>
      <button type="button" className="companion-closed link" onClick={() => setClosed(!closed)} aria-pressed={closed}>
        {t(closed ? "companion.goals.closed.hide" : "companion.goals.closed")}
      </button>
      <Schedule />
    </>
  );
}

function GoalForm({ onClose, onSaved, toast }: { onClose: () => void; onSaved: () => Promise<void>; toast: (t: string) => void }) {
  const [title, setTitle] = useState("");
  const [brief, setBrief] = useState("");
  const [preset, setPreset] = useState<string>("daily");
  const [custom, setCustom] = useState("");
  const [busy, setBusy] = useState(false);
  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;
    setBusy(true);
    try {
      await api.post("/api/companion/goals", { title, brief, cadence: preset === "custom" ? custom : preset });
      await onSaved();
      onClose();
    } catch (err) {
      toast(errorText(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("companion.goals.add")} onClose={onClose} size="narrow">
      <form className="companion-goal-form" onSubmit={(e) => void add(e)}>
        <label className="companion-field">
          <span className="sub">{t("companion.goals.title.label")}</span>
          <input className="field" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t("companion.goals.title")} maxLength={200} autoFocus />
        </label>
        <label className="companion-field">
          <span className="sub">{t("companion.goals.brief.label")}</span>
          <textarea className="field" value={brief} onChange={(e) => setBrief(e.target.value)} placeholder={t("companion.goals.brief")} rows={3} maxLength={4000} />
        </label>
        <div className="companion-field">
          <span className="sub">{t("companion.goals.cadence")}</span>
          <Dropdown
            label={t("companion.goals.cadence")}
            value={preset}
            onChange={setPreset}
            options={[...CADENCES.map((id) => ({ id: id as string, label: cadenceLabel(id) })), { id: "custom", label: t("companion.cadence.custom") }]}
          />
          {preset === "custom" && <input className="field" value={custom} onChange={(e) => setCustom(e.target.value)} placeholder={t("companion.goals.cadence.hint")} aria-label={t("companion.cadence.custom")} maxLength={120} />}
        </div>
        <div className="btnrow">
          <button className="btn ghost small" type="button" onClick={onClose}>{t("companion.edit.cancel")}</button>
          <button className="btn small" type="submit" disabled={busy || !title.trim()}>{t("companion.goals.create")}</button>
        </div>
      </form>
    </Sheet>
  );
}

// ── what runs on its clock ─────────────────────────────────────────────────────────────────

type ScheduleRow = { id: string; name: string; kind?: string; cron?: string | null; run_at?: string | null; next_run_at?: string | null; status?: string };

/** Its own schedules and the ones it proposed, under the goals; a goal's checks are on its card. */
function Schedule() {
  const { data } = useQuery<{ schedules: ScheduleRow[]; proposals: ScheduleRow[] }>("/api/companion/schedules", { staleMs: 5000, pollMs: 60000 });
  if (!data || (!data.schedules.length && !data.proposals.length)) return null;
  const row = (r: ScheduleRow, waiting: boolean) => (
    <div key={r.id} className="companion-sched">
      <Icon name={waiting ? "question" : "clock"} size={15} />
      <div className="grow">
        <div className="truncate">{r.name}</div>
        <div className="sub num">{waiting ? t("companion.schedule.waiting") : r.next_run_at ? t("companion.schedule.next", { when: shortDateTime(r.next_run_at) }) : r.cron || shortDateTime(r.run_at)}</div>
      </div>
    </div>
  );
  return (
    <>
      <div className="companion-head-row">
        <div className="section-title">{t("companion.schedule.title")}</div>
        <a className="btn ghost small" href={pathFor("schedules")} onClick={(e) => go(e, pathFor("schedules"))}>{t("companion.schedule.manage")}</a>
      </div>
      <div className="card companion-scheds">
        {data.proposals.map((r) => row(r, true))}
        {data.schedules.map((r) => row(r, false))}
      </div>
    </>
  );
}

// ── activity ───────────────────────────────────────────────────────────────────────────────

type ActivityRow = { id: number; at: string; kind: string; title: string; body: string; session_id: string | null; goal_id: string | null };

function Activity({ onOpen }: { onOpen: (id: string) => void }) {
  const [before, setBefore] = useState<number | null>(null);
  const { data } = useQuery<{ items: ActivityRow[] }>(`/api/companion/activity${before ? `?before=${before}` : ""}`, { staleMs: 3000, pollMs: before ? 0 : 15000 });
  const items = data?.items ?? [];
  if (data && !items.length) return <div className="empty"><div>{t("companion.activity.empty")}</div></div>;
  return (
    <>
      <ol className="companion-activity">
        {items.map((item) => {
          const known = `companion.activity.kind.${item.kind}`;
          const word = t(known).startsWith("[") ? item.kind : t(known);
          return (
            <li key={item.id} className={`companion-event kind-${item.kind}`}>
              <span className="sub num companion-event-when">{timeAgo(item.at)}</span>
              <span className="chip quiet">{word}</span>
              {item.session_id ? (
                <a className="link truncate" href={sessionPath(item.session_id)} onClick={(e) => { e.preventDefault(); onOpen(item.session_id!); }}>{item.title}</a>
              ) : (
                <b className="truncate">{item.title}</b>
              )}
              {item.body && <div className="sub clamp-2 companion-event-body">{item.body}</div>}
            </li>
          );
        })}
      </ol>
      {items.length >= 100 && (
        <div className="btnrow">
          <button className="btn ghost small" onClick={() => setBefore(items[items.length - 1].id)}>{t("companion.activity.more")}</button>
        </div>
      )}
    </>
  );
}

// ── computer ───────────────────────────────────────────────────────────────────────────────

function Computer({ toast }: { toast: (t: string) => void }) {
  const phone = useMedia("(max-width: 1023px)");
  const { data } = useQuery<{ available: boolean; reason: string; groups: BrowserGroup[] }>("/api/companion/computer", { staleMs: 3000, pollMs: 10000 });
  return (
    <>
      <div className="sub companion-intro">{t("companion.computer.intro")}</div>
      {data && !data.available && data.reason && <div className="sub">{data.reason}</div>}
      <div className="card companion-computer">
        <BrowserTab groups={data?.groups ?? []} toast={toast} phone={phone} />
      </div>
    </>
  );
}

// ── tools ──────────────────────────────────────────────────────────────────────────────────

function Tools() {
  const { data } = useQuery<{ companion: { name: string; description: string }[]; agents: { tools: number; mcp: string[] } }>("/api/companion/tools", { staleMs: 60000 });
  if (!data) return <div className="sub">{t("common.loading")}</div>;
  return (
    <div className="companion-sheet-body">
      <div className="sub">{t("companion.tools.never")}</div>
      <div className="section-title">{t("companion.tools.own")}</div>
      <div className="card companion-tools">
        {data.companion.map((tool) => (
          <div key={tool.name} className="companion-tool">
            <code>{tool.name}</code>
            <div className="sub clamp-2">{tool.description}</div>
          </div>
        ))}
      </div>
      <div className="section-title">{t("companion.tools.agents")}</div>
      <div className="sub">{t("companion.tools.agents.body", { n: data.agents.tools })}</div>
      <div className="sub">{data.agents.mcp.length ? t("companion.tools.mcp", { list: data.agents.mcp.join(", ") }) : t("companion.tools.mcp.none")}</div>
    </div>
  );
}

// ── memory ─────────────────────────────────────────────────────────────────────────────────

type MemoryRecord = { id: string; kind: string; text: string; created_at: string | null };

function Memory({ sessionId, toast }: { sessionId: string; toast: (t: string) => void }) {
  const { data, refresh } = useQuery<{ records: MemoryRecord[] }>(sessionId ? "/api/memory?scope=global" : null, { staleMs: 5000 });
  const records = data?.records ?? [];
  async function forget(id: string) {
    try {
      await api.delete(`/api/memory/${encodeURIComponent(id)}`);
      await refresh();
    } catch (err) {
      toast(errorText(err));
    }
  }
  return (
    <div className="companion-sheet-body">
      <div className="sub">{t("companion.memory.intro")}</div>
      {(!sessionId || (data && !records.length)) && <div className="companion-empty sub">{t("companion.memory.empty")}</div>}
      {records.length > 0 && (
        <div className="card companion-tools">
          {records.map((r) => (
            <div key={r.id} className="companion-note">
              <span className="grow">{r.text}</span>
              <button className="iconbtn" onClick={() => void forget(r.id)} aria-label={t("companion.memory.delete")} title={t("companion.memory.delete")}>
                <Icon name="trash" size={16} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── the profile editor ─────────────────────────────────────────────────────────────────────

export function CompanionEditor({ profile, onClose, onSaved, toast }: { profile: CompanionProfile; onClose: () => void; onSaved: (next: CompanionProfile) => void; toast: (t: string) => void }) {
  const [name, setName] = useState(profile.name);
  const [avatar, setAvatar] = useState<AvatarParams>(avatarOf(profile.avatar));
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try {
      onSaved(await api.put<CompanionProfile>("/api/companion", { name, avatar }));
      onClose();
    } catch (err) {
      toast(errorText(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("companion.edit")} onClose={onClose} size="narrow">
      <div className="companion-editor">
        <div className="companion-editor-face">
          <CompanionAvatar avatar={avatar} size={96} name={name} />
        </div>
        <label className="companion-field">
          <span className="sub">{t("companion.edit.name")}</span>
          <input className="field" value={name} maxLength={40} onChange={(e) => setName(e.target.value)} aria-label={t("companion.edit.name")} />
        </label>
        <label className="companion-field">
          <span className="sub">{t("companion.edit.shape")}</span>
          <Dropdown label={t("companion.edit.shape")} value={avatar.shape} onChange={(shape) => setAvatar({ ...avatar, shape })} options={SHAPES.map((id) => ({ id, label: t(`companion.shape.${id}`) }))} />
        </label>
        <div className="companion-field">
          <span className="sub">{t("companion.edit.color")}</span>
          <div className="companion-swatches">
            {SWATCHES.map((color) => (
              <button key={color} type="button" className={`companion-swatch${avatar.color === color ? " on" : ""}`} style={{ background: color }} aria-label={color} aria-pressed={avatar.color === color} onClick={() => setAvatar({ ...avatar, color })} />
            ))}
            <input type="color" value={avatar.color} onChange={(e) => setAvatar({ ...avatar, color: e.target.value })} aria-label={t("companion.edit.color")} />
          </div>
        </div>
        <label className="companion-field">
          <span className="sub">{t("companion.edit.eyes")}</span>
          <Dropdown label={t("companion.edit.eyes")} value={avatar.eyes} onChange={(eyes) => setAvatar({ ...avatar, eyes })} options={EYES.map((id) => ({ id, label: t(`companion.eyes.${id}`) }))} />
        </label>
        <label className="companion-field">
          <span className="sub">{t("companion.edit.accessory")}</span>
          <Dropdown label={t("companion.edit.accessory")} value={avatar.accessory} onChange={(accessory) => setAvatar({ ...avatar, accessory })} options={ACCESSORIES.map((id) => ({ id, label: t(`companion.accessory.${id}`) }))} />
        </label>
        <div className="btnrow">
          <button className="btn ghost small" onClick={onClose}>{t("companion.edit.cancel")}</button>
          <button className="btn small" disabled={busy || !name.trim()} onClick={() => void save()}>{t("companion.edit.save")}</button>
        </div>
      </div>
    </Sheet>
  );
}
