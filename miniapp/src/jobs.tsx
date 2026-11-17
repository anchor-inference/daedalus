// The Jobs tab: what the session did off the transcript — its jobs and waiters as the host tracks
// them (its subagents have a tab of their own), the receipts of what it verified, the files it sent. The transcript row says a
// job was started; this is where its outcome is. The list comes only from the host: a list rebuilt
// from the transcript showed the same command twice and could not tell a finished job from a running one.
// A job clicked opens here, in the tab, following its output as `tail -f` does. It used to open its log
// in Preview, read once: a running build showed what it had written by the click and nothing after.
// Only the session's own jobs are here; a helper's jobs belong to its own session and are read there.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, ApiError, JobLogPiece, MessageView, TaskView } from "./api";
import { useEvent, useStreamUp } from "./events";
import { timeAgo } from "./ui/components";
import { duration } from "./format";
import { codeBlock } from "./md";
import { RawHtml } from "./rawhtml";
import { Icon } from "./icons";
import { PanelEntry } from "./panel";
import { PreviewSource, fileGlyph, previewKind, sessionBase } from "./preview";
import { DiffView } from "./previewparts";
import { looksLikeDiff } from "./diff";
import { t } from "./i18n";
import { sentPath } from "./turnfiles";
import { errorText } from "./ui";
import { EMPTY_JOB_LOG, JOB_LOG_PIECE, JOB_LOG_POLL_MS, behind, nextOffset, shownLines, stillFollowing, takePiece, type JobLog } from "./joblog";

export type Verification = { id: number; criterion: string; command: string; exit_code: number; passed: number; output_head: string; duration_ms: number; at: string; sandboxed: number; dependencies: string; tests_run: number | null };

export type SentFile = { callId: string; path: string; name: string; caption: string; at: string };

/** How often the list is read again while something runs and the event stream is down: the net
 *  under the events, which say when a job starts or ends. */
export const TASK_POLL_MS = 5000;
/** How often a running job's "running · 3m" is drawn again; nothing is fetched for it. */
export const TASK_TICK_MS = 15000;
/** Several events of one turn arrive together; the list is read once for the burst. */
export const TASK_EVENT_DEBOUNCE_MS = 250;

/** Whether an event changes this session's Jobs list: one of its own jobs started, ended or stopped. */
export function touchesJobs(event: { type: string; session_id: string | null; payload: Record<string, unknown> }, sessionId: string): boolean {
  return event.session_id === sessionId && event.type === "session.jobs";
}

/** The words under a task's title: how it stands (or ended) and how long, and the warning flag if it has one. */
export function taskSub(task: TaskView, now: number): { text: string; flag: string; service: boolean } {
  const started = Date.parse(task.started_at);
  const ended = task.ended_at ? Date.parse(task.ended_at) : now;
  const took = Number.isNaN(started) ? "" : duration(ended - started);
  if (task.state === "running") {
    const flag = task.flag ? t(`task.flag.${task.flag}`) : "";
    return { text: [task.kind === "service" ? t("task.service") : t("task.running"), took].filter(Boolean).join(" · "), flag, service: task.kind === "service" };
  }
  const outcome = task.outcome ?? (task.state === "done" ? "succeeded" : task.state === "cancelled" ? "killed" : task.state === "lost" ? "lost" : "failed");
  const word = outcome === "failed" && task.exit_code !== null && task.exit_code !== 0 ? t("task.outcome.failed.code", { code: task.exit_code }) : t(`task.outcome.${outcome}`);
  return { text: [word, took].filter(Boolean).join(" · "), flag: "", service: false };
}

/** "2 running · 5", or just the total when nothing runs. */
export function tasksAside(tasks: TaskView[]): string {
  const running = tasks.filter((task) => task.state === "running").length;
  return running ? t("panel.jobs.aside", { running, total: tasks.length }) : String(tasks.length);
}

/** The files the agent sent with `SendFile`, newest first. */
export function sentFiles(messages: MessageView[]): SentFile[] {
  const out: SentFile[] = [];
  for (const m of messages) {
    for (const call of m.tool_calls ?? []) {
      if (call.name !== "SendFile" || typeof call.arguments?.path !== "string") continue;
      const path = sentPath(call.name, call.arguments);
      out.push({ callId: call.id, path, name: path.split("/").filter(Boolean).pop() ?? path, caption: typeof call.arguments.caption === "string" ? call.arguments.caption : "", at: m.created_at });
    }
  }
  return out.reverse();
}

