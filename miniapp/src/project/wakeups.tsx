// The orchestrator's wake-ups and the project's watches. Wake-ups are the alarms it set itself and the
// ones the operator left it; watches are "when X happens, do Y". Each can be cancelled from its row,
// a watch switched off and on, and a new one of either set from a sheet. One component serves the
// centre of focus mode and the right panel's tab (`compact`), like every page of a project.

import { useRef, useState } from "react";
import { api, ApiError, type ProjectWatch, type Wakeup, type WatchList } from "../api";
import { Skeleton } from "../ui/components";
import { Sheet } from "../ui/dialogs";
import { absTime, describeCron, relTime, relTimeLong, untilShort } from "../format";
import { t } from "../i18n";
import { Icon } from "../icons";
import { PageHeader } from "../shell";
import { invalidate, useOffline, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { useFocus, useProject, wakeupsKey, watchesKey } from "./data";
import { NOTIFY_LEVELS, TASK_STATUSES, TELL_TIMINGS, WATCH_ACTIONS, WATCH_KINDS, emptyWatch, watchBody, watchThenText, watchWhenText, type WatchDraft, type WatchKind } from "./watchmodel";
import { WAKEUP_WHENS, wakeupBody, wakeupWhen, type WakeupDraft, type WakeupWhen } from "./wakeupmodel";

const enc = encodeURIComponent;

type Props = { projectId: string; back?: string | null; compact?: boolean; toast?: (text: string) => void };

export function WakeupsPage({ projectId, back, compact, toast }: Props) {
  const offline = useOffline();
  const { project } = useProject(projectId);
  const { data, error, refresh } = useQuery<{ wakeups: Wakeup[]; max: number; collection_revision?: number }>(wakeupsKey(projectId), { pollMs: 30000, staleMs: 5000 });
  const [adding, setAdding] = useState(false);
  const pendingDeletes = useRef<Record<string, { fingerprint: string; id: string }>>({});
  const say = toast ?? (() => undefined);
  const enabled = !!project?.settings.orchestrator?.enabled;
  const list = data?.wakeups ?? [];

  async function cancel(w: Wakeup) {
    if (offline || error || !Number.isInteger(data?.collection_revision) || !Number.isInteger(w.schedule_revision)) return;
    try {
      const body = { expected_collection_revision: data!.collection_revision!, expected_schedule_revision: w.schedule_revision! };
      const fingerprint = JSON.stringify(body);
      if (pendingDeletes.current[w.id]?.fingerprint !== fingerprint) pendingDeletes.current[w.id] = { fingerprint, id: crypto.randomUUID() };
      await api.request("DELETE", `${wakeupsKey(projectId)}/${enc(w.id)}`, { ...body, client_operation_id: pendingDeletes.current[w.id].id });
      delete pendingDeletes.current[w.id];
      say(t("focus.wakeups.cancelled"));
      invalidate(wakeupsKey(projectId));
    } catch (e) {
      say(errorText(e));
    }
  }

  const body = (
    <div className="wakeups">
      {(offline || error) && <div className="result-warning" role="status">{t("focus.connectionRequired")}</div>}
      <section className="wakeup-section" aria-label={t("focus.wakeups.title")}>
        <div className="wakeup-section-head">
          <span className="wakeup-section-title">{t("focus.wakeups.title")}</span>
          <span className="grow" />
          {enabled && <button className="btn small" disabled={offline || !!error} onClick={() => setAdding(true)}><Icon name="plus" size={14} /> {t("focus.wakeups.add")}</button>}
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
            <button className="iconbtn small quiet" disabled={offline || !!error || !Number.isInteger(data?.collection_revision) || !Number.isInteger(w.schedule_revision)} onClick={() => void cancel(w)} title={t("focus.wakeups.cancel")} aria-label={t("focus.wakeups.cancel")}>
              <Icon name="close" size={16} />
            </button>
          </div>
        ))}
      </section>
      <WatchesSection projectId={projectId} toast={say} />
      {adding && <WakeupSheet projectId={projectId} collectionRevision={data?.collection_revision} unverified={!!error} onClose={() => setAdding(false)} toast={say} />}
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

