// A task of the board of every chat and project, as `GET /api/board` lists it, and what the Board
// page, its task pane and a chat's own board read from it: the columns, the moves the host allows,
// what a card waits on, its due date. Pure, so the rules are tested without a browser.

import { absDate } from "../format";
import { t } from "../i18n";
import type { ChatFinishInfo } from "./ChatFinish";

export type Status = "todo" | "doing" | "review" | "done" | "blocked" | "dropped";

/** Why a card waits on the operator: an open question or approval tied to it, review, or a card the
 *  operator keeps for themselves. */
export type NeedsReason = "question" | "approval" | "review" | "assigned";

export type BriefField = "objective" | "deliverable" | "boundaries" | "done_when";

/** Who does the card: a member of a project's team, the operator, or (null) the agent of the chat
 *  that holds it. The operator's entry carries no name: the app says "you". */
export type Assignee = { kind: "staff"; id: string; name: string; color: string } | { kind: "operator" };

export type BoardTask = {
  id: string;
  title: string;
  status: Status;
  priority: number;
  acceptance: string;
  checklist: { id?: string; text: string; done: boolean }[];
  entity_revision?: number;
  depends_on: string[];
  session_id: string | null;
  origin_session_id: string | null;
  notes: string;
  created_at: string;
  updated_at: string;
  brief?: Partial<Record<BriefField, string>>;
  project_id?: string | null;
  project_name?: string | null;
  assignee?: Assignee | null;
  needs_you?: NeedsReason | null;
  finished_by?: ChatFinishInfo | null;
  /** The due instant in UTC; a bare date the operator picked is stored as the end of that day. */
  due_at?: string | null;
  /** Past its due instant with the work still open, as the host decides it. */
  overdue?: boolean;
};

/** One line of a card's history, as `GET /api/board/{id}/events` lists it, oldest first. The host keeps
 *  every change in a table of its own; a line marked `backfilled` was rebuilt when that table was made,
 *  from what was left of the card then, so what came before it is not all there. */
export type TaskEvent = {
  seq: number;
  type: string;
  at: string;
  actor: { kind: string; id: string | null; name: string | null };
  /** The column before and after, for a creation or a move; a rebuilt creation's column is unknown (null). */
  from: string | null;
  to: string | null;
  text: string;
  /** The command that made the change (`board.task.close`, `staff.report` …); null for the system's own. */
  operation?: string | null;
  backfilled?: boolean;
  /** Which field a `task.changed` line is about, with its old and new value for a title or a priority. */
  field?: "title" | "priority" | "due_at";
  change?: { from: unknown; to: unknown };
  /** The card whose closing or acceptance moved this one, for a dependent that was waiting on it. */
  cause?: { task_id: string; title: string | null };
  assignee?: { kind: "operator" } | { kind: "staff"; id: string; name: string | null } | null;
  due_at?: string | null;
};

export const OPEN_COLUMNS: Status[] = ["todo", "doing", "review", "blocked"];
export const FINISHED: Status[] = ["done", "dropped"];
export const isFinished = (task: { status: string }) => task.status === "done" || task.status === "dropped";

export const columnLabel = (status: Status) => t(`board.col.${status}`);

/** The priority every card is made with unless someone chose another: a card at it says nothing about
 *  priority, so only the others are marked. */
export const DEFAULT_PRIORITY = 3;

/**
 * The columns the operator may move a card to with a plain edit, as the host's update command allows
 * them: to do, blocked and dropped from an open card that is not in review; "doing" only for a card
 * the operator keeps for themselves (the agents' work moves by launches and reports, never by a drag);
 * nothing out of review or done, which take a verdict or a reopening of a chat's finish (the pane's own
 * buttons). The host stays the judge: a move it refuses is reported at the control that asked.
 */
export function movesFor(task: Pick<BoardTask, "status" | "assignee">): Status[] {
  const own = task.assignee?.kind === "operator";
  switch (task.status) {
    case "todo": return own ? ["doing", "blocked", "dropped"] : ["blocked", "dropped"];
    case "blocked": return own ? ["todo", "doing", "dropped"] : ["todo", "dropped"];
    case "doing": return own ? ["todo", "blocked"] : [];
    case "dropped": return ["todo"];
    default: return [];
  }
}

/** What a card is waiting on or doing right now, for its coloured line: the operator (orange), or its
 *  chat at work (cyan). Null for a card that is simply queued or finished. */
export type CardState = { tone: "attention" | "live"; text: string } | null;

export function cardState(task: BoardTask, running: (sessionId: string) => boolean): CardState {
  if (isFinished(task)) return null;
  if (task.needs_you) return { tone: "attention", text: t(`board.needs.${task.needs_you}`) };
  const chat = task.session_id ?? task.origin_session_id;
  if (task.status === "doing" && chat && running(chat)) return { tone: "live", text: t("board.state.running") };
  return null;
}

/** Who does the card, in the card's words: "you", a member's name, or the agent of its chat. */
export function assigneeName(task: Pick<BoardTask, "assignee">): string {
  const who = task.assignee;
  if (!who) return t("board.who.agent");
  return who.kind === "operator" ? t("board.who.you") : who.name;
}

/** A due date chosen as a day is stored as 23:59:59 UTC of that day, and is that day wherever the
 *  reader is: read in a zone east of Greenwich it would otherwise turn into the next morning. */
export function dueDay(dueAt: string): string | null {
  const day = /^(\d{4}-\d{2}-\d{2})T23:59:59(?:\.0+)?(?:Z|\+00:00)$/.exec(dueAt);
  return day ? day[1] : null;
}