export function JobsTab({ sessionId, messages, onOpen, onPreview, openJob, onOpenJob }: {
  sessionId: string;
  messages: MessageView[];
  onOpen: (entry: PanelEntry) => void;
  onPreview: (src: PreviewSource) => void;
  /** The job whose output the tab shows in place of the list, or null for the list. Held by the screen,
   *  so a job named in the transcript opens here as one clicked in the list does. */
  openJob: string | null;
  onOpenJob: (id: string | null) => void;
}) {
  const [receipts, setReceipts] = useState<Verification[] | null>(null);
  const [tasks, setTasks] = useState<TaskView[] | null>(null);
  const load = (gone: () => boolean = () => false) => api.get<{ tasks: TaskView[] }>(`/api/sessions/${sessionId}/tasks`).then((answer) => !gone() && setTasks(answer.tasks)).catch(() => !gone() && setTasks([]));
  useEffect(() => {
    let gone = false;
    api
      .get<Verification[]>(`/api/sessions/${sessionId}/verifications`)
      .then((rows) => !gone && setReceipts(Array.isArray(rows) ? rows : []))
      .catch(() => !gone && setReceipts([]));
    void load(() => gone);
    return () => {
      gone = true;
    };
  }, [sessionId, messages.length]);
  const anyRunning = !!tasks?.some((task) => task.state === "running");
  const live = useStreamUp();
  // The list used to be read when the tab opened and then only while something already ran: a job
  // started with nothing else running showed only once the tab was left and opened again. The event
  // stream now says when; the poll is kept for a stream that is down.
  const soon = useRef<number | null>(null);
  useEffect(() => () => { if (soon.current) window.clearTimeout(soon.current); }, [sessionId]);
  useEvent(["session.jobs"], (event) => {
    if (!touchesJobs(event, sessionId) || soon.current) return;
    soon.current = window.setTimeout(() => { soon.current = null; void load(); }, TASK_EVENT_DEBOUNCE_MS);
  }, [sessionId]);
  // The stream coming back may have missed an end while it was away.
  const wasLive = useRef(live);
  useEffect(() => {
    if (live && !wasLive.current) void load();
    wasLive.current = live;
  }, [live]);
  const [, tick] = useState(0);
  useEffect(() => {
    if (!anyRunning) return;
    let gone = false;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      if (live) tick((n) => n + 1);
      else void load(() => gone);
    }, live ? TASK_TICK_MS : TASK_POLL_MS);
    return () => {
      gone = true;
      window.clearInterval(timer);
    };
  }, [sessionId, anyRunning, live]);
  const sent = sentFiles(messages);
  const base = sessionBase(sessionId);
  const empty = (tasks?.length ?? 0) === 0 && sent.length === 0 && (receipts?.length ?? 0) === 0;
  if (openJob) {
    const task = tasks?.find((each) => each.id === openJob);
    const stop = async () => { await api.post(`/api/sessions/${sessionId}/tasks/${encodeURIComponent(openJob)}/stop`, {}); await load(); };
    // The file under the view: the whole log in Preview, for what the view has let go of the top.
    const whole = () => onOpen({ base, path: task?.result_ref ?? `.jobs/${openJob}.log` });
    return <JobView key={openJob} sessionId={sessionId} jobId={openJob} task={task} onBack={() => onOpenJob(null)} onStop={task?.stop_supported ? stop : undefined} onWhole={whole} />;
  }
  return (
    <div className="jobs">
      {tasks && tasks.length > 0 && (
        <section className="dt-section task-list">
          <div className="dt-label"><span>{t("panel.jobs.title")}</span><span className="dt-aside">{tasksAside(tasks)}</span></div>
          {tasks.map((task) => {
            const sub = taskSub(task, Date.now());
            return (
              <div key={task.id} className={`aside-row task-row ${task.state}`} data-kind={task.kind}>
                <span className={`dot ${task.state}`} />
                <Icon name="terminal" size={14} />
                <button className="grow task-open mono" title={task.command ?? task.title} onClick={() => onOpenJob(task.id)}>
                  <span className="name">{task.title}</span>
                  <span className="sub task-sub">
                    {sub.text}
                    {sub.flag && <span className={`task-flag ${task.flag}`}>{sub.flag}</span>}
                    {task.where === "host" && <span className="task-where">{t("task.host")}</span>}
                  </span>
                </button>
                {task.stop_supported && <button className="iconbtn small quiet task-stop" aria-label={t("task.stop")} title={t("task.stop")} onClick={async () => { await api.post(`/api/sessions/${sessionId}/tasks/${encodeURIComponent(task.id)}/stop`, {}); await load(); }}><Icon name="stop" size={14} /></button>}
              </div>
            );
          })}
        </section>
      )}
      {empty && (
        <div className="empty">
          <b>{t("panel.jobs.empty.title")}</b>
          <div>{t("panel.jobs.empty.body")}</div>
        </div>
      )}
      {receipts && receipts.length > 0 && (
        <section className="dt-section">
          <div className="dt-label"><span>{t("panel.jobs.receipts")}</span><span className="dt-aside">{receipts.length}</span></div>
          {receipts.map((r) => (
            <details key={r.id} className="receipt">
              <summary className="aside-row">
                <span className={`dot ${r.passed ? "done" : "failed"}`} />
                <span className="grow name">{r.criterion}</span>
                <span className="sub">v{r.id}</span>
              </summary>
              <div className="sub">
                {t("session.receipt.meta", { code: r.exit_code, secs: (r.duration_ms / 1000).toFixed(1), when: timeAgo(r.at) })}
                {r.tests_run !== null && t("session.receipt.tests", { n: r.tests_run })}
                {r.sandboxed ? t("session.receipt.sandboxed") : ""}
                {r.dependencies && t("session.receipt.depends", { list: r.dependencies })}
              </div>
              <RawHtml html={codeBlock(r.command, "sh")} />
              {r.output_head && (looksLikeDiff(r.output_head) ? <DiffView text={r.output_head} /> : <pre className="filetext">{r.output_head}</pre>)}
            </details>
          ))}
        </section>
      )}
      {sent.length > 0 && (
        <section className="dt-section">
          <div className="dt-label"><span>{t("session.sentfiles")}</span><span className="dt-aside">{sent.length}</span></div>
          {sent.map((f) => (
            <button key={f.callId} className="aside-row link" onClick={() => onPreview({ base: `${base}/sent/${encodeURIComponent(f.callId)}`, path: f.name })} title={f.caption || f.path}>
              <span aria-hidden>{fileGlyph(f.name)}</span>
              <span className="grow name">{f.name}</span>
              <span className="sub">{previewKind(f.name) === "other" ? t("preview.download") : timeAgo(f.at)}</span>
            </button>
          ))}
        </section>
      )}
    </div>
  );
}

