import { useMemo, useState } from "react";
import { api, Schedule } from "../api";
import { Pill, Skeleton } from "../components";
import { OverflowMenu, Sheet } from "../dialogs";
import { absTime, cronFor, describeCron, describeSchedule, relTime, untilShort } from "../format";
import { Icon } from "../icons";
import { navigate, pathFor } from "../router";
import { PageHeader, screenTitle } from "../shell";
import { invalidate, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { t } from "../i18n";
import { useSessionTitles } from "./Sessions";

type Group = "upcoming" | "paused" | "done";

function groupOf(s: Schedule): Group {
  if (s.enabled && s.next_run_at) return "upcoming";
  if (!s.enabled && (s.cron || (s.run_at && !s.last_run_at))) return "paused";
  return "done";
}

function lastOutcome(s: Schedule): { status: string; word: string } | null {
  if (!s.last_run_at) return null;
  if (s.failure_count > 0) return { status: "failed", word: t("sched.failed", { n: s.failure_count }) };
  return { status: "done", word: t("sched.ran") };
}

const kindWord = (kind: Schedule["kind"]) => t(`sched.kind.${kind}`);

export function SchedulesScreen({ toast, onOpen, selected }: { toast: (t: string) => void; onOpen: (id: string) => void; selected?: string | null }) {
  const { data: items, error, loading, refresh } = useQuery<Schedule[]>("/api/schedules", { pollMs: 30000, staleMs: 5000 });
  const titles = useSessionTitles();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Schedule | null>(null);
  const reload = () => {
    refresh();
    invalidate("/api/schedules");
  };
  const groups = useMemo(() => {
    const all = items ?? [];
    const by: Record<Group, Schedule[]> = { upcoming: [], paused: [], done: [] };
    for (const s of all) by[groupOf(s)].push(s);
    by.upcoming.sort((a, b) => Date.parse(a.next_run_at ?? "") - Date.parse(b.next_run_at ?? ""));
    by.done.sort((a, b) => Date.parse(b.last_run_at ?? "") - Date.parse(a.last_run_at ?? ""));
    return by;
  }, [items]);
  const open = selected ? (items ?? []).find((s) => s.id === selected) ?? null : null;

  async function remove(s: Schedule) {
    if (!(await confirmAsync(t("sched.delete.title", { name: s.name }), { body: t(s.cron ? "sched.delete.body.cron" : "sched.delete.body.once"), action: t("sched.delete.action") }))) return;
    try {
      await api.delete(`/api/schedules/${s.id}`);
      if (selected === s.id) navigate(pathFor("schedules"), { replace: true });
      reload();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function runNow(s: Schedule) {
    try {
      const r = await api.post<{ session_id: string }>(`/api/schedules/${s.id}/run`);
      toast(t("sched.started"));
      reload();
      if (r.session_id) onOpen(r.session_id);
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function setEnabled(s: Schedule, enabled: boolean) {
    try {
      await api.patch(`/api/schedules/${s.id}`, { enabled });
      toast(t(enabled ? "sched.resumed" : "sched.pausedtoast"));
      reload();
    } catch (e) {
      toast(errorText(e));
    }
  }

  const row = (s: Schedule) => {
    const outcome = lastOutcome(s);
    const target = s.target_session ? titles[s.target_session] : undefined;
    return (
      <div key={s.id} className="erow schedule" role="link" tabIndex={0} onClick={() => navigate(pathFor("schedules", s.id))} onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); navigate(pathFor("schedules", s.id)); } }}>
        <span className={`kind ${s.enabled ? "" : "muted"}`}><Icon name={s.kind === "message" ? "inbox" : s.kind === "lazy" ? "bulb" : "clock"} size={16} /></span>
        <div className="erow-main">
          <div className="erow-head">
            <span className="erow-title clamp-2">{s.name}</span>
            {s.enabled && s.next_run_at ? <span className="erow-time num">{untilShort(s.next_run_at)}</span> : outcome ? <Pill status={outcome.status}>{outcome.word}</Pill> : null}
          </div>
          <div className="erow-meta">
            <span>{describeSchedule(s)}</span>
            {!s.enabled && s.cron && <span className="sep">·</span>}
            {!s.enabled && s.cron && <span className="word">{t("sched.paused.word")}</span>}
          </div>
          <div className="erow-meta">
            <span>{kindWord(s.kind)}</span>
            {s.run_in === "self" && target && <span className="sep">·</span>}
            {s.run_in === "self" && target && <span>{t("sched.in", { name: target })}</span>}
            {s.last_run_at && <span className="sep">·</span>}
            {s.last_run_at && <span title={absTime(s.last_run_at)}>{t("sched.last", { t: relTime(s.last_run_at) })}</span>}
            {s.active_session_id && <span className="sep">·</span>}
            {s.active_session_id && <span className="word running">{t("sched.running")}</span>}
          </div>
        </div>
      </div>
    );
  };

  return (
    <>
      <PageHeader
        title={screenTitle("schedules")}
        subtitle={items ? `${t("sched.upcoming", { n: groups.upcoming.length })}${groups.paused.length ? t("sched.paused.count", { n: groups.paused.length }) : ""}` : undefined}
        actions={<button className="iconbtn primary" onClick={() => setCreating(true)} title={t("sched.new")} aria-label={t("sched.new")}><Icon name="plus" /></button>}
      />
      <div className="screen narrow">
        {loading && !error && <Skeleton rows={4} />}
        {error && !items && <div className="empty"><b>{t("sched.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
        {items && items.length === 0 && (
          <div className="empty">
            <b>{t("sched.empty")}</b>
            <div>{t("sched.empty.sub")}</div>
            <button className="btn primary" onClick={() => setCreating(true)}>{t("sched.new")}</button>
          </div>
        )}
        {(["upcoming", "paused", "done"] as Group[]).map((g) =>
          groups[g].length === 0 ? null : (
            <section key={g}>
              <div className="section-title">
                {t(`sched.group.${g}`)} <span className="n">{groups[g].length}</span>
              </div>
              {groups[g].map(row)}
            </section>
          ),
        )}
      </div>
      {creating && <ScheduleForm onClose={() => setCreating(false)} onSaved={() => { setCreating(false); reload(); }} toast={toast} />}
      {editing && <ScheduleForm existing={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); reload(); }} toast={toast} />}
      {open && !editing && (
        <Sheet
          title={open.name}
          onClose={() => navigate(pathFor("schedules"), { replace: true })}
          head={
            <OverflowMenu
              small
              label={t("sched.actions")}
              items={[
                { label: t("common.edit"), icon: "pen", onSelect: () => setEditing(open) },
                open.enabled ? { label: t("common.pause"), icon: "pause", onSelect: () => setEnabled(open, false) } : { label: t("common.resume"), icon: "play", onSelect: () => setEnabled(open, true), disabled: !open.cron && !!open.last_run_at },
                "-",
                { label: t("board.delete.menu"), icon: "trash", danger: true, onSelect: () => remove(open) },
              ]}
            />
          }
        >
          {/* Named as local time: beside "Cron (UTC)" an unqualified clock that disagreed with it read as a
              second schedule rather than the same one in the reader's zone. */}
          <div className="kv"><span>{t("sched.when.local")}</span><b>{describeSchedule(open)}</b></div>
          {open.cron && <div className="kv"><span>{t("sched.cron")}</span><b className="mono">{open.cron}</b></div>}
          <div className="kv"><span>{t("sched.next")}</span><b>{open.enabled && open.next_run_at ? `${absTime(open.next_run_at)} · ${untilShort(open.next_run_at)}` : open.enabled ? "—" : t("sched.paused.word")}</b></div>
          {open.last_run_at && <div className="kv"><span>{t("sched.lastrun")}</span><b>{absTime(open.last_run_at)}{open.failure_count ? ` · ${t("sched.failed", { n: open.failure_count })}` : ""}</b></div>}
          <div className="kv"><span>{t("sched.kind")}</span><b>{kindWord(open.kind)}{open.run_in === "self" ? t("sched.kind.in", { name: (open.target_session && titles[open.target_session]) || t("sched.kind.itssession") }) : open.kind === "agent" ? t("sched.kind.own") : ""}</b></div>
          {open.last_error && <div className="kv"><span>{t("sched.lasterror")}</span><b style={{ color: "var(--bad)" }}>{open.last_error}</b></div>}
          <section className="sheet-section">
            <div className="sheet-section-title">{t(open.kind === "agent" ? "sched.instruction" : "sched.text")}</div>
            <div className="proposal-text">{open.prompt}</div>
          </section>
          {open.last_summary && (
            <section className="sheet-section">
              <div className="sheet-section-title">{t("sched.lastresult")}</div>
              <div className="proposal-text">{open.last_summary}</div>
            </section>
          )}
          <div className="sheet-foot">
            {open.active_session_id && <button className="btn" onClick={() => onOpen(open.active_session_id!)}>{t("sched.openrunning")}</button>}
            <button className="btn primary" onClick={() => runNow(open)}><Icon name="play" size={14} /> {t("common.runnow")}</button>
          </div>
        </Sheet>
      )}
    </>
  );
}

type When = "once" | "daily" | "weekdays" | "weekly" | "hours" | "cron";
/** Monday first, the way a week is picked here; the cron field counts from Sunday. */
const DAYS = [1, 2, 3, 4, 5, 6, 0];

/** When, in the reader's own clock: a moment, a daily or weekly time, or every few hours; cron stays for the rest. */
function ScheduleForm({ existing, onClose, onSaved, toast }: { existing?: Schedule; onClose: () => void; onSaved: () => void; toast: (t: string) => void }) {
  const [name, setName] = useState(existing?.name ?? "");
  const [kind, setKind] = useState<"agent" | "message">(existing?.kind === "message" ? "message" : "agent");
  const [prompt, setPrompt] = useState(existing?.prompt ?? "");
  const [when, setWhen] = useState<When>(existing?.cron ? "cron" : "once");
  const [date, setDate] = useState(() => (existing?.run_at ? new Date(existing.run_at) : new Date(Date.now() + 3600000)).toLocaleDateString("en-CA"));
  const [time, setTime] = useState(() => (existing?.run_at ? new Date(existing.run_at) : new Date(Date.now() + 3600000)).toTimeString().slice(0, 5));
  const [days, setDays] = useState<number[]>([1, 3, 5]);
  const [every, setEvery] = useState("2");
  const [cron, setCron] = useState(existing?.cron ?? "");
  const [busy, setBusy] = useState(false);
  const [h, m] = time.split(":").map(Number);

  const built = (() => {
    if (when === "once") {
      const d = new Date(`${date}T${time}`);
      return { run_at: Number.isNaN(d.getTime()) ? null : d.toISOString(), cron: null };
    }
    if (when === "cron") return { run_at: null, cron: cron.trim() || null };
    if (when === "weekly") return { run_at: null, cron: cronFor("weekly", h, m, days.map((d) => (d + 1) % 7)) };
    if (when === "hours") return { run_at: null, cron: cronFor("hours", h, m, [], Number(every) || 1) };
    return { run_at: null, cron: cronFor(when, h, m) };
  })();
  const preview = built.cron ? describeCron(built.cron) : built.run_at ? t("fmt.once", { when: absTime(built.run_at) }) : "";
  const valid = name.trim() && prompt.trim() && (built.cron || built.run_at) && (when !== "weekly" || days.length > 0);

  async function save() {
    setBusy(true);
    try {
      if (existing) await api.patch(`/api/schedules/${existing.id}`, { name: name.trim(), prompt: prompt.trim(), cron: built.cron, run_at: built.run_at });
      else await api.post("/api/schedules", { name: name.trim(), prompt: prompt.trim(), cron: built.cron, run_at: built.run_at, kind });
      toast(t(existing ? "sched.saved" : "sched.created"));
      onSaved();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t(existing ? "sched.edit" : "sched.new")} onClose={onClose}>
      <label className="field">{t("common.name")}</label>
      <input className="field" autoFocus value={name} onChange={(e) => setName(e.target.value)} />
      {!existing && (
        <>
          <label className="field">{t("sched.type")}</label>
          <div className="segmented inline" role="radiogroup">
            <button role="radio" aria-checked={kind === "agent"} className={kind === "agent" ? "on" : ""} onClick={() => setKind("agent")}>{t("sched.kind.agent")}</button>
            <button role="radio" aria-checked={kind === "message"} className={kind === "message" ? "on" : ""} onClick={() => setKind("message")}>{t("sched.kind.message")}</button>
          </div>
          <div className="sub" style={{ marginTop: 4 }}>{t(kind === "agent" ? "sched.type.agent.hint" : "sched.type.message.hint")}</div>
        </>
      )}
      <label className="field">{t(kind === "agent" ? "sched.instruction" : "sched.remindertext")}</label>
      <textarea className="field" rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
      <label className="field">{t("sched.when")}</label>
      {/* Wrapped, not scrolled: in one scrolling row a phone showed four and a half of the six, and
          an existing Cron schedule opened with its own choice out of sight. */}
      <div className="chips wrap">
        {(["once", "daily", "weekdays", "weekly", "hours", "cron"] as When[]).map((w) => (
          <button key={w} className="chip select" aria-pressed={when === w} onClick={() => setWhen(w)}>
            {t(`sched.when.${w}`)}
          </button>
        ))}
      </div>
      {when === "once" && (
        <div className="grid2">
          <div><label className="field">{t("sched.date")}</label><input className="field" type="date" value={date} onChange={(e) => setDate(e.target.value)} /></div>
          <div><label className="field">{t("sched.time")}</label><input className="field" type="time" value={time} onChange={(e) => setTime(e.target.value)} /></div>
        </div>
      )}
      {(when === "daily" || when === "weekdays" || when === "weekly" || when === "hours") && (
        <div className="grid2">
          <div><label className="field">{t(when === "hours" ? "sched.minute" : "sched.time")}</label><input className="field" type="time" value={time} onChange={(e) => setTime(e.target.value)} /></div>
          {when === "hours" && <div><label className="field">{t("sched.everyhours")}</label><input className="field" type="number" min={1} max={24} value={every} onChange={(e) => setEvery(e.target.value)} /></div>}
        </div>
      )}
      {when === "weekly" && (
        <>
          <label className="field">{t("sched.days")}</label>
          <div className="chips wrap">
            {DAYS.map((d, i) => (
              <button key={d} className="chip select" aria-pressed={days.includes(i)} onClick={() => setDays((cur) => (cur.includes(i) ? cur.filter((x) => x !== i) : [...cur, i].sort()))}>{t(`fmt.dow.${d}`)}</button>
            ))}
          </div>
        </>
      )}
      {when === "cron" && (
        <>
          <label className="field">{t("sched.cron.label")}</label>
          <input className="field mono" placeholder="0 4 * * 1-5" value={cron} onChange={(e) => setCron(e.target.value)} />
        </>
      )}
      <div className="sub preview-line">{preview ? t("sched.preview", { when: preview }) : t("sched.preview.none")}{built.cron && when !== "cron" ? <span className="faint"> · cron {built.cron}</span> : null}</div>
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" disabled={busy || !valid} onClick={save}>{t(existing ? "common.save" : "common.create")}</button>
      </div>
    </Sheet>
  );
}
