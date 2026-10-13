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
import { Dropdown, Sheet, go, useMedia } from "../ui/index";
import { timeAgo } from "../ui/components";

export const TABS = ["conversation", "goals", "schedule", "activity", "computer", "tools", "memory"] as const;
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

export function CompanionTab({ tab, profile, toast, onOpen }: { tab: CompanionTabId; profile: CompanionProfile | null; toast: (t: string) => void; onOpen: (id: string) => void }) {
  return (
    <div className="screen wide companion-pane">
      {tab === "goals" && <Goals toast={toast} />}
      {tab === "schedule" && <Schedule />}
      {tab === "activity" && <Activity onOpen={onOpen} />}
      {tab === "computer" && <Computer toast={toast} />}
      {tab === "tools" && <Tools />}
      {tab === "memory" && <Memory sessionId={profile?.session_id ?? ""} toast={toast} />}
    </div>
  );
}

// ── goals ──────────────────────────────────────────────────────────────────────────────────

function Goals({ toast }: { toast: (t: string) => void }) {
  const [closed, setClosed] = useState(false);
  const { data } = useQuery<{ goals: Goal[] }>(`/api/companion/goals${closed ? "?closed=true" : ""}`, { staleMs: 3000, pollMs: 30000 });
  // Both lists (open, and with the finished ones) are stale after any write, and a read already on
  // its way answers from before it: invalidating drops that read rather than joining it.
  const refresh = async () => invalidate("/api/companion/goals");
  const [title, setTitle] = useState("");
  const [brief, setBrief] = useState("");
  const [cadence, setCadence] = useState("");
  const [busy, setBusy] = useState(false);
  const goals = data?.goals ?? [];

  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;
    setBusy(true);
    try {
      await api.post("/api/companion/goals", { title, brief, cadence });
      setTitle("");
      setBrief("");
      setCadence("");
      await refresh();
    } catch (err) {
      toast(errorText(err));
    } finally {
      setBusy(false);
    }
  }
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
      <form className="card companion-goal-form" onSubmit={(e) => void add(e)}>
        <div className="section-title" style={{ marginTop: 0 }}>{t("companion.goals.add")}</div>
        <input className="field" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t("companion.goals.title")} aria-label={t("companion.goals.title")} maxLength={200} />
        <textarea className="field" value={brief} onChange={(e) => setBrief(e.target.value)} placeholder={t("companion.goals.brief")} aria-label={t("companion.goals.brief")} rows={2} maxLength={4000} />
        <div className="companion-goal-row">
          <input className="field" value={cadence} onChange={(e) => setCadence(e.target.value)} placeholder={t("companion.goals.cadence.hint")} aria-label={t("companion.goals.cadence")} maxLength={120} />
          <button className="btn small" type="submit" disabled={busy || !title.trim()}>{t("companion.goals.create")}</button>
        </div>
      </form>
      {goals.length === 0 && data && <div className="empty"><div>{t("companion.goals.empty")}</div></div>}
      <div className="companion-list">
        {goals.map((goal) => (
          <div key={goal.id} className={`card companion-goal ${goal.status}`}>
            <div className="companion-goal-top">
              <b className="grow">{goal.title}</b>
              <span className={`chip ${goal.status === "active" ? "accent" : "quiet"}`}>{t(`companion.goals.status.${goal.status}`)}</span>
            </div>
            {goal.brief && <div className="sub clamp-2">{goal.brief}</div>}
            <div className="sub num">
              {goal.cadence ? `${goal.cadence} · ${goal.next_check_at && goal.status === "active" ? t("companion.goals.next", { when: shortDateTime(goal.next_check_at) }) : ""}` : t("companion.goals.never")}
              {goal.sessions.length > 0 && ` · ${t("companion.goals.agents", { n: goal.sessions.length })}`}
            </div>
            {goal.last_report && (
              <div className="companion-goal-report">
                <div className="sub">{t("companion.goals.report", { when: timeAgo(goal.last_report_at) })}</div>
                <div>{goal.last_report}</div>
              </div>
            )}
            <div className="btnrow">
              {goal.status === "active" && <button className="btn ghost small" onClick={() => void change(goal, "paused")}>{t("companion.goals.pause")}</button>}
              {goal.status !== "active" && <button className="btn ghost small" onClick={() => void change(goal, "active")}>{t("companion.goals.resume")}</button>}
              {goal.status !== "done" && <button className="btn ghost small" onClick={() => void change(goal, "done")}>{t("companion.goals.done")}</button>}
              {goal.status !== "dropped" && <button className="btn ghost small" onClick={() => void change(goal, "dropped")}>{t("companion.goals.drop")}</button>}
              <button className="btn ghost small danger" onClick={() => void remove(goal)}>{t("companion.goals.delete")}</button>
            </div>
          </div>
        ))}
      </div>
      <label className="companion-closed">
        <input type="checkbox" checked={closed} onChange={(e) => setClosed(e.target.checked)} /> {t("companion.goals.closed")}
      </label>
    </>
  );
}

