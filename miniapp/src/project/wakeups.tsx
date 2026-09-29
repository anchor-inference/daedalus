// The orchestrator's wake-ups and the project's watches. Wake-ups are the alarms it set itself and the
// ones the operator left it; watches are "when X happens, do Y". Each can be cancelled from its row,
// a watch switched off and on, and a new one of either set from a sheet. One component serves the
// centre of focus mode and the right panel's tab (`compact`), like every page of a project.

import { useState } from "react";
import { api, type ProjectWatch, type Wakeup, type WatchList } from "../api";
import { Skeleton } from "../components";
import { Sheet } from "../dialogs";
import { absTime, describeCron, relTime, relTimeLong, untilShort } from "../format";
import { t } from "../i18n";
import { Icon } from "../icons";
import { PageHeader } from "../shell";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";
import { useFocus, useProject, wakeupsKey, watchesKey } from "./data";
import { NOTIFY_LEVELS, TASK_STATUSES, TELL_TIMINGS, WATCH_ACTIONS, WATCH_KINDS, emptyWatch, watchBody, watchThenText, watchWhenText, type WatchDraft, type WatchKind } from "./watchmodel";
import { WAKEUP_WHENS, wakeupBody, wakeupWhen, type WakeupDraft, type WakeupWhen } from "./wakeupmodel";

const enc = encodeURIComponent;

type Props = { projectId: string; back?: string | null; compact?: boolean; toast?: (text: string) => void };