/** The local calendar day of an instant, as YYYY-MM-DD. */
function localDay(at: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`;
}

/** The day a due date falls on, as the date field holds it. */
export function dueValue(dueAt: string | null | undefined): string {
  if (!dueAt) return "";
  return dueDay(dueAt) ?? localDay(new Date(dueAt));
}

/** "today", "tomorrow", "yesterday" or "28 Nov", for the card; with `full`, the pane's "Today, 25 Nov". */
export function dueLabel(dueAt: string, now = new Date(), full = false): string {
  const day = dueValue(dueAt);
  const shift = (days: number) => localDay(new Date(now.getFullYear(), now.getMonth(), now.getDate() + days));
  // Noon of the day, so the short date is that day in every zone.
  const date = absDate(`${day}T12:00:00`);
  const near = day === shift(0) ? "today" : day === shift(1) ? "tomorrow" : day === shift(-1) ? "yesterday" : null;
  if (!near) return date;
  return full ? t(`board.due.${near}.full`, { date }) : t(`board.due.${near}`);
}

/** The words for a priority in the pane: "High · P2". */
export const priorityLabel = (priority: number) => t("board.priority.named", { name: t(`board.priority.${priority}`), n: priority });

/** Whether `text` says no more than `other`: one contains the other once case, spaces and the final
 *  full stop are set aside. */
export function restates(text: string, other: string | undefined): boolean {
  const norm = (s: string) => s.toLowerCase().replace(/\s+/g, " ").trim().replace(/[.;:!]+$/, "");
  const a = norm(text);
  const b = norm(other ?? "");
  return Boolean(a && b) && (a.includes(b) || b.includes(a));
}

/**
 * A task's words in two blocks, Description and Acceptance. A project's task carries a four-part
 * brief; read as four headed sections beside the acceptance and the checklist they made the pane a
 * form to fill rather than a task to read. The objective opens the description, the deliverable and
 * the boundaries follow it under a lead word; "done when" and the acceptance are one block, and a
 * "done when" that only restates the acceptance is said once, in the longer words.
 */
export function taskText(task: Pick<BoardTask, "acceptance" | "brief">): { description: { lead?: string; text: string }[]; acceptance: string[] } {
  const brief = task.brief ?? {};
  const said = (field: BriefField) => (brief[field] ?? "").trim();
  const description: { lead?: string; text: string }[] = [];
  if (said("objective")) description.push({ text: said("objective") });
  if (said("deliverable")) description.push({ lead: t("pboard.brief.deliverable"), text: said("deliverable") });
  if (said("boundaries")) description.push({ lead: t("pboard.brief.boundaries"), text: said("boundaries") });
  const acceptance = (task.acceptance ?? "").trim();
  const doneWhen = said("done_when");
  const both = acceptance && doneWhen && restates(acceptance, doneWhen)
    ? [acceptance.length >= doneWhen.length ? acceptance : doneWhen]
    : [doneWhen, acceptance].filter(Boolean);
  return { description, acceptance: both };
}

/** A short handle for a card: its id, which is already six characters; a longer one is cut. */
export const shortId = (id: string) => (id.length > 8 ? id.slice(0, 8) : id).toUpperCase();

/** The cards in the order a column reads them: the most urgent first, then the most recently touched. */
export const byUrgency = (a: BoardTask, b: BoardTask) => a.priority - b.priority || Date.parse(b.updated_at) - Date.parse(a.updated_at);

/** One history line in the reader's language, from the event's fields rather than the host's English. */
export function eventText(event: TaskEvent): string {
  const column = (status: string | null) => (status && ["todo", "doing", "review", "done", "blocked", "dropped"].includes(status) ? columnLabel(status as Status) : status ?? "?");
  const priority = (value: unknown) => (typeof value === "number" && value >= 1 && value <= 5 ? t(`board.priority.${value}`) : String(value ?? "?"));
  switch (event.type) {
    // A creation rebuilt from an old card does not know its column, and saying "To do" would be a guess.
    case "task.created": return event.to ? t("board.event.created", { column: column(event.to) }) : t("board.event.createdBare");
    case "task.moved": return t("board.event.moved", { from: column(event.from), to: column(event.to) });
    case "task.assigned":
      if (!event.assignee) return t("board.event.unassigned");
      return event.assignee.kind === "operator" ? t("board.event.kept") : t("board.event.assigned", { name: event.assignee.name ?? t("board.event.member") });
    case "task.accepted": return t("board.event.accepted");
    case "task.removed": return t("board.event.removed");
    case "task.restored": return t("board.event.restored");
    default:
      if (event.field === "title") return t("board.event.titled", { title: String(event.change?.to ?? "") });
      if (event.field === "priority") return t("board.event.prioritised", { from: priority(event.change?.from), to: priority(event.change?.to) });
      if (event.field === "due_at" || "due_at" in event) return event.due_at ? t("board.event.due", { date: dueLabel(event.due_at) }) : t("board.event.dueCleared");
      return t("board.event.changed");
  }
}

/** Why a dependent moved: the card it waited on was closed by hand or its result was accepted. */
export function eventCause(event: TaskEvent): string {
  if (!event.cause) return "";
  const title = event.cause.title ?? t("board.event.cause.unnamed");
  return event.operation?.endsWith(".close") ? t("board.event.cause.closed", { title }) : t("board.event.cause.finished", { title });
}

/** Who did it, when the event names a chat: "in Menu planning". */
export function eventActor(event: TaskEvent): string {
  if (event.actor.kind === "operator") return t("board.event.byYou");
  return event.actor.name ? t("board.event.inChat", { name: event.actor.name }) : "";
}