// ── schedule ───────────────────────────────────────────────────────────────────────────────

type ScheduleRow = { id: string; name: string; kind?: string; cron?: string | null; run_at?: string | null; next_run_at?: string | null; status?: string };

function Schedule() {
  const { data } = useQuery<{ schedules: ScheduleRow[]; proposals: ScheduleRow[]; goals: Goal[] }>("/api/companion/schedules", { staleMs: 5000, pollMs: 60000 });
  if (!data) return <div className="sub">{t("common.loading")}</div>;
  const nothing = !data.schedules.length && !data.proposals.length && !data.goals.length;
  if (nothing) return <div className="empty"><div>{t("companion.schedule.empty")}</div></div>;
  const row = (r: ScheduleRow) => (
    <div key={r.id} className="card row companion-row">
      <Icon name="clock" size={16} />
      <div className="grow">
        <b className="truncate">{r.name}</b>
        <div className="sub num">{r.cron || shortDateTime(r.run_at)}{r.next_run_at ? ` · ${t("companion.schedule.next", { when: shortDateTime(r.next_run_at) })}` : ""}</div>
      </div>
    </div>
  );
  return (
    <>
      {data.goals.length > 0 && <div className="section-title">{t("companion.schedule.goals")}</div>}
      {data.goals.map((g) => (
        <div key={g.id} className="card row companion-row">
          <Icon name="compass" size={16} />
          <div className="grow">
            <b className="truncate">{g.title}</b>
            <div className="sub num">{g.cadence}{g.next_check_at && g.status === "active" ? ` · ${t("companion.schedule.next", { when: shortDateTime(g.next_check_at) })}` : ` · ${t(`companion.goals.status.${g.status}`)}`}</div>
          </div>
        </div>
      ))}
      {data.schedules.length > 0 && <div className="section-title">{t("companion.schedule.own")}</div>}
      {data.schedules.map(row)}
      {data.proposals.length > 0 && <div className="section-title">{t("companion.schedule.proposals")}</div>}
      {data.proposals.map(row)}
      <div className="btnrow">
        <a className="btn ghost small" href={pathFor("schedules")} onClick={(e) => go(e, pathFor("schedules"))}>{t("companion.schedule.manage")}</a>
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
    <>
      <div className="section-title">{t("companion.tools.own")}</div>
      <div className="companion-list">
        {data.companion.map((tool) => (
          <div key={tool.name} className="card row companion-row">
            <code>{tool.name}</code>
            <span className="sub grow">{tool.description}</span>
          </div>
        ))}
      </div>
      <div className="sub companion-intro">{t("companion.tools.never")}</div>
      <div className="section-title">{t("companion.tools.agents")}</div>
      <div className="sub">{t("companion.tools.agents.body", { n: data.agents.tools })}</div>
      <div className="sub">{data.agents.mcp.length ? t("companion.tools.mcp", { list: data.agents.mcp.join(", ") }) : t("companion.tools.mcp.none")}</div>
    </>
  );
}

// ── memory ─────────────────────────────────────────────────────────────────────────────────

type MemoryRecord = { id: string; kind: string; text: string; created_at: string | null };

function Memory({ sessionId, toast }: { sessionId: string; toast: (t: string) => void }) {
  const { data, refresh } = useQuery<{ records: MemoryRecord[] }>(sessionId ? `/api/memory?scope=session&scope_key=${encodeURIComponent(sessionId)}` : null, { staleMs: 5000 });
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
    <>
      <div className="sub companion-intro">{t("companion.memory.intro")}</div>
      {(!sessionId || (data && !records.length)) && <div className="empty"><div>{t("companion.memory.empty")}</div></div>}
      <div className="companion-list">
        {records.map((r) => (
          <div key={r.id} className="card row companion-row">
            <span className="chip quiet">{r.kind}</span>
            <span className="grow">{r.text}</span>
            <button className="btn ghost small" onClick={() => void forget(r.id)}>{t("companion.memory.delete")}</button>
          </div>
        ))}
      </div>
    </>
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
