// The Jobs tab: what the session did off the transcript — its jobs, waiters and sub-agents as the
// host tracks them, the receipts of what it verified, the files it sent. The transcript row says a
// job was started; this is where its outcome is. The list comes only from the host: a list rebuilt
// from the transcript showed the same command twice and could not tell a finished job from a running one.

import { useEffect, useState } from "react";
import { api, MessageView, TaskView } from "./api";
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

export type Verification = { id: number; criterion: string; command: string; exit_code: number; passed: number; output_head: string; duration_ms: number; at: string; sandboxed: number; dependencies: string; tests_run: number | null };

export type SentFile = { callId: string; path: string; name: string; caption: string; at: string };

/** How often the list is read again while something runs: a job ends with no message in the transcript to say so. */
export const TASK_POLL_MS = 5000;

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
      const path = call.arguments.path;
      out.push({ callId: call.id, path, name: path.split("/").filter(Boolean).pop() ?? path, caption: typeof call.arguments.caption === "string" ? call.arguments.caption : "", at: m.created_at });
    }
  }
  return out.reverse();
}

export function JobsTab({ sessionId, messages, onOpen, onPreview, onOpenSession }: { sessionId: string; messages: MessageView[]; onOpen: (entry: PanelEntry) => void; onPreview: (src: PreviewSource) => void; onOpenSession?: (id: string) => void }) {
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
  useEffect(() => {
    if (!anyRunning) return;
    let gone = false;
    const timer = window.setInterval(() => void load(() => gone), TASK_POLL_MS);
    return () => {
      gone = true;
      window.clearInterval(timer);
    };
  }, [sessionId, anyRunning]);
  const sent = sentFiles(messages);
  const base = sessionBase(sessionId);
  const empty = (tasks?.length ?? 0) === 0 && sent.length === 0 && (receipts?.length ?? 0) === 0;
  const open = (task: TaskView) => (task.child_session_id ? onOpenSession?.(task.child_session_id) : task.result_ref ? onOpen({ base, path: task.result_ref }) : undefined);
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
                <Icon name={task.kind === "agent" ? "bots" : "terminal"} size={14} />
                <button className={`grow task-open ${task.kind === "agent" ? "" : "mono"}`} title={task.command ?? task.title} onClick={() => open(task)}>
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
