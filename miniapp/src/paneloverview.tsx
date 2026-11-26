// The panel's front page: how the chat stands in one line (working for six minutes, waiting for the
// operator, failed), its title, what it runs on, then one row a tool with what is behind it — two
// files changed, a helper at work, the context a fifth full — then the subscription's windows, and
// the chat's tasks with their progress. Every row opens its tool as a tab, so the Overview is both the
// summary and the way in.

import type { ReactNode } from "react";
import type { SessionToolGroup, SubagentModelsView, TaskView } from "./api";
import type { PanelTab } from "./panel";
import { TAB_ICON } from "./panelhost";
import { Icon } from "./icons";
import { t } from "./i18n";
import { Elapsed } from "./details";
import type { BoardTask } from "./board/tasks";
import { useQuery } from "./store";
import { useEvent } from "./events";
import { SHOWN_DIFFERENCES, differences, type Difference, type DifferenceInput, type DifferenceTarget, type McpView } from "./overviewfacts";
import { taskSub } from "./jobs";
import { commandPreview } from "./format";
import { quotaTone } from "./detailsmodel";
import { QuotaRows, type QuotaWindow } from "./quotarow";
import "./paneloverview.css";

/** One row: the tool, what it holds in a few words, and a figure at its end in the tone of what it says.
 *  `kind` names what a row stands for when its tool alone does not (the Usage row is the metered spend;
 *  a subscription is the block under the rows). */
export type OverviewRow = { tab: PanelTab; name: string; text: ReactNode; tag?: string; tone?: "live" | "attn" | "add"; kind?: string };

/** The subscription the chat's model runs on, as the Overview draws it: the provider by its whole name,
 *  the plan and the account when the host names them, and every window on a line of its own. */
export type OverviewUsage = { provider: string; name: string; plan?: string; account?: string; windows: QuotaWindow[]; limit?: boolean; note?: string };

/** The subscription as a block under the tools: a heading with the provider, its plan and the way to
 *  the Usage tool, then one line a window. It was one row of chips, which a provider with three long
 *  window names ("Gemini 4 Argon (high)") cut to letters, and which never said when a window resets. */
function UsageBlock({ usage, onOpen }: { usage: OverviewUsage; onOpen: () => void }) {
  const tone = usage.limit || usage.windows.some((w) => quotaTone(w.used_percent) === "bad") ? "bad" : usage.windows.some((w) => quotaTone(w.used_percent) !== "ok") ? "attn" : undefined;
  return (
    <section className="ov-usage" data-usage="subscription" data-provider={usage.provider} data-tone={tone} aria-label={usage.name}>
      <div className="ov-usage-head">
        <Icon name={TAB_ICON.usage} size={16} />
        <span className="ov-usage-name">{usage.name}</span>
        {(usage.plan || usage.account) && <span className="ov-usage-plan">{[usage.plan, usage.account].filter(Boolean).join(" · ")}</span>}
        {usage.limit && <span className="ov-usage-tag">{t("usage.limitreached")}</span>}
        <button type="button" className="ov-usage-open" onClick={onOpen} title={t("panel.ov.usage.open")}>{t("panel.tab.usage")}<Icon name="forward" size={14} /></button>
      </div>
      {usage.note && <p className="ov-usage-note">{usage.note}</p>}
      {usage.windows.length > 0 && <QuotaRows windows={usage.windows} />}
    </section>
  );
}

export type OverviewStatus = { tone: "live" | "attn" | "bad" | "idle"; word: string; since?: string | null };

