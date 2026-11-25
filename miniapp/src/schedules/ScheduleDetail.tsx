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
import { Button, IconButton, OverflowMenu, Switch, useLayer, type MenuItem } from "../ui/primitives";
import type { ScheduleActions } from "./actions";
import { type Cycle, type CycleHistory, conversationOf, cycleState, type Item, lastRun, scheduleFiles, type ScheduleRow } from "./model";
import { ScheduleFormBody } from "./ScheduleForm";

/** The state a schedule is in, said in a word, and the tone it is said in. */
function statusOf(item: Item): { word: string; tone: "attention" | "plain" | "quiet" } {
  if (item.source === "proposal") return { word: t("sched.status.waiting"), tone: "attention" };
  if (item.group === "approval") return { word: t("sched.approval.needed"), tone: "attention" };
  if (item.group === "active") return { word: t("sched.status.active"), tone: "plain" };
  if (item.group === "paused") return { word: t("sched.status.paused"), tone: "quiet" };
  return { word: t("sched.status.done"), tone: "quiet" };
}

/**
 * One schedule, opened: what it does and when, in the reader's own words, its switch and commands, its
 * full instruction, its files and its recent runs. A 420 px pane beside the list on the Schedules
 * page, as a card's pane is beside the board, and the same contents in a sheet on a phone or over a
 * chat (`layout="sheet"`, where the sheet carries the closing and the foot stays under the last line).
 */
