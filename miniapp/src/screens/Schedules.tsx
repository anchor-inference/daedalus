import { useMemo, useRef, useState } from "react";
import { api, ApiError, Schedule } from "../api";
import { Pill, Skeleton } from "../ui/components";
import { OverflowMenu, Sheet } from "../ui/dialogs";
import { absTime, cronFor, describeCron, describeSchedule, relTime, untilShort } from "../format";
import { Icon } from "../icons";
import { navigate, pathFor } from "../router";
import { PageHeader, screenTitle } from "../ui/index";
import { invalidate, useOffline, useQuery } from "../store";
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

type Cycle = { id: string; due_at: string; kind: string; effect_state: string | null; action_state: string; effect_error: string | null };
type CycleHistory = { schedule_revision: number; authority_state: string; collection_revision: number | null; scope: { kind: string; id: string }; cycles: Cycle[] };
type ScheduleProposal = { id: string; name: string; prompt: string; cron: string | null; run_at: string | null; kind: Schedule["kind"]; next_run_at: string; file_count: number; files: { name: string; size: number; digest: string }[]; legacy_file_review_required: boolean; source_session_id: string | null; source_project_id: string | null; proposal_revision: number; request_digest: string; status: "pending" | "withdrawn" | "accepted"; created_at: string };
type ProposalList = { entries: ScheduleProposal[]; collection_revisions: Record<string, number> };

function scheduleRevision(s: Schedule): number {
  if (!Number.isInteger(s.schedule_revision)) throw new Error(t("sched.approval.refresh"));
  return s.schedule_revision!;
}