export function Overview({ sessionId, board, status, title, meta, rows, usage, facts, jobs, onOpen, onBoard, onDetails, onDifference }: {
  sessionId: string;
  /** The chat's tasks (what it and its helpers made), or undefined until they are read. */
  board: BoardTask[] | undefined;
  status: OverviewStatus;
  title: string;
  meta: ReactNode;
  rows: OverviewRow[];
  /** The subscription the chat's model runs on, drawn under the rows; a metered model is a row instead. */
  usage?: OverviewUsage | null;
  /** What the chat is and holds, for the block of what sets it apart; the Overview reads the rest
   *  (its MCP servers, standing grants, helper models and tool groups) itself. Left out, no block. */
  facts?: Omit<DifferenceInput, "mcp" | "standing" | "helpers" | "toolGroups">;
  /** Whether the Jobs row is drawn: the session runs jobs (an orchestrator runs none). */
  jobs?: boolean;
  onOpen: (tab: PanelTab) => void;
  /** The way to the chat's tasks (its Board tab); left out, the block has no way there. */
  onBoard?: () => void;
  /** Details, for a reader who closed its tab beside the Overview; left out while the tab is there. */
  onDetails?: () => void;
  /** Where a row of the block leads: a page of Details, or the approval or the queue in the composer. */
  onDifference?: (target: DifferenceTarget) => void;
}) {
  const extras = useSettingsRead(facts ? sessionId : null);
  const different = facts ? differences({ ...facts, ...extras }) : [];
  const tasks = useQuery<{ tasks: TaskView[] }>(jobs ? `/api/sessions/${encodeURIComponent(sessionId)}/tasks` : null, { staleMs: 5000 }).data?.tasks;
  const job = jobRow(tasks);
  // The Jobs row stands with the rest of the chat's work: after the changes and the helpers.
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
      {usage && <UsageBlock usage={usage} onOpen={() => onOpen("usage")} />}
      {different.length > 0 && <DifferenceBlock rows={different} onOpen={(target) => onDifference?.(target)} />}
      {board && board.length > 0 && <BoardBlock tasks={board} onOpen={onBoard} />}
    </div>
  );
}

/** The lines of Details that are read from routes of their own, read again each time the Overview is
 *  shown (it is, after a change made in Details) and, for the standing grants, when an approval is
 *  answered anywhere. A route that fails or answers something else counts as the default. */
function useSettingsRead(sessionId: string | null): Pick<DifferenceInput, "mcp" | "standing" | "helpers" | "toolGroups"> {
  const base = sessionId ? `/api/sessions/${encodeURIComponent(sessionId)}` : null;
  const mcp = useQuery<McpView>(base && `${base}/mcp`, { staleMs: 2000 }).data;
  const standing = useQuery<{ standing?: unknown[] }>(base && `${base}/policy/standing`, { staleMs: 2000 });
  const helpers = useQuery<SubagentModelsView>(base && `${base}/subagent-models`, { staleMs: 2000 }).data;
  const groups = useQuery<{ groups?: SessionToolGroup[] }>(base && `${base}/tool-groups`, { staleMs: 2000 }).data;
  const refreshStanding = standing.refresh;
  useEvent(["permission.resolved"], (event) => { if (sessionId && event.session_id === sessionId) void refreshStanding(); }, [sessionId, refreshStanding]);
  return {
    mcp: mcp && Array.isArray(mcp.enabled) && Array.isArray(mcp.servers) ? mcp : null,
    standing: Array.isArray(standing.data?.standing) ? standing.data!.standing : null,
    helpers: helpers && Array.isArray(helpers.allowed) && Array.isArray(helpers.options) ? helpers : null,
    toolGroups: Array.isArray(groups?.groups) ? groups!.groups : null,
  };
}

/** What sets this chat apart, one 32 px row each: its name, its value in words and the way to the
 *  place it is changed. Absent when nothing does; past eight rows the rest are counted and left to
 *  Details, so the Overview stays something read at a glance. */
function DifferenceBlock({ rows, onOpen }: { rows: Difference[]; onOpen: (target: DifferenceTarget) => void }) {
  const shown = rows.slice(0, SHOWN_DIFFERENCES);
  const more = rows.length - shown.length;
  return (
    <section className="ov-diff" data-diff aria-label={t("panel.ov.diff")}>
      <h3 className="ov-diff-title">{t("panel.ov.diff")}</h3>
      <ul className="ov-diff-rows">
        {shown.map((row) => {
          const body = <>
            <Icon name={row.icon} size={16} />
            <span className="ov-diff-name">{row.name}</span>
            <span className={`ov-diff-value ${row.tone ?? ""}`}>{row.value}</span>
            {row.target && <Icon name="forward" size={14} />}
          </>;
          return (
            <li key={row.id}>
              {row.target
                ? <button type="button" className="ov-diff-row" data-diff-row={row.id} data-tone={row.tone} data-rank={row.rank} title={`${row.name}: ${row.value}`} onClick={() => onOpen(row.target)}>{body}</button>
                : <div className="ov-diff-row" data-diff-row={row.id} data-tone={row.tone} data-rank={row.rank} title={`${row.name}: ${row.value}`}>{body}</div>}
            </li>
          );
        })}
        {more > 0 && (
          <li>
            <button type="button" className="ov-diff-row more" data-diff-more onClick={() => onOpen({ details: "session" })}>
              <span className="ov-diff-name">{t("panel.ov.diff.more", { n: more })}</span>
              <Icon name="forward" size={14} />
            </button>
          </li>
        )}
      </ul>
    </section>
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