export function ScheduleDetail({ item, actions, titles, toast, onOpenSession, onRemoved, onClose, onBack, layout = "pane" }: {
  item: Item;
  actions: ScheduleActions;
  titles: Record<string, string>;
  toast: (text: string) => void;
  onOpenSession: (id: string) => void;
  onRemoved: () => void;
  /** The pane's own close; a sheet is closed from its own head. */
  onClose?: () => void;
  /** The way back to the list, in a sheet that took the list's place. */
  onBack?: () => void;
  layout?: "pane" | "sheet";
}) {
  const [editing, setEditing] = useState(false);
  // Escape closes the pane (or leaves the form first), unless a list or a menu opened from it is on top.
  useLayer(() => (editing ? setEditing(false) : onClose?.()), layout === "pane" && !!onClose);
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

  const menu: MenuItem[] = s ? [
    { label: t("common.edit"), icon: "pen", disabled: busy, onSelect: () => setEditing(true) },
    "-",
    { label: t("common.delete"), icon: "trash", danger: true, disabled: busy, onSelect: () => void remove() },
  ] : [];
  const head = (title: string) => (
    <div className="sched-pane-head">
      {onBack && <IconButton icon="back" label={t("shell.back")} onClick={editing ? () => setEditing(false) : onBack} />}
      <span className="sched-pane-kind">{title}</span>
      <span className="grow" />
      {!editing && s && (
        <span className="sched-enabled">
          <span className="sched-enabled-word" aria-hidden="true">{t("sched.enabled")}</span>
          <Switch label={t("sched.enabled")} checked={!!s.enabled} disabled={busy || item.group === "done" || (!s.enabled && !resumable)} onChange={(on) => void actions.setEnabled(s, on)} />
        </span>
      )}
      {!editing && s && <OverflowMenu label={t("sched.actions")} items={menu} />}
      {onClose && <IconButton icon="close" label={t("common.close")} onClick={editing ? () => setEditing(false) : onClose} />}
    </div>
  );

  if (editing && s) {
    return (
      <article className={`sched-pane editing ${layout === "sheet" ? "in-sheet" : ""}`} aria-label={t("sched.edit")} data-schedule={item.key}>
        {head(t("sched.edit"))}
        <div className="sched-pane-body">
          <ScheduleFormBody existing={s} onCancel={() => setEditing(false)} onSave={(edit) => actions.save(s, edit)} toast={toast} approvalDays={actions.approvalDays} />
        </div>
      </article>
    );
  }

  return (
    <article className={`sched-pane ${layout === "sheet" ? "in-sheet" : ""}`} aria-label={rule.name} data-schedule={item.key}>
      {head(t("sched.pane"))}
      <div className="sched-pane-body">
        <h2 className="sched-pane-title">{rule.name}</h2>
        {(item.group === "approval") && (
          <div className="sched-pane-need" role="status"><span className="dot attention" aria-hidden="true" />{p ? t("sched.proposal.from.agent") : t("sched.approval.needed")}</div>
        )}
        {p?.legacy_file_review_required && <div className="result-warning">{t("sched.proposal.legacyfiles")}</div>}

        <dl className="sched-kv">
          <div className="sched-kv-row"><dt>{t("sched.when")}</dt><dd>{describeSchedule(rule)}</dd></div>
          {next && <div className="sched-kv-row"><dt>{t("sched.nextrun")}</dt><dd title={absTime(next)}>{untilShort(next)} <span className="faint">{absTime(next)}</span></dd></div>}
          <div className="sched-kv-row"><dt>{t("sched.state")}</dt><dd><span className={`sched-state ${status.tone}`}><span className="dot" aria-hidden="true" />{status.word}</span></dd></div>
          <div className="sched-kv-row"><dt>{t("sched.kind")}</dt><dd>{t(`sched.kind.${rule.kind}`)}{s?.run_in === "self" ? t("sched.runs.here") : rule.kind === "agent" ? t("sched.runs.fresh") : ""}</dd></div>
          <div className="sched-kv-row">
            <dt>{t("sched.chat")}</dt>
            <dd>{chat ? <button type="button" className="linkbtn" onClick={() => onOpenSession(chat)}>{titles[chat] ?? t("sched.chat.untitled")}</button> : <span className="faint">{t("sched.chat.none")}</span>}</dd>
          </div>
          <div className="sched-kv-row">
            <dt>{t("sched.lastrun")}</dt>
            <dd>
              {last ? (
                <span className={`sched-last ${last.status}`}>
                  <span className="dot" aria-hidden="true" />
                  {last.status === "running" ? t("sched.running") : t(last.status === "failed" ? "sched.last.failed" : "sched.last.ok", { t: relTimeLong(last.at) })}
                  {last.sessionId && <> <button type="button" className="linkbtn" onClick={() => onOpenSession(last.sessionId!)}>{t("sched.run.open")}</button></>}
                </span>
              ) : <span className="faint">{t("sched.lastrun.never")}</span>}
            </dd>
          </div>
          <div className="sched-kv-row"><dt>{t("sched.setby")}</dt><dd>{p || s?.created_by_session || s?.source_session_id ? t("sched.setby.agent") : t("sched.setby.you")}</dd></div>
          {/* Said only while the operator reviews the agent's schedules: with the standing approval the
              date is ten years out, and a line about renewing it was noise that read as a warning. */}
          {s?.grant_expires_at && actions.requireApproval && (
            <div className="sched-kv-row"><dt>{t("sched.approval.until")}</dt><dd>{absTime(s.grant_expires_at)}</dd></div>
          )}
        </dl>

        {s?.last_error && <div className="sched-error"><b>{t("sched.lasterror")}</b> {s.last_error}</div>}

        <Prompt key={item.key} text={rule.prompt} title={t(rule.kind === "agent" ? "sched.instruction" : "sched.text")} />

        <Files item={item} />

        {s?.last_summary && (
          <section className="sched-block">
            <h3>{t("sched.lastresult")}</h3>
            <div className="sched-summary">{s.last_summary}</div>
          </section>
        )}

        {s && <History schedule={s} onOpenSession={onOpenSession} toast={toast} />}

        <details className="sched-tech">
          <summary>{t("common.details")}</summary>
          <div className="sub mono">{s ? s.id : p!.id}{rule.cron ? ` · cron ${rule.cron} (UTC)` : ""}</div>
          {p && <div className="sub mono">{t("sched.proposal.revision", { n: p.proposal_revision })}</div>}
          {p?.files.map((file) => <div className="sub mono" key={`${file.name}:${file.digest}`}>{file.name} · SHA-256 {file.digest}</div>)}
        </details>
      </div>
      {/* In a sheet the foot is the sheet's own: it stays under the last line, and the sheet's "more below" button gives way to it. */}
      <footer className={`sched-pane-foot ${layout === "sheet" ? "sheet-foot" : ""}`} role="toolbar" aria-label={t("sched.actions")}>
        {p ? (
          <>
            {p.legacy_file_review_required && <Button disabled={busy} onClick={() => void actions.decide(p, "review-files")}>{t("sched.proposal.reviewfiles")}</Button>}
            <Button variant="quiet" disabled={busy} onClick={() => void actions.decide(p, "withdraw")}>{t("sched.proposal.withdraw")}</Button>
            <Button variant="primary" icon="check" disabled={busy || p.legacy_file_review_required} onClick={() => void actions.decide(p, "accept")}>{t("sched.proposal.accept")}</Button>
          </>
        ) : s && (
          <>
            {s.authority_state === "needs_approval" && item.group === "approval" && (
              <Button variant="primary" icon="check" disabled={busy} onClick={() => void actions.approve(s)}>{t("sched.approval.action")}</Button>
            )}
            <Button icon="play" disabled={busy || item.group === "approval" || !!s.active_session_id} onClick={() => void actions.runNow(s)}>{t("common.runnow")}</Button>
            <Button variant="quiet" icon="pen" disabled={busy} onClick={() => setEditing(true)}>{t("common.edit")}</Button>
          </>
        )}
      </footer>
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
    <section className="sched-block">
      <h3>{title}</h3>
      <div className={`sched-prompt ${long && !open ? "folded" : ""} ${open ? "open" : ""}`}>
        <RawHtml className="answer" html={html} />
      </div>
      {long && (
        <button type="button" className="linkbtn sched-more" aria-expanded={open} onClick={() => setOpen(!open)}>
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
    <section className="sched-block">
      <h3>{t("sched.files")}</h3>
      <ul className="sched-files">
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
    <section className="sched-block">
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