/** How an ended job ended, for the line under its output. */
export function jobEnd(piece: Pick<JobLogPiece, "state" | "outcome" | "exit_code">): string {
  const outcome = piece.outcome ?? (piece.state === "done" ? "succeeded" : piece.state === "cancelled" ? "killed" : piece.state === "lost" ? "lost" : "failed");
  const how = t(`task.outcome.${outcome}`);
  return piece.exit_code !== null ? t("job.ended.code", { how, code: piece.exit_code }) : t("job.ended", { how });
}

/** Within this many pixels of the end the reader is "at the bottom", and the view follows new output. */
const AT_END_PX = 24;

/** One job, in the Jobs tab: its title and state over its output, followed while it runs.
 *
 *  The output scrolls in a box of its own so the head and the end line stay put. It follows new lines
 *  while the reader is at the end; scrolled up, it stays where they are and offers the way back down. */
export function JobView({ sessionId, jobId, task, onBack, onStop, onWhole }: { sessionId: string; jobId: string; task: TaskView | undefined; onBack: () => void; onStop?: () => void; onWhole: () => void }) {
  const [log, setLog] = useState<JobLog>(EMPTY_JOB_LOG);
  const [last, setLast] = useState<JobLogPiece | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [following, setFollowing] = useState(true);
  useEffect(() => {
    let done = false;
    let timer = 0;
    let offset = -1;
    let held = EMPTY_JOB_LOG;
    const read = async () => {
      if (done) return;
      // A hidden page asks for nothing; it catches up in one read when it is looked at again.
      if (document.visibilityState !== "visible") { timer = window.setTimeout(read, JOB_LOG_POLL_MS); return; }
      try {
        const piece = await api.get<JobLogPiece>(`/api/sessions/${sessionId}/tasks/${encodeURIComponent(jobId)}/log?offset=${offset}&limit=${JOB_LOG_PIECE}`);
        if (done) return;
        held = takePiece(held, piece);
        offset = nextOffset(piece);
        setLog(held);
        setLast(piece);
        setFailure(null);
        if (stillFollowing(piece)) timer = window.setTimeout(read, behind(piece) ? 0 : JOB_LOG_POLL_MS);
      } catch (error) {
        if (done) return;
        if (error instanceof ApiError && error.status === 404) { setGone(true); return; }
        setFailure(errorText(error));
        timer = window.setTimeout(read, JOB_LOG_POLL_MS * 3);
      }
    };
    void read();
    return () => { done = true; window.clearTimeout(timer); };
  }, [sessionId, jobId]);
  useLayoutEffect(() => {
    const el = box.current;
    if (el && follow.current) el.scrollTop = el.scrollHeight;
  }, [log]);
  const onScroll = () => {
    const el = box.current;
    if (!el) return;
    const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight <= AT_END_PX;
    follow.current = atEnd;
    setFollowing(atEnd);
  };
  const toEnd = () => {
    const el = box.current;
    follow.current = true;
    setFollowing(true);
    if (el) el.scrollTop = el.scrollHeight;
  };
  const running = last ? last.running : task?.state === "running";
  const state = last?.state ?? task?.state ?? "running";
  const sub = task ? taskSub(task, Date.now()) : null;
  const lines = shownLines(log);
  return (
    <div className="job-view" data-job={jobId} data-state={state} data-following={following ? "" : undefined}>
      <div className="job-head">
        <button type="button" className="iconbtn small job-back" onClick={onBack} aria-label={t("job.back")} title={t("job.back")}><Icon name="back" size={16} /></button>
        <span className={`dot ${state}`} aria-hidden="true" />
        <span className="job-head-copy">
          <b className="truncate mono" title={task?.command ?? undefined}>{task?.title ?? jobId}</b>
          {sub && <small className="truncate">{sub.text}{sub.flag && <span className={`task-flag ${task?.flag}`}> · {sub.flag}</span>}</small>}
        </span>
        {running && <span className="job-live" title={t("job.live.title")}><i aria-hidden="true" />{t("job.live")}</span>}
        {running && onStop && <button type="button" className="iconbtn small quiet job-stop" aria-label={t("task.stop")} title={t("task.stop")} onClick={onStop}><Icon name="stop" size={14} /></button>}
        <button type="button" className="iconbtn small quiet job-whole" aria-label={t("job.whole")} title={t("job.whole")} onClick={onWhole}><Icon name="file" size={14} /></button>
      </div>
      <div className="job-log-wrap">
        <div ref={box} className="job-log" onScroll={onScroll} tabIndex={0} aria-label={t("job.output")}>
          {(log.cut || log.dropped > 0) && <button type="button" className="job-earlier" onClick={onWhole}>{t("job.earlier")}</button>}
          {gone ? <div className="empty job-gone"><div>{t("job.gone")}</div><button type="button" className="btn small" onClick={onWhole}>{t("job.whole")}</button></div>
            : !last && !failure ? <div className="sub job-wait">{t("common.loading")}</div>
            : lines.length === 0 ? <div className="sub job-wait">{running ? t("job.empty.running") : t("job.empty")}</div>
            : <pre className="job-log-text">{lines.join("\n")}</pre>}
        </div>
        {!following && !gone && <button type="button" className="btn small job-latest" onClick={toEnd}><Icon name="down" size={14} />{t("job.latest")}</button>}
      </div>
      {failure && <div className="job-end failed" role="status">{t("job.unreadable", { error: failure })}</div>}
      {last && !last.running && !failure && <div className={`job-end ${state}`} role="status" data-job-end>{jobEnd(last)}</div>}
    </div>
  );
}