function WakeupSheet({ projectId, collectionRevision, unverified, onClose, toast }: { projectId: string; collectionRevision?: number; unverified: boolean; onClose: () => void; toast: (text: string) => void }) {
  const offline = useOffline();
  const soon = new Date(Date.now() + 3600000);
  const [draft, setDraft] = useState<WakeupDraft>({ note: "", when: "in", minutes: "30", date: soon.toLocaleDateString("en-CA"), time: soon.toTimeString().slice(0, 5), cron: "" });
  const [busy, setBusy] = useState(false);
  const [approvalClock] = useState(() => Date.now());
  const pending = useRef<{ fingerprint: string; id: string } | null>(null);
  const set = (patch: Partial<WakeupDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const request = wakeupBody(draft);
  // The moment is previewed before the note is written, so a time that will not do shows at once.
  const timing = wakeupBody({ ...draft, note: "·" });
  const preview = timing?.cron ? describeCron(timing.cron) : timing?.at ? t("fmt.once", { when: absTime(timing.at) }) : timing?.in_minutes ? untilShort(Date.now() + timing.in_minutes * 60000) : "";

  async function save() {
    if (!request || offline || unverified || !Number.isInteger(collectionRevision)) return;
    setBusy(true);
    try {
      const due = request.at ? Date.parse(request.at) : request.in_minutes ? approvalClock + request.in_minutes * 60000 : 0;
      const expiresAt = new Date(Math.max(approvalClock + 30 * 86400000,
        Number.isFinite(due) ? due + 2 * 86400000 : 0)).toISOString();
      const fields = { ...request, expires_at: expiresAt, expected_collection_revision: collectionRevision! };
      const fingerprint = JSON.stringify(fields);
      if (pending.current?.fingerprint !== fingerprint) pending.current = { fingerprint, id: crypto.randomUUID() };
      await api.post(wakeupsKey(projectId), { ...fields, client_operation_id: pending.current.id });
      pending.current = null;
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
        <button className="btn primary" disabled={busy || !request || offline || unverified || !Number.isInteger(collectionRevision)} onClick={() => void save()}>{t("focus.wakeups.set")}</button>
      </div>
    </Sheet>
  );
}

// ── watches ──────────────────────────────────────────────────────────────────────────────────

/** Why a watch switched itself off, as the host names it. */
const STOPPED = ["once", "budget", "pattern", "slow_pattern", "expired", "needs_approval"];
const DELIVERY = ["pending", "reconciling", "delivered", "failed", "cancelled"];

type WatchWrite = { method: "POST" | "PATCH" | "DELETE"; path: string; body: Record<string, unknown>; label: string };
type WriteWatch = (method: WatchWrite["method"], path: string, fields: Record<string, unknown>, label: string, watch?: ProjectWatch, onSuccess?: () => void) => Promise<boolean>;

function savedWatchWrite(projectId: string): WatchWrite | null {
  const base = watchesKey(projectId);
  try {
    const value = JSON.parse(sessionStorage.getItem(`daedalus.watch.write.${projectId}`) ?? "null");
    const suffix = typeof value?.path === "string" && value.path.startsWith(base) ? value.path.slice(base.length) : null;
    return (suffix === "" || (typeof suffix === "string" && /^\/[^/]+$/.test(suffix)))
      && ["POST", "PATCH", "DELETE"].includes(value.method)
      && typeof value.body?.client_operation_id === "string" && typeof value.label === "string"
      ? value as WatchWrite : null;
  } catch { return null; }
}

function useWatchWrites(projectId: string, data: WatchList | undefined, failed: boolean, refresh: () => void, toast: (text: string) => void) {
  const offline = useOffline();
  const [pending, setPending] = useState<WatchWrite | null>(() => savedWatchWrite(projectId));
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const known = Number.isInteger(data?.collection_revision) && Number.isInteger(data?.project_entity_revision) && (data?.project_entity_revision ?? 0) > 0;
  const ready = !offline && !failed && known && !pending && !conflict && !busy && !needsRefresh;

  function remember(value: WatchWrite | null) {
    setPending(value);
    try {
      if (value) sessionStorage.setItem(`daedalus.watch.write.${projectId}`, JSON.stringify(value));
      else sessionStorage.removeItem(`daedalus.watch.write.${projectId}`);
    } catch { /* the mounted form retains the same request until this page closes */ }
  }

  async function run(intent: WatchWrite, onSuccess?: () => void): Promise<boolean> {
    if (offline || failed || busy) return false;
    setBusy(true);
    try {
      const receipt = await api.request<{ receipt_id: string }>(intent.method, intent.path, intent.body);
      if (!receipt || typeof receipt.receipt_id !== "string") throw new Error(t("focus.watches.badReceipt"));
      remember(null);
      setConflict(false);
      setNeedsRefresh(true);
      invalidate(watchesKey(projectId));
      onSuccess?.();
      toast(intent.label);
      const fresh = await api.get<WatchList>(watchesKey(projectId));
      if (!Number.isInteger(fresh.collection_revision) || !Number.isInteger(fresh.project_entity_revision))
        throw new Error(t("focus.watches.unverified"));
      await refresh();
      setNeedsRefresh(false);
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        remember(null);
        setConflict(true);
        refresh();
      } else if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429) {
        remember(null);
      }
      toast(errorText(error));
      return false;
    } finally { setBusy(false); }
  }

  const write: WriteWatch = (method, path, fields, label, watch, onSuccess) => {
    if (!ready || !data) return Promise.resolve(false);
    if (watch && !Number.isInteger(watch.condition_revision)) return Promise.resolve(false);
    const body = { ...fields, client_operation_id: crypto.randomUUID(),
      ...(method === "POST" ? { expected_collection_revision: data.collection_revision }
        : { expected_entity_revision: data.project_entity_revision, expected_condition_revision: watch?.condition_revision }) };
    const intent: WatchWrite = { method, path, body, label };
    remember(intent);
    return run(intent, onSuccess);
  };

  async function review() {
    if (offline || busy) return;
    setBusy(true);
    try {
      const fresh = await api.get<WatchList>(watchesKey(projectId));
      if (!Number.isInteger(fresh.collection_revision) || !Number.isInteger(fresh.project_entity_revision)) return;
      await refresh();
      setConflict(false);
      setNeedsRefresh(false);
    } catch (error) { toast(errorText(error)); }
    finally { setBusy(false); }
  }

  return { write, retry: () => pending ? run(pending) : Promise.resolve(false), review, pending, conflict, busy, needsRefresh, ready, offline, known };
}

