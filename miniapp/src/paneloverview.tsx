// The panel's front page: how the chat stands in one line (working for six minutes, waiting for the
// operator, failed), its title, what it runs on, then one row a tool with what is behind it — two
// files changed, a helper at work, the context a fifth full — and the chat's tasks with their
// progress. Every row opens its tool as a tab, so the Overview is both the summary and the way in.

import type { ReactNode } from "react";
import type { TaskView } from "./api";
import type { PanelTab } from "./panel";
import { TAB_ICON } from "./panelhost";
import { Icon } from "./icons";
import { t } from "./i18n";
import { Elapsed } from "./details";
import type { BoardTask } from "./board/tasks";
import { useQuery } from "./store";
import { taskSub } from "./jobs";
import { commandPreview } from "./format";

/** One row: the tool, what it holds in a few words, and a figure at its end in the tone of what it says. */
export type OverviewRow = { tab: PanelTab; name: string; text: ReactNode; tag?: string; tone?: "live" | "attn" | "add" };

export type OverviewStatus = { tone: "live" | "attn" | "bad" | "idle"; word: string; since?: string | null };

export function Overview({ sessionId, board, status, title, meta, rows, jobs, onOpen, onBoard }: {
  sessionId: string;
  /** The chat's tasks (what it and its helpers made), or undefined until they are read. */
  board: BoardTask[] | undefined;
  status: OverviewStatus;
  title: string;
  meta: ReactNode;
  rows: OverviewRow[];
  /** Whether the Terminal row is drawn: the session runs jobs (an orchestrator runs none). */
  jobs?: boolean;
  onOpen: (tab: PanelTab) => void;
  /** The way to the chat's tasks (its Board tab); left out, the block has no way there. */
  onBoard?: () => void;
}) {
  const tasks = useQuery<{ tasks: TaskView[] }>(jobs ? `/api/sessions/${encodeURIComponent(sessionId)}/tasks` : null, { staleMs: 5000 }).data?.tasks;
  const job = jobRow(tasks);
  // The Terminal row stands where the mockup's work group has it: after the changes and the helpers.
  const at = rows.findIndex((row) => row.tab !== "changes" && row.tab !== "board" && row.tab !== "subagents" && row.tab !== "questions");
  const all = job ? [...rows.slice(0, at < 0 ? rows.length : at), job, ...rows.slice(at < 0 ? rows.length : at)] : rows;
  return (
    <div className="ov" data-tool-page="overview">
      <div className="ov-head">
        <div className={`ov-status ${status.tone}`} data-status={status.tone}>
          <span className="ov-dot" aria-hidden />
          <span>{status.word}{status.since ? <> · <Elapsed since={status.since} /></> : null}</span>
        </div>
        <h2 className="ov-title">{title}</h2>
        <div className="ov-meta">{meta}</div>
      </div>
      <div className="ov-rows">
        {all.map((row) => (
          <button key={row.tab} type="button" className="ov-row" data-row={row.tab} onClick={() => onOpen(row.tab)}>
            <Icon name={TAB_ICON[row.tab]} size={16} />
            <span className="ov-row-name">{row.name}</span>
            <span className="ov-row-text">{row.text}</span>
            {row.tag && <span className={`ov-row-tag ${row.tone ?? ""}`}>{row.tag}</span>}
            <Icon name="forward" size={14} />
          </button>
        ))}
      </div>
      {board && board.length > 0 && <BoardBlock tasks={board} onOpen={onBoard} />}
    </div>
  );
}

/** The newest job as a row: its command and how it ended, or how long it runs. */
function jobRow(tasks: TaskView[] | undefined): OverviewRow | null {
  const own = (tasks ?? []).filter((task) => task.kind !== "agent");
  if (own.length === 0) return null;
  const running = own.filter((task) => task.state === "running").length;
  const last = [...own].sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at))[0];
  const what = last.command ? commandPreview(last.command) : last.title;
  return { tab: "jobs", name: t("panel.tab.jobs"), text: `${what} — ${taskSub(last, Date.now()).text}`, tag: running ? t("panel.ov.running", { n: running }) : String(own.length), tone: running ? "live" : undefined };
}

const ORDER = ["review", "doing", "blocked", "todo", "done", "dropped"];

/** The chat's tasks: how many of them are done, how many stand where, and the open ones by name with a
 *  ring in the tone of where they stand — the nearest thing to a plan the host keeps for a chat. */
function BoardBlock({ tasks, onOpen }: { tasks: BoardTask[]; onOpen?: () => void }) {
  const closed = tasks.filter((task) => task.status === "done" || task.status === "dropped").length;
  const counts = ORDER.map((status) => ({ status, n: tasks.filter((task) => task.status === status).length })).filter((c) => c.n > 0);
  const open = tasks.filter((task) => task.status !== "done" && task.status !== "dropped").sort((a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status));
  const shown = open.slice(0, 5);
  const percent = tasks.length ? Math.round((100 * closed) / tasks.length) : 0;
  return (
    <section className="ov-board" data-board aria-label={t("panel.ov.board")}>
      <div className="ov-board-head">
        <span className="ov-board-title">{t("panel.ov.board.of", { done: closed, all: tasks.length })}</span>
        {onOpen && <button type="button" className="iconbtn small" onClick={onOpen} aria-label={t("panel.ov.board.open")} title={t("panel.ov.board.open")}><Icon name="external" size={14} /></button>}
      </div>
      <div className="ov-board-bar" role="img" aria-label={t("panel.ov.board.bar", { n: percent })}><i style={{ width: `${percent}%` }} /></div>
      <div className="ov-board-counts">{counts.map((c) => <span key={c.status} data-count={c.status}>{t(`panel.ov.board.${c.status}`, { n: c.n })}</span>)}</div>
      <ul className="ov-tasks">
        {shown.map((task) => (
          <li key={task.id} className={`ov-task ${task.status} ${task.needs_you ? "you" : ""}`}>
            <span className="ov-ring" aria-hidden />
            <span className="ov-task-title">{task.title}</span>
            {(task.needs_you || task.status === "review") && <span className="ov-task-note">{t(task.status === "review" ? "panel.ov.task.review" : "panel.ov.task.you")}</span>}
          </li>
        ))}
        {open.length > shown.length && <li className="ov-task more">{t("panel.ov.board.more", { n: open.length - shown.length })}</li>}
      </ul>
    </section>
  );
}