export function WakeupsPage({ projectId, back, compact, toast }: Props) {
  const { project } = useProject(projectId);
  const { data, error, refresh } = useQuery<{ wakeups: Wakeup[]; max: number }>(wakeupsKey(projectId), { pollMs: 30000, staleMs: 5000 });
  const [adding, setAdding] = useState(false);
  const say = toast ?? (() => undefined);
  const enabled = !!project?.settings.orchestrator?.enabled;
  const list = data?.wakeups ?? [];

  async function cancel(w: Wakeup) {
    try {
      await api.delete(`${wakeupsKey(projectId)}/${enc(w.id)}`);
      say(t("focus.wakeups.cancelled"));
      invalidate(wakeupsKey(projectId));
    } catch (e) {
      say(errorText(e));
    }
  }

  const body = (
    <div className="wakeups">
      <section className="wakeup-section" aria-label={t("focus.wakeups.title")}>
        <div className="wakeup-section-head">
          <span className="wakeup-section-title">{t("focus.wakeups.title")}</span>
          <span className="grow" />
          {enabled && <button className="btn small" onClick={() => setAdding(true)}><Icon name="plus" size={14} /> {t("focus.wakeups.add")}</button>}
        </div>
        {!data && !error && <Skeleton rows={2} />}
        {error && !data && <div className="empty"><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
        {data && list.length === 0 && <div className="empty calm">{t(enabled ? "focus.wakeups.empty" : "focus.wakeups.off")}</div>}
        {list.map((w) => (
          <div key={w.id} className={`wakeup-row ${w.enabled ? "" : "off"}`}>
            <Icon name="clock" size={16} />
            <div className="wakeup-main">
              <div className="wakeup-name">{w.note}</div>
              <div className="wakeup-meta sub">
                <span>{wakeupWhen(w)}</span>
                {w.next_run_at && <span title={absTime(w.next_run_at)}> · {t("focus.wakeups.next", { when: untilShort(w.next_run_at) })}</span>}
              </div>
              <div className="wakeup-meta sub faint">
                {t(w.set_by === "operator" ? "focus.wakeups.by.operator" : "focus.wakeups.by.orchestrator")} · {relTime(w.created_at)}
                {w.cron && <span className="mono"> · {w.cron} UTC</span>}
              </div>
            </div>
            <button className="iconbtn small quiet" onClick={() => void cancel(w)} title={t("focus.wakeups.cancel")} aria-label={t("focus.wakeups.cancel")}>
              <Icon name="close" size={16} />
            </button>
          </div>
        ))}
      </section>
      <WatchesSection projectId={projectId} toast={say} />
      {adding && <WakeupSheet projectId={projectId} onClose={() => setAdding(false)} toast={say} />}
    </div>
  );
  if (compact) return body;
  return (
    <>
      <PageHeader title={project ? t("focus.wakeups.page", { name: project.name }) : t("focus.wakeups.title")} back={back ?? undefined} />
      <div className="screen narrow">{body}</div>
    </>
  );
}

function WakeupSheet({ projectId, onClose, toast }: { projectId: string; onClose: () => void; toast: (text: string) => void }) {
  const soon = new Date(Date.now() + 3600000);
  const [draft, setDraft] = useState<WakeupDraft>({ note: "", when: "in", minutes: "30", date: soon.toLocaleDateString("en-CA"), time: soon.toTimeString().slice(0, 5), cron: "" });
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<WakeupDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const request = wakeupBody(draft);
  // The moment is previewed before the note is written, so a time that will not do shows at once.
  const timing = wakeupBody({ ...draft, note: "·" });
  const preview = timing?.cron ? describeCron(timing.cron) : timing?.at ? t("fmt.once", { when: absTime(timing.at) }) : timing?.in_minutes ? untilShort(Date.now() + timing.in_minutes * 60000) : "";

  async function save() {
    if (!request) return;
    setBusy(true);
    try {
      await api.post(wakeupsKey(projectId), request);
      toast(t("focus.wakeups.added"));
      invalidate(wakeupsKey(projectId));
      onClose();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("focus.wakeups.new")} onClose={onClose} size="narrow" className="wakeup-sheet">
      <label className="field" htmlFor="wakeup-note">{t("focus.wakeups.note")}</label>
      <textarea id="wakeup-note" className="field" rows={3} maxLength={500} autoFocus value={draft.note} placeholder={t("focus.wakeups.note.hint")} onChange={(e) => set({ note: e.target.value })} />
      <label className="field">{t("sched.when")}</label>
      <div className="chips" aria-label={t("sched.when")}>
        {WAKEUP_WHENS.map((w: WakeupWhen) => (
          <button key={w} className="chip select" aria-pressed={draft.when === w} onClick={() => set({ when: w })}>{t(w === "cron" ? "sched.when.cron" : `focus.wakeups.when.${w}`)}</button>
        ))}
      </div>
      {draft.when === "in" && (
        <>
          <label className="field" htmlFor="wakeup-minutes">{t("focus.wakeups.minutes")}</label>
          <input id="wakeup-minutes" className="field" type="number" inputMode="numeric" min={1} value={draft.minutes} onChange={(e) => set({ minutes: e.target.value })} />
        </>
      )}
      {draft.when === "once" && (
        <div className="grid2">
          <div><label className="field" htmlFor="wakeup-date">{t("sched.date")}</label><input id="wakeup-date" className="field" type="date" value={draft.date} onChange={(e) => set({ date: e.target.value })} /></div>
          <div><label className="field" htmlFor="wakeup-time">{t("sched.time")}</label><input id="wakeup-time" className="field" type="time" value={draft.time} onChange={(e) => set({ time: e.target.value })} /></div>
        </div>
      )}
      {draft.when === "daily" && (
        <>
          <label className="field" htmlFor="wakeup-daily">{t("sched.time")}</label>
          <input id="wakeup-daily" className="field" type="time" value={draft.time} onChange={(e) => set({ time: e.target.value })} />
        </>
      )}
      {draft.when === "cron" && (
        <>
          <label className="field" htmlFor="wakeup-cron">{t("sched.cron.label")}</label>
          <input id="wakeup-cron" className="field mono" placeholder="0 9 * * 1-5" value={draft.cron} onChange={(e) => set({ cron: e.target.value })} />
        </>
      )}
      <div className="sub preview-line">{preview ? t("focus.wakeups.preview", { when: preview }) : t("sched.preview.none")}</div>
      <div className="sub form-hint">{t("focus.wakeups.hint")}</div>
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" disabled={busy || !request} onClick={() => void save()}>{t("focus.wakeups.set")}</button>
      </div>
    </Sheet>
  );
}

// ── watches ──────────────────────────────────────────────────────────────────────────────────

/** Why a watch switched itself off, as the host names it. */
const STOPPED = ["once", "budget", "pattern", "slow_pattern"];

function WatchesSection({ projectId, toast }: { projectId: string; toast: (text: string) => void }) {
  const { data, error, refresh } = useQuery<WatchList>(watchesKey(projectId), { pollMs: 30000, staleMs: 5000 });
  const [adding, setAdding] = useState(false);
  const list = data?.watches ?? [];

  async function act(w: ProjectWatch, change: "toggle" | "remove") {
    try {
      if (change === "remove") await api.delete(`${watchesKey(projectId)}/${enc(w.id)}`);
      else await api.patch(`${watchesKey(projectId)}/${enc(w.id)}`, { enabled: !w.enabled });
      toast(t(change === "remove" ? "focus.watches.removed" : w.enabled ? "focus.watches.paused" : "focus.watches.resumed"));
      invalidate(watchesKey(projectId));
    } catch (e) {
      toast(errorText(e));
    }
  }

  return (
    <section className="wakeup-section" aria-label={t("focus.watches.title")}>
      <div className="wakeup-section-head">
        <span className="wakeup-section-title">{t("focus.watches.title")}</span>
        <span className="grow" />
        <button className="btn small" onClick={() => setAdding(true)}><Icon name="plus" size={14} /> {t("focus.watches.add")}</button>
      </div>
      {!data && !error && <Skeleton rows={2} />}
      {error && !data && <div className="empty"><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
      {data && list.length === 0 && <div className="empty calm">{t("focus.watches.empty")}</div>}
      {list.map((w) => (
        <div key={w.id} className={`wakeup-row watch-row ${w.enabled ? "" : "off"}`}>
          <Icon name="eye" size={16} />
          <div className="wakeup-main">
            <div className="wakeup-name">{watchWhenText(w.when)} → {watchThenText(w.then)}</div>
            {w.note && <div className="wakeup-note sub">{w.note}</div>}
            <div className="wakeup-meta sub">
              {t("focus.watches.cooldown", { n: w.cooldown_minutes })}
              {w.once && ` · ${t("focus.watches.once")}`}
              {" · "}
              {w.fire_count ? t("focus.watches.fired", { n: w.fire_count, when: relTimeLong(w.last_fired_at) }) : t("focus.watches.notyet")}
            </div>
            <div className="wakeup-meta sub faint">{t(w.created_by === "operator" ? "focus.wakeups.by.operator" : "focus.wakeups.by.orchestrator")} · {relTime(w.created_at)}</div>
            {!w.enabled && w.stopped && <div className={`wakeup-meta ${w.stopped === "once" ? "sub" : "watch-stopped"}`}>{t(STOPPED.includes(w.stopped) ? `focus.watches.stopped.${w.stopped}` : "focus.watches.stopped.other")}</div>}
            {w.last_error && !w.stopped && <div className="wakeup-meta watch-stopped">{t("focus.watches.error", { error: w.last_error })}</div>}
          </div>
          <button className="iconbtn small quiet" onClick={() => void act(w, "toggle")} title={t(w.enabled ? "focus.watches.pause" : "focus.watches.resume")} aria-label={t(w.enabled ? "focus.watches.pause" : "focus.watches.resume")} aria-pressed={!w.enabled}>
            <Icon name={w.enabled ? "pause" : "play"} size={16} />
          </button>
          <button className="iconbtn small quiet" onClick={() => void act(w, "remove")} title={t("focus.watches.remove")} aria-label={t("focus.watches.remove")}>
            <Icon name="close" size={16} />
          </button>
        </div>
      ))}
      {adding && <WatchSheet projectId={projectId} providers={data?.providers ?? []} minCooldown={data?.min_cooldown_minutes ?? 1} onClose={() => setAdding(false)} toast={toast} />}
    </section>
  );
}

function WatchSheet({ projectId, providers, minCooldown, onClose, toast }: { projectId: string; providers: string[]; minCooldown: number; onClose: () => void; toast: (text: string) => void }) {
  const { project } = useProject(projectId);
  const { team, board, terminals } = useFocus(projectId);
  const [draft, setDraft] = useState<WatchDraft>(() => ({ ...emptyWatch(), provider: providers[0] ?? "" }));
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<WatchDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const staff = (team?.staff ?? []).filter((m) => !m.archived_at);
  const cli = staff.filter((m) => m.harness !== "daedalus");
  const running = (terminals?.terminals ?? []).filter((term) => term.status === "running");
  const tasks = (board?.tasks ?? []).filter((task) => task.status !== "done" && task.status !== "dropped");
  const orchestrated = !!project?.settings.orchestrator?.enabled;
  const request = watchBody(draft);
  const kind = draft.kind;
  const staffKind = kind.startsWith("staff_");

  async function save() {
    if (!request) return;
    setBusy(true);
    try {
      await api.post(watchesKey(projectId), request);
      toast(t("focus.watches.added"));
      invalidate(watchesKey(projectId));
      onClose();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const staffSelect = (id: string, value: string, onChange: (v: string) => void, anyone: boolean) => (
    <select id={id} className="field" value={value} onChange={(e) => onChange(e.target.value)}>
      {anyone ? <option value="">{t("focus.watch.anyone")}</option> : <option value="" disabled>{t("focus.watches.pick")}</option>}
      {staff.map((m) => <option key={m.id} value={m.name}>{m.name}</option>)}
    </select>
  );
  return (
    <Sheet title={t("focus.watches.new")} onClose={onClose} size="narrow" className="watch-sheet">
      <label className="field" htmlFor="watch-kind">{t("focus.watches.when")}</label>
      <select id="watch-kind" className="field" value={kind} onChange={(e) => set({ kind: e.target.value as WatchKind })}>
        {WATCH_KINDS.map((k) => <option key={k} value={k}>{t(`focus.watches.kind.${k}`)}</option>)}
      </select>
      {staffKind && (
        <>
          <label className="field" htmlFor="watch-staff">{t("focus.watches.staff")}</label>
          {staffSelect("watch-staff", draft.staff, (v) => set({ staff: v }), kind !== "staff_silent")}
        </>
      )}
      {kind === "staff_silent" && (
        <>
          <label className="field" htmlFor="watch-minutes">{t("focus.watches.minutes")}</label>
          <input id="watch-minutes" className="field" type="number" inputMode="numeric" min={5} value={draft.minutes} onChange={(e) => set({ minutes: e.target.value })} />
        </>
      )}
      {kind === "task_moved" && (
        <div className="grid2">
          <div>
            <label className="field" htmlFor="watch-task">{t("focus.watches.task")}</label>
            <select id="watch-task" className="field" value={draft.task} onChange={(e) => set({ task: e.target.value })}>
              <option value="">{t("focus.watch.anytask")}</option>
              {tasks.map((task) => <option key={task.id} value={task.id}>{task.title}</option>)}
            </select>
          </div>
          <div>
            <label className="field" htmlFor="watch-to">{t("focus.watches.to")}</label>
            <select id="watch-to" className="field" value={draft.to} onChange={(e) => set({ to: e.target.value })}>
              <option value="">{t("focus.watches.anycolumn")}</option>
              {TASK_STATUSES.map((status) => <option key={status} value={status}>{t(`board.col.${status}`)}</option>)}
            </select>
          </div>
        </div>
      )}
      {kind === "terminal_output" && (
        <>
          <label className="field" htmlFor="watch-terminal">{t("focus.watches.terminal")}</label>
          <select id="watch-terminal" className="field" value={draft.terminal} onChange={(e) => set({ terminal: e.target.value })}>
            <option value="" disabled>{t("focus.watches.pick")}</option>
            {running.map((term) => <option key={term.id} value={term.id}>{term.title || term.id}</option>)}
            {cli.map((m) => <option key={m.id} value={`staff:${m.name}`}>{t("focus.watches.terminal.of", { name: m.name })}</option>)}
          </select>
          <label className="field" htmlFor="watch-regex">{t("focus.watches.regex")}</label>
          <input id="watch-regex" className="field mono" maxLength={200} placeholder="FAILED|Error:" value={draft.regex} onChange={(e) => set({ regex: e.target.value })} />
        </>
      )}
      {kind === "git_commit" && (
        <div className="grid2">
          <div>
            <label className="field" htmlFor="watch-folder">{t("focus.watches.folder")}</label>
            <select id="watch-folder" className="field" value={draft.folder} onChange={(e) => set({ folder: e.target.value })}>
              <option value="">{t("focus.watches.primary")}</option>
              {(project?.folders ?? []).filter((f) => f.is_git).map((f) => <option key={f.id} value={f.id}>{f.label || f.path.split("/").pop()}</option>)}
            </select>
          </div>
          <div>
            <label className="field" htmlFor="watch-branch">{t("focus.watches.branch")}</label>
            <input id="watch-branch" className="field mono" placeholder="main" value={draft.branch} onChange={(e) => set({ branch: e.target.value })} />
          </div>
        </div>
      )}
      {(kind === "pr" || kind === "ci" || kind === "webhook") && (
        <>
          <label className="field" htmlFor="watch-provider">{t("focus.watches.provider")}</label>
          {providers.length === 0 ? <div className="sub form-hint">{t("focus.watches.noproviders")}</div> : (
            <select id="watch-provider" className="field" value={draft.provider} onChange={(e) => set({ provider: e.target.value })}>
              {providers.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          )}
          {kind === "webhook" ? (
            <>
              <label className="field" htmlFor="watch-hook-regex">{t("focus.watches.regex.payload")}</label>
              <input id="watch-hook-regex" className="field mono" maxLength={200} value={draft.regex} onChange={(e) => set({ regex: e.target.value })} />
            </>
          ) : (
            <div className="grid2">
              <div>
                <label className="field" htmlFor="watch-repo">{t("focus.watches.repo")}</label>
                <input id="watch-repo" className="field mono" placeholder="owner/repo" value={draft.repo} onChange={(e) => set({ repo: e.target.value })} />
              </div>
              <div>
                <label className="field" htmlFor="watch-conclusion">{t("focus.watches.conclusion")}</label>
                <input id="watch-conclusion" className="field mono" placeholder={kind === "pr" ? "merged" : "failure"} value={draft.conclusion} onChange={(e) => set({ conclusion: e.target.value })} />
              </div>
            </div>
          )}
        </>
      )}
      <label className="field">{t("focus.watches.then")}</label>
      <div className="chips">
        {WATCH_ACTIONS.filter((a) => a !== "wake" || orchestrated).map((a) => (
          <button key={a} className="chip select" aria-pressed={draft.action === a} onClick={() => set({ action: a })}>{t(`focus.watches.action.${a}`)}</button>
        ))}
      </div>
      {draft.action === "wake" && (
        <>
          <label className="field" htmlFor="watch-wake-note">{t("focus.wakeups.note")}</label>
          <input id="watch-wake-note" className="field" maxLength={300} placeholder={t("focus.wakeups.note.hint")} value={draft.wakeNote} onChange={(e) => set({ wakeNote: e.target.value })} />
        </>
      )}
      {draft.action === "tell" && (
        <>
          <label className="field" htmlFor="watch-tell-staff">{t("focus.watches.staff")}</label>
          {staffSelect("watch-tell-staff", draft.tellStaff, (v) => set({ tellStaff: v }), false)}
          <label className="field" htmlFor="watch-tell-text">{t("focus.watches.message")}</label>
          <textarea id="watch-tell-text" className="field" rows={2} maxLength={2000} value={draft.tellText} onChange={(e) => set({ tellText: e.target.value })} />
          <div className="chips">
            {TELL_TIMINGS.map((when) => <button key={when} className="chip select" aria-pressed={draft.tellWhen === when} onClick={() => set({ tellWhen: when })}>{t(`focus.watches.when.${when}`)}</button>)}
          </div>
        </>
      )}
      {draft.action === "notify" && (
        <>
          <label className="field" htmlFor="watch-title">{t("focus.watches.title.field")}</label>
          <input id="watch-title" className="field" maxLength={120} value={draft.title} onChange={(e) => set({ title: e.target.value })} />
          <label className="field" htmlFor="watch-text">{t("focus.watches.text")}</label>
          <textarea id="watch-text" className="field" rows={2} maxLength={1000} value={draft.text} onChange={(e) => set({ text: e.target.value })} />
          <div className="chips">
            {NOTIFY_LEVELS.map((level) => <button key={level} className="chip select" aria-pressed={draft.level === level} onClick={() => set({ level })}>{t(`focus.watches.level.${level}`)}</button>)}
          </div>
        </>
      )}
      <div className="grid2">
        <div>
          <label className="field" htmlFor="watch-cooldown">{t("focus.watches.cooldown.field")}</label>
          <input id="watch-cooldown" className="field" type="number" inputMode="numeric" min={minCooldown} value={draft.cooldown} onChange={(e) => set({ cooldown: e.target.value })} />
        </div>
        <label className="toggle-row watch-once">
          <input type="checkbox" checked={draft.once} onChange={(e) => set({ once: e.target.checked })} />
          <span>{t("focus.watches.once.field")}</span>
        </label>
      </div>
      <label className="field" htmlFor="watch-note">{t("focus.watches.note")}</label>
      <input id="watch-note" className="field" maxLength={300} value={draft.note} onChange={(e) => set({ note: e.target.value })} />
      {request && <div className="sub preview-line">{watchWhenText(request.when)} → {watchThenText(request.then)}</div>}
      <div className="sub form-hint">{t("focus.watches.hint")}</div>
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" disabled={busy || !request} onClick={() => void save()}>{t("focus.watches.set")}</button>
      </div>
    </Sheet>
  );
}