export function SchedulesScreen({ toast, onOpen, selected }: { toast: (t: string) => void; onOpen: (id: string) => void; selected?: string | null }) {
  const { data: items, error, loading, refresh } = useQuery<Schedule[]>("/api/schedules", { pollMs: 30000, staleMs: 5000 });
  const titles = useSessionTitles();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Schedule | null>(null);
  const pending = useRef<Record<string, { fingerprint: string; id: string }>>({});
  const operationId = (key: string, body: unknown) => {
    const fingerprint = JSON.stringify(body);
    if (pending.current[key]?.fingerprint !== fingerprint) pending.current[key] = { fingerprint, id: crypto.randomUUID() };
    return pending.current[key].id;
  };
  const history = async (s: Schedule) => api.get<CycleHistory>(`/api/recurring/${encodeURIComponent(s.id)}/cycles`);
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
      const current = await history(s);
      if (current.collection_revision == null) throw new Error(t("sched.approval.refresh"));
      const body = { expected_schedule_revision: scheduleRevision(s), expected_collection_revision: current.collection_revision };
      await api.post(`/api/recurring/${encodeURIComponent(s.id)}/remove`, { ...body, client_operation_id: operationId(`${s.id}:remove`, body) });
      delete pending.current[`${s.id}:remove`];
      if (selected === s.id) navigate(pathFor("schedules"), { replace: true });
      reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { delete pending.current[`${s.id}:remove`]; reload(); }
      toast(errorText(e));
    }
  }
  async function runNow(s: Schedule) {
    try {
      const current = await history(s);
      if (current.collection_revision == null) throw new Error(t("sched.approval.refresh"));
      const body = { expected_schedule_revision: scheduleRevision(s), expected_collection_revision: current.collection_revision };
      await api.post(`/api/recurring/${encodeURIComponent(s.id)}/run`, { ...body, client_operation_id: operationId(`${s.id}:run`, body) });
      delete pending.current[`${s.id}:run`];
      toast(t("sched.queued"));
      reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { delete pending.current[`${s.id}:run`]; reload(); }
      toast(errorText(e));
    }
  }
  async function setEnabled(s: Schedule, enabled: boolean) {
    try {
      const current = await history(s);
      if (current.collection_revision == null) throw new Error(t("sched.approval.refresh"));
      const body = { enabled, expected_schedule_revision: scheduleRevision(s), expected_collection_revision: current.collection_revision };
      await api.patch(`/api/recurring/${encodeURIComponent(s.id)}`, { ...body, client_operation_id: operationId(`${s.id}:enabled`, body) });
      delete pending.current[`${s.id}:enabled`];
      toast(t(enabled ? "sched.resumed" : "sched.pausedtoast"));
      reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { delete pending.current[`${s.id}:enabled`]; reload(); }
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
            {s.authority_state === "needs_approval" && <><span className="sep">·</span><span>{t("sched.approval.needed")}</span></>}
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
        <PendingProposals toast={toast} onAccepted={reload} />
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
          <RecurringInspection schedule={open} toast={toast} reload={reload} />
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

function PendingProposals({ toast, onAccepted }: { toast: (text: string) => void; onAccepted: () => void }) {
  const offline = useOffline();
  const { data, error, refresh } = useQuery<ProposalList>("/api/recurring/proposals", { pollMs: 30000, staleMs: 5000 });
  const [busy, setBusy] = useState<string | null>(null);
  const pending = useRef<Record<string, { fingerprint: string; id: string }>>({});
  const [approvalClock] = useState(() => Date.now());
  const entries = data?.entries.filter((item) => item.status === "pending") ?? [];
  const expiresFor = (item: ScheduleProposal) => {
    const due = Date.parse(item.next_run_at);
    return new Date(Math.max(approvalClock + 30 * 86400000,
      !item.cron && Number.isFinite(due) ? due + 86400000 : 0)).toISOString();
  };
  async function decide(item: ScheduleProposal, action: "accept" | "withdraw" | "review-files") {
    const scope = item.source_project_id ? `project:${item.source_project_id}` : "global:global";
    const revision = data?.collection_revisions[scope];
    if (!Number.isInteger(revision) || error || offline) return;
    const body = { request_digest: item.request_digest, expected_proposal_revision: item.proposal_revision,
      expected_collection_revision: revision!, ...(action === "accept" ? { expires_at: expiresFor(item) } : {}) };
    const key = `${item.id}:${action}`;
    const fingerprint = JSON.stringify(body);
    if (pending.current[key]?.fingerprint !== fingerprint) pending.current[key] = { fingerprint, id: crypto.randomUUID() };
    setBusy(item.id);
    try {
      await api.post(`/api/recurring/proposals/${encodeURIComponent(item.id)}/${action}`, { ...body, client_operation_id: pending.current[key].id });
      delete pending.current[key];
      refresh(); onAccepted();
      toast(t(action === "accept" ? "sched.proposal.accepted" : action === "review-files" ? "sched.proposal.reviewed" : "sched.proposal.withdrawn"));
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { delete pending.current[key]; refresh(); }
      toast(errorText(e));
    } finally { setBusy(null); }
  }
  return <section aria-label={t("sched.proposal.title")}>
    <div className="section-title">{t("sched.proposal.title")} <span className="n">{entries.length}</span></div>
    {error && <div className="result-warning" role="status">{t("sched.proposal.readfailed")} <button className="btn small" onClick={refresh}>{t("common.retry")}</button></div>}
    {entries.map((item) => <div key={item.id} className="erow schedule">
      <div className="grow">
        <b>{item.name}</b> <span className="sub">{t(`sched.kind.${item.kind}`)}</span>
        <div className="sub">{item.cron ? item.cron : absTime(item.next_run_at)}</div>
        <div className="sub">{t("sched.approval.expires", { at: absTime(expiresFor(item)) })}</div>
        <div className="proposal-text">{item.prompt}</div>
        {item.file_count > 0 && <div className="sub">{t("sched.proposal.files", { n: item.file_count })}</div>}
        {item.legacy_file_review_required && <div className="result-warning">{t("sched.proposal.legacyfiles")}</div>}
        {item.files.map((file) => <div className="sub" key={`${file.name}:${file.digest}`}>{file.name} · {file.size} B</div>)}
        {/* Which session asked and the content hashes pinned for approval are for checking, not reading. */}
        <details><summary>{t("common.details")}</summary>
          <div className="sub mono">{t("sched.proposal.source", { id: item.source_session_id ?? "—" })} · {t("sched.proposal.revision", { n: item.proposal_revision })}</div>
          {item.files.map((file) => <div className="sub mono" key={`${file.name}:${file.digest}`}>{file.name} · SHA-256 {file.digest}</div>)}
        </details>
      </div>
      <div className="row-actions">
        <button className="btn small" disabled={!!busy || !!error || offline || !data?.collection_revisions[item.source_project_id ? `project:${item.source_project_id}` : "global:global"]} onClick={() => void decide(item, "withdraw")}>{t("sched.proposal.withdraw")}</button>
        {item.legacy_file_review_required && <button className="btn small" disabled={!!busy || !!error || offline || !data?.collection_revisions[item.source_project_id ? `project:${item.source_project_id}` : "global:global"]} onClick={() => void decide(item, "review-files")}>{t("sched.proposal.reviewfiles")}</button>}
        <button className="btn small primary" disabled={item.legacy_file_review_required || !!busy || !!error || offline || !data?.collection_revisions[item.source_project_id ? `project:${item.source_project_id}` : "global:global"]} onClick={() => void decide(item, "accept")}>{t("sched.proposal.accept")}</button>
      </div>
    </div>)}
  </section>;
}

function RecurringInspection({ schedule, toast, reload }: { schedule: Schedule; toast: (text: string) => void; reload: () => void }) {
  const path = `/api/recurring/${encodeURIComponent(schedule.id)}/cycles`;
  const { data, error, refresh } = useQuery<CycleHistory>(path, { pollMs: 30000, staleMs: 5000 });
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [expiresAt] = useState(() => new Date(Date.now() + 30 * 86400000).toISOString());
  const pending = useRef<{ fingerprint: string; id: string } | null>(null);
  const operationId = (body: unknown) => {
    const fingerprint = JSON.stringify(body);
    if (pending.current?.fingerprint !== fingerprint) pending.current = { fingerprint, id: crypto.randomUUID() };
    return pending.current.id;
  };
  async function approve() {
    if (!data?.collection_revision) return;
    const body = { expires_at: expiresAt, expected_schedule_revision: data.schedule_revision, expected_collection_revision: data.collection_revision };
    setBusy(true);
    try {
      await api.post(`/api/recurring/${encodeURIComponent(schedule.id)}/approve`, { ...body, client_operation_id: operationId(body) });
      pending.current = null;
      refresh(); reload();
      toast(t("sched.approval.saved"));
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { pending.current = null; refresh(); reload(); }
      toast(errorText(e));
    } finally { setBusy(false); }
  }
  async function reconcile(cycle: Cycle, outcome: "delivered" | "not_delivered") {
    if (!data?.collection_revision || !reason.trim()) return;
    const body = { outcome, reason: reason.trim(), expected_collection_revision: data.collection_revision };
    setBusy(true);
    try {
      await api.post(`/api/recurring/${encodeURIComponent(schedule.id)}/cycles/${encodeURIComponent(cycle.id)}/reconcile`, { ...body, client_operation_id: operationId({ cycle_id: cycle.id, ...body }) });
      pending.current = null;
      setReason(""); refresh(); reload();
      toast(t("sched.cycle.reconciled"));
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { pending.current = null; refresh(); reload(); }
      toast(errorText(e));
    } finally { setBusy(false); }
  }
  return <section className="sheet-section">
    <div className="sheet-section-title">{t("sched.approval.title")}</div>
    {error && <div className="sub">{t("sched.approval.readfailed")}</div>}
    {data && <>
      <div className="kv"><span>{t("sched.approval.state")}</span><b>{t(data.authority_state === "current" ? "sched.approval.current" : "sched.approval.needed")}</b></div>
      {data.authority_state === "needs_approval" && <>
        <div className="sub">{t("sched.approval.expires", { at: absTime(expiresAt) })}</div>
        <button className="btn primary" disabled={busy} onClick={approve}>{t("sched.approval.action")}</button>
      </>}
      <div className="sheet-section-title">{t("sched.cycle.title")}</div>
      {data.cycles.length === 0 && <div className="sub">{t("sched.cycle.empty")}</div>}
      {data.cycles.map((cycle) => <div key={cycle.id} className="schedule-cycle">
        <div className="kv"><span>{absTime(cycle.due_at)}</span><b>{t(`sched.cycle.${cycle.effect_state === "unknown" ? "unknown" : cycle.effect_state === "completed" ? "completed" : cycle.effect_state === "failed" || cycle.effect_state === "cancelled" ? "failed" : cycle.action_state === "skipped" ? "skipped" : "pending"}`)}</b></div>
        {cycle.effect_state === "unknown" && <>
          <div className="sub">{t("sched.cycle.unknown.hint")}</div>
          <input className="field" value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("sched.cycle.reason")} />
          <div className="row-actions">
            <button className="btn" disabled={busy || !reason.trim()} onClick={() => reconcile(cycle, "delivered")}>{t("sched.cycle.confirmed")}</button>
            <button className="btn" disabled={busy || !reason.trim()} onClick={() => reconcile(cycle, "not_delivered")}>{t("sched.cycle.notdelivered")}</button>
          </div>
        </>}
      </div>)}
    </>}
  </section>;
}

type When = "once" | "daily" | "weekdays" | "weekly" | "hours" | "cron";
/** Monday first, the way a week is picked here; the cron field counts from Sunday. */
const DAYS = [1, 2, 3, 4, 5, 6, 0];

/** When, in the reader's own clock: a moment, a daily or weekly time, or every few hours; cron stays for the rest. */
function ScheduleForm({ existing, onClose, onSaved, toast }: { existing?: Schedule; onClose: () => void; onSaved: () => void; toast: (t: string) => void }) {
  const [name, setName] = useState(existing?.name ?? "");
  const [kind, setKind] = useState<Schedule["kind"]>(existing?.kind ?? "agent");
  const [prompt, setPrompt] = useState(existing?.prompt ?? "");
  const [when, setWhen] = useState<When>(existing?.cron ? "cron" : "once");
  const [date, setDate] = useState(() => (existing?.run_at ? new Date(existing.run_at) : new Date(Date.now() + 3600000)).toLocaleDateString("en-CA"));
  const [time, setTime] = useState(() => (existing?.run_at ? new Date(existing.run_at) : new Date(Date.now() + 3600000)).toTimeString().slice(0, 5));
  const [days, setDays] = useState<number[]>([1, 3, 5]);
  const [every, setEvery] = useState("2");
  const [cron, setCron] = useState(existing?.cron ?? "");
  const [busy, setBusy] = useState(false);
  const [expiresAt] = useState(() => new Date(Date.now() + 30 * 86400000).toISOString());
  const pending = useRef<{ fingerprint: string; body: Record<string, unknown>; id: string; approvalId?: string } | null>(null);
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
      const fields = { name: name.trim(), prompt: prompt.trim(), cron: built.cron, run_at: built.run_at };
      const fingerprint = JSON.stringify({ ...fields, kind, schedule: existing?.id, expiresAt });
      if (pending.current?.fingerprint !== fingerprint) {
        await api.post("/api/recurring/preview", { ...fields, kind, project_id: existing?.project_id ?? null, target_session: existing?.target_session ?? null });
        const revision = existing
          ? (await api.get<CycleHistory>(`/api/recurring/${encodeURIComponent(existing.id)}/cycles`)).collection_revision
          : (await api.get<{ global_collection_revision: number | null }>("/api/recurring/overview")).global_collection_revision;
        if (!revision) throw new Error(t("sched.approval.refresh"));
        pending.current = { fingerprint, body: { ...fields, expected_collection_revision: revision,
          ...(existing ? { expected_schedule_revision: scheduleRevision(existing) } : { kind, project_id: null, target_session: null, expires_at: expiresAt }) }, id: crypto.randomUUID() };
      }
      const intent = pending.current;
      if (existing) {
        const changed = await api.patch<{ entity_revision: number; schedule_revision: number }>(`/api/recurring/${encodeURIComponent(existing.id)}`, { ...intent.body, client_operation_id: intent.id });
        if (!intent.approvalId) intent.approvalId = crypto.randomUUID();
        await api.post(`/api/recurring/${encodeURIComponent(existing.id)}/approve`, {
          expires_at: expiresAt, expected_collection_revision: changed.entity_revision,
          expected_schedule_revision: changed.schedule_revision, client_operation_id: intent.approvalId,
        });
      } else {
        await api.post("/api/recurring", { ...intent.body, client_operation_id: intent.id });
      }
      pending.current = null;
      toast(t(existing ? "sched.saved" : "sched.created"));
      onSaved();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) pending.current = null;
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
      <div className="sub">{t("sched.approval.expires", { at: absTime(expiresAt) })}</div>
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
