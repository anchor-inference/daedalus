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
import { absTime, commandPreview, untilShort } from "./format";
import { quotaTone } from "./detailsmodel";

/** One row: the tool, what it holds in a few words, and a figure at its end in the tone of what it says.
 *  `kind` tells two rows of one tool apart for a reader of the page (the Usage tool's row is either the
 *  subscription's windows or the metered spend). */
export type OverviewRow = { tab: PanelTab; name: string; text: ReactNode; tag?: string; tone?: "live" | "attn" | "add"; kind?: string };

/** A subscription window as the host reports it: its name ("5h", "weekly"), how much of it is used, when it starts again. */
export type QuotaWindow = { name: string; used_percent: number; resets_at?: number | string | null };

/** The windows of the subscription the chat's model runs on, each a thin meter and its percentage, in
 *  the attention colour once the app judges it near its limit; when it starts again is in its tooltip. */
export function QuotaWindows({ windows }: { windows: QuotaWindow[] }) {
  return (
    <span className="ov-quota">
      {windows.map((w) => {
        const percent = Math.round(w.used_percent);
        const tone = quotaTone(w.used_percent);
        const resets = resetDate(w.resets_at);
        const title = [`${w.name}: ${percent}%`, resets ? `${t("details.quota.resets")} ${untilShort(resets)} (${absTime(resets)})` : ""].filter(Boolean).join(" · ");
        return (
          <span key={w.name} className={`ov-window ${tone === "ok" ? "" : tone}`} data-quota={w.name} data-tone={tone} title={title}>
            <span className="ov-window-name">{w.name}</span>
            <span className="ov-meter" aria-hidden><i style={{ width: `${Math.max(0, Math.min(100, w.used_percent))}%` }} /></span>
            <span className="ov-window-pct num">{percent}%</span>
          </span>
        );
      })}
    </span>
  );
}

/** The host sends a reset as seconds since the epoch or as an ISO date. */
function resetDate(at: number | string | null | undefined): Date | null {
  if (at === null || at === undefined || at === "") return null;
  const date = new Date(typeof at === "number" && at < 1e12 ? at * 1000 : at);
  return Number.isNaN(date.getTime()) ? null : date;
}

export type OverviewStatus = { tone: "live" | "attn" | "bad" | "idle"; word: string; since?: string | null };

export function Overview({ sessionId, board, status, title, meta, rows, jobs, onOpen, onBoard, onDetails }: {
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
  /** Details, for a reader who closed its tab beside the Overview; left out while the tab is there. */
  onDetails?: () => void;
}) {
  const tasks = useQuery<{ tasks: TaskView[] }>(jobs ? `/api/sessions/${encodeURIComponent(sessionId)}/tasks` : null, { staleMs: 5000 }).data?.tasks;
  const job = jobRow(tasks);
  // The Terminal row stands with the rest of the chat's work: after the changes and the helpers.
  const at = rows.findIndex((row) => row.tab !== "changes" && row.tab !== "board" && row.tab !== "subagents" && row.tab !== "questions");
  const all = job ? [...rows.slice(0, at < 0 ? rows.length : at), job, ...rows.slice(at < 0 ? rows.length : at)] : rows;
  return (
    <div className="ov" data-tool-page="overview">
      <div className="ov-head">
        <div className="ov-head-line">
          <div className={`ov-status ${status.tone}`} data-status={status.tone}>
            <span className="ov-dot" aria-hidden />
            <span>{status.word}{status.since ? <> · <Elapsed since={status.since} /></> : null}</span>
          </div>
          {onDetails && <button type="button" className="ov-details-link" onClick={onDetails} title={t("panel.ov.details.title")}>{t("panel.ov.details")}</button>}
        </div>
        <h2 className="ov-title">{title}</h2>
        <div className="ov-meta">{meta}</div>
      </div>
      <div className="ov-rows">
        {all.map((row) => (
          <button key={row.tab} type="button" className="ov-row" data-row={row.tab} data-kind={row.kind} onClick={() => onOpen(row.tab)}>
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
