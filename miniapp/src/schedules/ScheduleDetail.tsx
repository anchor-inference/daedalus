// One schedule, opened: what it does and when, in the reader's own words, the commands for it, its
// full instruction, its files and its recent runs. The same pane on the Schedules screen and in the
// schedules dialog over a chat.

import { useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { absTime, describeSchedule, relTimeLong, untilShort } from "../format";
import { t } from "../i18n";
import { Icon } from "../icons";
import { renderMarkdown } from "../md";
import { RawHtml } from "../rawhtml";
import { useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import type { ScheduleActions } from "./actions";
import { type Cycle, type CycleHistory, conversationOf, cycleState, type Item, lastRun, scheduleFiles, type ScheduleRow } from "./model";
import { ScheduleFormBody } from "./ScheduleForm";

/** The pill a schedule's state is said with, and its tone. */
function statusOf(item: Item): { word: string; tone: string } {
  if (item.source === "proposal") return { word: t("sched.status.waiting"), tone: "pending" };
  if (item.group === "approval") return { word: t("sched.approval.needed"), tone: "pending" };
  if (item.group === "active") return { word: t("sched.status.active"), tone: "done" };
  if (item.group === "paused") return { word: t("sched.status.paused"), tone: "closed" };
  return { word: t("sched.status.done"), tone: "closed" };
}

export function ScheduleDetail({ item, actions, titles, toast, onOpenSession, onRemoved, onBack }: {
  item: Item;
  actions: ScheduleActions;
  titles: Record<string, string>;
  toast: (text: string) => void;
  onOpenSession: (id: string) => void;
  onRemoved: () => void;
  onBack?: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const s = item.source === "schedule" ? item.schedule : null;
  const p = item.source === "proposal" ? item.proposal : null;
  const rule = s ?? p!;
  const status = statusOf(item);
  const next = s ? (s.enabled ? s.next_run_at : null) : p!.next_run_at;
  const chat = conversationOf(item);
  const last = s ? lastRun(s) : null;
  const busy = !!actions.busy;
  // A one-off that has run, or whose moment passed while it was paused, has nothing left to resume.
  const resumable = !!s && (!!s.cron || (!!s.run_at && !s.last_run_at && Date.parse(s.run_at) > Date.now()));

  async function remove() {
    if (!s) return;
    if (!(await confirmAsync(t("sched.delete.title", { name: s.name }), { body: t(s.cron ? "sched.delete.body.cron" : "sched.delete.body.once"), action: t("sched.delete.action"), danger: true }))) return;
    if (await actions.remove(s)) onRemoved();
  }

  if (editing && s) {
    return (
      <article className="scheddetail editing" aria-label={t("sched.edit")}>
        <header className="scheddetail-head">
          {onBack && <button type="button" className="iconbtn small scheddetail-back" onClick={() => setEditing(false)} aria-label={t("shell.back")} title={t("shell.back")}><Icon name="back" size={16} /></button>}
          <h2 className="scheddetail-name">{t("sched.edit")}</h2>
        </header>
        <ScheduleFormBody existing={s} onCancel={() => setEditing(false)} onSave={(edit) => actions.save(s, edit)} toast={toast} approvalDays={actions.approvalDays} />
      </article>
    );
  }

  return (
    <article className="scheddetail" aria-label={rule.name} data-schedule={item.key}>
      <header className="scheddetail-head">
        {onBack && <button type="button" className="iconbtn small scheddetail-back" onClick={onBack} aria-label={t("shell.back")} title={t("shell.back")}><Icon name="back" size={16} /></button>}
        <div className="scheddetail-title">
          <h2 className="scheddetail-name">{rule.name}</h2>
          <div className="scheddetail-when">
            <span>{describeSchedule(rule)}</span>
            {next && <span className="scheddetail-next" title={absTime(next)}>{t("sched.next.short", { rel: untilShort(next), at: absTime(next) })}</span>}
          </div>
        </div>
        <span className={`pill ${status.tone}`}>{status.word}</span>
      </header>

      <div className="scheddetail-actions" role="toolbar" aria-label={t("sched.actions")}>
        {p ? (
          <>
            {p.legacy_file_review_required && <button type="button" className="btn small" disabled={busy} onClick={() => void actions.decide(p, "review-files")}>{t("sched.proposal.reviewfiles")}</button>}
            <button type="button" className="btn small" disabled={busy} onClick={() => void actions.decide(p, "withdraw")}>{t("sched.proposal.withdraw")}</button>
            <button type="button" className="btn small primary" disabled={busy || p.legacy_file_review_required} onClick={() => void actions.decide(p, "accept")}><Icon name="check" size={14} /> {t("sched.proposal.accept")}</button>
          </>
        ) : s && (
          <>
            {s.authority_state === "needs_approval" && item.group === "approval" && (
              <button type="button" className="btn small primary" disabled={busy} onClick={() => void actions.approve(s)}><Icon name="check" size={14} /> {t("sched.approval.action")}</button>
            )}
            <button type="button" className="btn small" disabled={busy || item.group === "approval" || !!s.active_session_id} onClick={() => void actions.runNow(s)}><Icon name="play" size={14} /> {t("common.runnow")}</button>
            {s.enabled
              ? <button type="button" className="btn small" disabled={busy} onClick={() => void actions.setEnabled(s, false)}><Icon name="pause" size={14} /> {t("common.pause")}</button>
              : <button type="button" className="btn small" disabled={busy || !resumable} onClick={() => void actions.setEnabled(s, true)}><Icon name="play" size={14} /> {t("common.resume")}</button>}
            <button type="button" className="btn small" disabled={busy} onClick={() => setEditing(true)}><Icon name="pen" size={14} /> {t("common.edit")}</button>
            <button type="button" className="btn small danger-ghost" disabled={busy} onClick={() => void remove()}><Icon name="trash" size={14} /> {t("common.delete")}</button>
          </>
        )}
      </div>
      {p?.legacy_file_review_required && <div className="result-warning">{t("sched.proposal.legacyfiles")}</div>}

      <dl className="scheddetail-facts">
        <div><dt>{t("sched.kind")}</dt><dd>{t(`sched.kind.${rule.kind}`)}{s?.run_in === "self" ? t("sched.runs.here") : rule.kind === "agent" ? t("sched.runs.fresh") : ""}</dd></div>
        <div>
          <dt>{t("sched.chat")}</dt>
          <dd>{chat ? <button type="button" className="linkbtn" onClick={() => onOpenSession(chat)}>{titles[chat] ?? t("sched.chat.untitled")}</button> : <span className="faint">{t("sched.chat.none")}</span>}</dd>
        </div>
        <div>
          <dt>{t("sched.lastrun")}</dt>
          <dd>
            {last ? (
              <span className={`scheddetail-last ${last.status}`}>
                <span className="dot" aria-hidden />
                {last.status === "running" ? t("sched.running") : t(last.status === "failed" ? "sched.last.failed" : "sched.last.ok", { t: relTimeLong(last.at) })}
                {last.at && <span className="faint"> {absTime(last.at)}</span>}
                {last.sessionId && <> <button type="button" className="linkbtn" onClick={() => onOpenSession(last.sessionId!)}>{t("sched.run.open")}</button></>}
              </span>
            ) : <span className="faint">{t("sched.lastrun.never")}</span>}
          </dd>
        </div>
        <div><dt>{t("sched.setby")}</dt><dd>{p || s?.created_by_session || s?.source_session_id ? t("sched.setby.agent") : t("sched.setby.you")}</dd></div>
        {/* Said only while the operator reviews the agent's schedules: with the standing approval the
            date is ten years out, and a line about renewing it was noise that read as a warning. */}
        {s?.grant_expires_at && actions.requireApproval && (
          <div><dt>{t("sched.approval.until")}</dt><dd>{absTime(s.grant_expires_at)}</dd></div>
        )}
      </dl>

      {s?.last_error && <div className="scheddetail-error"><b>{t("sched.lasterror")}</b> {s.last_error}</div>}

      <Prompt key={item.key} text={rule.prompt} title={t(rule.kind === "agent" ? "sched.instruction" : "sched.text")} />

      <Files item={item} />

      {s?.last_summary && (
        <section className="scheddetail-section">
          <h3>{t("sched.lastresult")}</h3>
          <div className="scheddetail-summary">{s.last_summary}</div>
        </section>
      )}

      {s && <History schedule={s} onOpenSession={onOpenSession} toast={toast} />}

      <details className="scheddetail-tech">
        <summary>{t("common.details")}</summary>
        <div className="sub mono">{s ? s.id : p!.id}{rule.cron ? ` · cron ${rule.cron} (UTC)` : ""}</div>
        {p && <div className="sub mono">{t("sched.proposal.revision", { n: p.proposal_revision })}</div>}
        {p?.files.map((file) => <div className="sub mono" key={`${file.name}:${file.digest}`}>{file.name} · SHA-256 {file.digest}</div>)}
      </details>
    </article>
  );
}

/**
 * The instruction, drawn as the markdown the agent wrote it in, folded to a few lines with its own
 * scroll once opened. Shown whole, a two-thousand-character prompt pushed everything else off the
 * screen, which is what made the old proposals section a wall of text.
 */
function Prompt({ text, title }: { text: string; title: string }) {
  const [open, setOpen] = useState(false);
  const html = useMemo(() => renderMarkdown(text), [text]);
  const long = text.length > 280 || text.split("\n").length > 5;
  return (
    <section className="scheddetail-section">
      <h3>{title}</h3>
      <div className={`scheddetail-prompt ${long && !open ? "folded" : ""} ${open ? "open" : ""}`}>
        <RawHtml className="answer" html={html} />
      </div>
      {long && (
        <button type="button" className="linkbtn scheddetail-more" aria-expanded={open} onClick={() => setOpen(!open)}>
          {t(open ? "sched.prompt.less" : "sched.prompt.more")}
        </button>
      )}
    </section>
  );
}

function Files({ item }: { item: Item }) {
  const files = item.source === "proposal" ? item.proposal.files : scheduleFiles(item.schedule);
  if (!files.length) return null;
  return (
    <section className="scheddetail-section">
      <h3>{t("sched.files")}</h3>
      <ul className="scheddetail-files">
        {files.map((file, index) => (
          <li key={`${file.name}:${index}`}><Icon name="file" size={14} /><span className="truncate">{file.name}</span>{file.size != null && <span className="faint num">{bytes(file.size)}</span>}</li>
        ))}
      </ul>
    </section>
  );
}

function bytes(n: number): string {
  if (n < 1024) return t("sched.bytes.b", { n });
  if (n < 1024 * 1024) return t("sched.bytes.kb", { n: Math.round(n / 1024) });
  return t("sched.bytes.mb", { n: (n / 1048576).toFixed(1) });
}

/** The recent occurrences, newest first, with the conversation each run went into. */
function History({ schedule, onOpenSession, toast }: { schedule: ScheduleRow; onOpenSession: (id: string) => void; toast: (text: string) => void }) {
  const path = `/api/recurring/${encodeURIComponent(schedule.id)}/cycles`;
  const { data, error, refresh } = useQuery<CycleHistory>(path, { pollMs: 30000, staleMs: 5000 });
  return (
    <section className="scheddetail-section">
      <h3>{t("sched.cycle.title")}</h3>
      {error && <div className="sub">{t("sched.approval.readfailed")}</div>}
      {data && data.cycles.length === 0 && <div className="sub faint">{t("sched.cycle.empty")}</div>}
      {data && data.cycles.length > 0 && (
        <ol className="schedhistory">
          {data.cycles.slice(0, 12).map((cycle) => <CycleRow key={cycle.id} cycle={cycle} schedule={schedule} revision={data.collection_revision} onOpenSession={onOpenSession} toast={toast} onDone={refresh} />)}
        </ol>
      )}
    </section>
  );
}

function CycleRow({ cycle, schedule, revision, onOpenSession, toast, onDone }: { cycle: Cycle; schedule: ScheduleRow; revision: number | null; onOpenSession: (id: string) => void; toast: (text: string) => void; onDone: () => void }) {
  const state = cycleState(cycle);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = useRef<{ fingerprint: string; id: string } | null>(null);
  async function reconcile(outcome: "delivered" | "not_delivered") {
    if (!revision || !reason.trim()) return;
    const body = { outcome, reason: reason.trim(), expected_collection_revision: revision };
    const fingerprint = JSON.stringify(body);
    if (pending.current?.fingerprint !== fingerprint) pending.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post(`/api/recurring/${encodeURIComponent(schedule.id)}/cycles/${encodeURIComponent(cycle.id)}/reconcile`, { ...body, client_operation_id: pending.current.id });
      pending.current = null;
      setReason("");
      toast(t("sched.cycle.reconciled"));
      onDone();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) pending.current = null;
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <li className={`schedhistory-row ${state}`}>
      <span className="dot" aria-hidden />
      <span className="schedhistory-at num" title={absTime(cycle.due_at)}>{absTime(cycle.due_at)}</span>
      <span className="schedhistory-state">{t(`sched.cycle.${state}`)}{cycle.manual ? t("sched.cycle.manual") : ""}</span>
      {cycle.session_id && <button type="button" className="linkbtn" onClick={() => onOpenSession(cycle.session_id!)}>{t("sched.run.open")}</button>}
      {cycle.effect_error && state === "failed" && <span className="schedhistory-error">{cycle.effect_error}</span>}
      {state === "unknown" && (
        <div className="schedhistory-reconcile">
          <div className="sub">{t("sched.cycle.unknown.hint")}</div>
          <input className="field" value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("sched.cycle.reason")} />
          <div className="row-actions">
            <button className="btn small" disabled={busy || !reason.trim()} onClick={() => void reconcile("delivered")}>{t("sched.cycle.confirmed")}</button>
            <button className="btn small" disabled={busy || !reason.trim()} onClick={() => void reconcile("not_delivered")}>{t("sched.cycle.notdelivered")}</button>
          </div>
        </div>
      )}
    </li>
  );
}