function WatchesSection({ projectId, toast }: { projectId: string; toast: (text: string) => void }) {
  const offline = useOffline();
  const { data, error, refresh } = useQuery<WatchList>(watchesKey(projectId), { pollMs: 30000, staleMs: 5000 });
  const [adding, setAdding] = useState(false);
  const list = data?.watches ?? [];
  const writes = useWatchWrites(projectId, data, !!error, refresh, toast);

  async function act(w: ProjectWatch, change: "toggle" | "remove") {
    if (!writes.ready || !Number.isInteger(w.condition_revision)) return;
    if (change === "toggle" && (!w.authority_state || (w.authority_state === "capability_unavailable" && !w.enabled))) return;
    const needsApproval = w.authority_state === "needs_approval" || w.stopped === "needs_approval";
    if (change === "toggle" && needsApproval && !(await confirmAsync(t("focus.watches.approval.title"), {
      body: t("focus.watches.approval.preview", { condition: watchWhenText(w.when), action: watchThenText(w.then),
        cooldown: w.cooldown_minutes, until: w.deadline_at ? absTime(w.deadline_at) : t("focus.watches.deadline.none") }),
      action: t("focus.watches.approval.action"),
    }))) return;
    await writes.write(change === "remove" ? "DELETE" : "PATCH", `${watchesKey(projectId)}/${enc(w.id)}`,
      change === "remove" ? {} : { enabled: needsApproval ? true : !w.enabled }, t(change === "remove" ? "focus.watches.removed" : needsApproval ? "focus.watches.approval.recorded" : w.enabled ? "focus.watches.paused" : "focus.watches.resumed"), w);
  }

  return (
    <section className="wakeup-section" aria-label={t("focus.watches.title")}>
      {(offline || error) && <div className="result-warning" role="status">{t("focus.connectionRequired")}</div>}
      {!error && data && !writes.known && <div className="result-warning" role="status">{t("focus.watches.unverified")}</div>}
      {writes.pending && <div className="result-warning" role="status">{t("focus.watches.pendingWrite", { action: writes.pending.label })} <button type="button" className="linkbtn" disabled={offline || !!error || writes.busy} onClick={() => void writes.retry()}>{t("project.write.retry")}</button></div>}
      {writes.conflict && <div className="result-warning" role="status">{t("focus.watches.conflict")} <button type="button" className="linkbtn" disabled={offline || writes.busy} onClick={() => void writes.review()}>{t("project.write.review")}</button></div>}
      {writes.needsRefresh && <div className="result-warning" role="status">{t("focus.watches.unverified")} <button type="button" className="linkbtn" disabled={offline || writes.busy} onClick={() => void writes.review()}>{t("common.retry")}</button></div>}
      <div className="wakeup-section-head">
        <span className="wakeup-section-title">{t("focus.watches.title")}</span>
        <span className="grow" />
        <button className="btn small" disabled={!writes.ready} onClick={() => setAdding(true)}><Icon name="plus" size={14} /> {t("focus.watches.add")}</button>
      </div>
      {!data && !error && <Skeleton rows={2} />}
      {error && !data && <div className="empty"><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
      {data && list.length === 0 && <div className="empty calm">{t("focus.watches.empty")}</div>}
      {list.map((w) => (
        <div key={w.id} className={`wakeup-row watch-row ${w.enabled && w.authority_state === "current" ? "" : "off"}`}>
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
            {w.deadline_at && <div className="wakeup-meta sub">{t("focus.watches.until", { when: absTime(w.deadline_at) })}</div>}
            {!w.enabled && w.stopped && <div className={`wakeup-meta ${w.stopped === "once" ? "sub" : "watch-stopped"}`}>{t(STOPPED.includes(w.stopped) ? `focus.watches.stopped.${w.stopped}` : "focus.watches.stopped.other")}</div>}
            {w.authority_state === "needs_approval" && w.stopped !== "needs_approval" && <div className="wakeup-meta watch-stopped" role="status">{t("focus.watches.authorityBlocked")}</div>}
            {w.authority_state === "capability_unavailable" && <div className="wakeup-meta watch-stopped" role="status">{t("focus.watches.capabilityUnavailable")}</div>}
            {!w.authority_state && <div className="wakeup-meta watch-stopped" role="status">{t("focus.watches.authorityUnknown")}</div>}
            {w.last_error && !w.stopped && <div className="wakeup-meta watch-stopped">{t("focus.watches.error", { error: w.last_error })}</div>}
            {w.latest_delivery && <div className={`wakeup-meta ${w.latest_delivery.status === "failed" ? "watch-stopped" : "sub"}`} role="status">
              {t(DELIVERY.includes(w.latest_delivery.status) ? `focus.watches.delivery.${w.latest_delivery.status}` : "focus.watches.delivery.unknown")}
              {w.latest_delivery.last_error && <details><summary>{t("focus.watches.delivery.reason")}</summary>{w.latest_delivery.last_error}</details>}
            </div>}
          </div>
          <button className="iconbtn small quiet" disabled={!writes.ready || !Number.isInteger(w.condition_revision) || !w.authority_state || (w.authority_state === "capability_unavailable" && !w.enabled)} onClick={() => void act(w, "toggle")} title={t(w.authority_state === "needs_approval" || w.stopped === "needs_approval" ? "focus.watches.approval.action" : w.enabled ? "focus.watches.pause" : "focus.watches.resume")} aria-label={t(w.authority_state === "needs_approval" || w.stopped === "needs_approval" ? "focus.watches.approval.action" : w.enabled ? "focus.watches.pause" : "focus.watches.resume")} aria-pressed={!w.enabled || w.authority_state !== "current"}>
            <Icon name={w.enabled && w.authority_state !== "needs_approval" ? "pause" : "play"} size={16} />
          </button>
          <button className="iconbtn small quiet" disabled={!writes.ready || !Number.isInteger(w.condition_revision)} onClick={() => void act(w, "remove")} title={t("focus.watches.remove")} aria-label={t("focus.watches.remove")}>
            <Icon name="close" size={16} />
          </button>
        </div>
      ))}
      {adding && <WatchSheet projectId={projectId} providers={data?.providers ?? []} minCooldown={data?.min_cooldown_minutes ?? 1} unverified={!!error} onClose={() => setAdding(false)} write={writes.write} canWrite={writes.ready} pending={writes.pending} retry={async () => { if (await writes.retry()) setAdding(false); }} conflict={writes.conflict} review={writes.review} needsRefresh={writes.needsRefresh} />}
    </section>
  );
}

function WatchSheet({ projectId, providers, minCooldown, unverified, onClose, write, canWrite, pending, retry, conflict, review, needsRefresh }: { projectId: string; providers: string[]; minCooldown: number; unverified: boolean; onClose: () => void; write: WriteWatch; canWrite: boolean; pending: WatchWrite | null; retry: () => Promise<void>; conflict: boolean; review: () => Promise<void>; needsRefresh: boolean }) {
  const offline = useOffline();
  const { project } = useProject(projectId);
  const { team, board, terminals } = useFocus(projectId);
  const [draft, setDraft] = useState<WatchDraft>(() => ({ ...emptyWatch(), provider: providers[0] ?? "" }));
  const [deadlineChoice, setDeadlineChoice] = useState("");
  const [deadlineAt, setDeadlineAt] = useState<string | null>(null);
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
    if (!request || offline || unverified || !canWrite) return;
    setBusy(true);
    await write("POST", watchesKey(projectId), { ...request, deadline_at: deadlineAt }, t("focus.watches.added"), undefined, onClose);
    setBusy(false);
  }
  const staffSelect = (id: string, value: string, onChange: (v: string) => void, anyone: boolean) => (
    <select id={id} className="field" value={value} onChange={(e) => onChange(e.target.value)}>
      {anyone ? <option value="">{t("focus.watch.anyone")}</option> : <option value="" disabled>{t("focus.watches.pick")}</option>}
      {staff.map((m) => <option key={m.id} value={m.name}>{m.name}</option>)}
    </select>
  );
  return (
    <Sheet title={t("focus.watches.new")} onClose={onClose} size="narrow" className="watch-sheet">
      {pending && <div className="result-warning" role="status">{t("focus.watches.pendingWrite", { action: pending.label })} <button type="button" className="linkbtn" disabled={offline || unverified || busy} onClick={() => void retry()}>{t("project.write.retry")}</button></div>}
      {conflict && <div className="result-warning" role="status">{t("focus.watches.conflict")} <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => void review()}>{t("project.write.review")}</button></div>}
      {needsRefresh && <div className="result-warning" role="status">{t("focus.watches.unverified")} <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => void review()}>{t("common.retry")}</button></div>}
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
            {running.map((term) => <option key={term.id} value={term.id}>{term.title || t("term.untitled")}</option>)}
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
      <label className="field" htmlFor="watch-deadline">{t("focus.watches.deadline")}</label>
      <select id="watch-deadline" className="field" value={deadlineChoice} onChange={(e) => {
        const choice = e.target.value;
        setDeadlineChoice(choice);
        setDeadlineAt(choice ? new Date(Date.now() + Number(choice) * 60 * 60 * 1000).toISOString() : null);
      }}>
        <option value="">{t("focus.watches.deadline.none")}</option>
        <option value="24">{t("focus.watches.deadline.day")}</option>
        <option value="168">{t("focus.watches.deadline.week")}</option>
        <option value="720">{t("focus.watches.deadline.month")}</option>
      </select>
      {deadlineAt && <div className="sub form-hint">{t("focus.watches.until", { when: absTime(deadlineAt) })}</div>}
      <label className="field" htmlFor="watch-note">{t("focus.watches.note")}</label>
      <input id="watch-note" className="field" maxLength={300} value={draft.note} onChange={(e) => set({ note: e.target.value })} />
      {request && <div className="sub preview-line">{watchWhenText(request.when)} → {watchThenText(request.then)}</div>}
      <div className="sub form-hint">{t("focus.watches.hint")}</div>
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" disabled={busy || !request || offline || unverified || !canWrite} onClick={() => void save()}>{t("focus.watches.set")}</button>
      </div>
    </Sheet>
  );
}
