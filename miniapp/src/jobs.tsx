// The Jobs tab: what the session did off the transcript — background commands and their logs, the
// receipts of what it verified, the files it sent. The transcript row says a job was started; this
// is where its outcome is.

import { useEffect, useState } from "react";
import { api, MessageView, TaskView } from "./api";
import { timeAgo } from "./ui/components";
import { codeBlock } from "./md";
import { RawHtml } from "./rawhtml";
import { Icon } from "./icons";
import { PanelEntry } from "./panel";
import { PreviewSource, fileGlyph, previewKind, sessionBase } from "./preview";
import { DiffView } from "./previewparts";
import { looksLikeDiff } from "./diff";
import { t } from "./i18n";

export type Verification = { id: number; criterion: string; command: string; exit_code: number; passed: number; output_head: string; duration_ms: number; at: string; sandboxed: number; dependencies: string; tests_run: number | null };

export type BackgroundJob = { id: string; command: string; at: string; log: string };
export type SentFile = { callId: string; path: string; name: string; caption: string; at: string };

/** The background commands the transcript started: an `Exec` with `background`, answered with a job id. */
export function backgroundJobs(messages: MessageView[]): BackgroundJob[] {
  const results = new Map<string, string>();
  for (const m of messages) for (const r of m.tool_results ?? []) results.set(r.id, r.content);
  const jobs: BackgroundJob[] = [];
  for (const m of messages) {
    for (const call of m.tool_calls ?? []) {
      if (call.name !== "Exec" || !call.arguments?.background) continue;
      const answer = results.get(call.id) ?? "";
      const id = /^(job-[A-Za-z0-9-]+):/.exec(answer)?.[1];
      if (!id) continue;
      jobs.push({ id, command: String(call.arguments.command ?? ""), at: m.created_at, log: `.jobs/${id}.log` });
    }
  }
  return jobs.reverse();
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
  const loadTasks = () => api.get<{ tasks: TaskView[] }>(`/api/sessions/${sessionId}/tasks`).then((answer) => setTasks(answer.tasks)).catch(() => setTasks([]));
  useEffect(() => {
    let gone = false;
    api
      .get<Verification[]>(`/api/sessions/${sessionId}/verifications`)
      .then((rows) => !gone && setReceipts(Array.isArray(rows) ? rows : []))
      .catch(() => !gone && setReceipts([]));
    void api.get<{ tasks: TaskView[] }>(`/api/sessions/${sessionId}/tasks`).then((answer) => !gone && setTasks(answer.tasks)).catch(() => !gone && setTasks([]));
    return () => {
      gone = true;
    };
  }, [sessionId, messages.length]);
  const jobs = backgroundJobs(messages);
  const sent = sentFiles(messages);
  const base = sessionBase(sessionId);
  const empty = jobs.length === 0 && sent.length === 0 && (receipts?.length ?? 0) === 0;
  return (
    <div className="jobs">
      {tasks && tasks.length > 0 && (
        <section className="dt-section task-list">
          <div className="dt-label"><span>{t("panel.jobs.tasks")}</span><span className="dt-aside">{tasks.length}</span></div>
          {tasks.map((task) => (
            <div key={task.id} className="aside-row task-row">
              <span className={`dot ${task.state}`} />
              <button className="grow name task-open" onClick={() => task.child_session_id ? onOpenSession?.(task.child_session_id) : task.result_ref ? onOpen({ base, path: task.result_ref }) : undefined}>{task.title}</button>
              <span className="sub">{t(`task.state.${task.state}`)}</span>
              {task.stop_supported && <button className="iconbtn small quiet task-stop" aria-label={t("task.stop")} title={t("task.stop")} onClick={async () => { await api.post(`/api/sessions/${sessionId}/tasks/${encodeURIComponent(task.id)}/stop`, {}); await loadTasks(); }}><Icon name="stop" size={14} /></button>}
            </div>
          ))}
        </section>
      )}
      {empty && (
        <div className="empty">
          <b>{t("panel.jobs.empty.title")}</b>
          <div>{t("panel.jobs.empty.body")}</div>
        </div>
      )}
      {jobs.length > 0 && (
        <section className="dt-section">
          <div className="dt-label"><span>{t("panel.jobs.background")}</span><span className="dt-aside">{jobs.length}</span></div>
          {jobs.map((j) => (
            <button key={j.id} className="aside-row link job-row" onClick={() => onOpen({ base, path: j.log })} title={j.command}>
              <Icon name="terminal" size={16} />
              <span className="grow name mono">{j.command}</span>
              <span className="sub">{timeAgo(j.at)}</span>
            </button>
          ))}
        </section>
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
