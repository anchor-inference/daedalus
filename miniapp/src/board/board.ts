// A project's board as the app draws it: the shapes the host sends, and the decisions the board makes
// from them — which column a task stands in, what order a column is read in, which chips a phone gets,
// what a card's status line says. Pure, so every one is tested without a browser.

import type { Harness, StaffStatus } from "../team/team";

export type TaskStatus = "todo" | "doing" | "review" | "done" | "blocked" | "dropped";

export type LaunchEffect = { state: string; error?: string | null; wait_reason?: string | null; wait_detail?: string | null };

/** A failed delivery is definitive; a stale or lost observation still blocks another launch. */
export function launchFeedback(effect: LaunchEffect | undefined, readFailed: boolean) {
  if (readFailed || !effect) return { key: "pboard.launch.unconfirmed", blocked: true, detail: "", wait: null };
  const keys: Record<string, string> = {
    pending: "pboard.launch.pending", claimed: "pboard.launch.delivering", completed: "pboard.launch.dispatched",
    failed: "pboard.launch.deliveryFailed", cancelled: "pboard.launch.cancelled",
  };
  let detail = effect.wait_detail || effect.error || "";
  try {
    const parsed = JSON.parse(detail);
    if (parsed && typeof parsed.detail === "string") detail = parsed.detail;
  } catch { /* A host refusal can be plain text. */ }
  return {
    key: keys[effect.state] || "pboard.launch.unconfirmed",
    blocked: !["failed", "cancelled", "completed"].includes(effect.state),
    detail,
    wait: effect.state === "pending" ? effect.wait_reason || null : null,
  };
}

/** The board's columns, left to right: what waits on the operator, then the work in the order it flows. */
export const COLUMNS = ["needs", "doing", "review", "queue", "done"] as const;
export type Column = (typeof COLUMNS)[number];

export const BRIEF_FIELDS = ["objective", "deliverable", "boundaries", "done_when"] as const;
export type BriefField = (typeof BRIEF_FIELDS)[number];
export type Brief = Record<BriefField, string>;

export type Assignee = {
  id: string;
  name: string;
  color: string;
  harness: Harness;
  archived_at: string | null;
  /** The member's live status, or "off" without a live session. */
  status: StaffStatus;
  /** Whether that live session works on this task; otherwise the status is about something else. */
  on_task: boolean;
  waiting_for: string;
  status_at: string | null;
  session_id: string | null;
};

export type ProjectTask = {
  id: string;
  entity_revision?: number;
  title: string;
  status: TaskStatus;
  priority: number;
  acceptance: string;
  checklist: CheckItem[];
  depends_on: string[];
  session_id: string | null;
  notes: string;
  created_at: string;
  updated_at: string;
  project_id: string | null;
  folder_id?: string | null;
  contract_revision?: number;
  current_attempt_id?: string | null;
  assignee_staff_id: string | null;
  brief: Brief;
  branch: string | null;
  merge_state: "" | "proposed" | "merged" | "conflict" | "rejected";
  assignee: Assignee | null;
  /** How far the work was accepted; absent from a host older than the task contract. */
  acceptance_state?: AcceptanceState;
  /** Priced native inference linked to the exact accepted worker attempt; null when it cannot be observed. */
  accepted_attempt_cost_microusd?: number | null;
  /** What the card must satisfy, each with who was given it; absent from an older host. */
  requirements?: Requirement[];
};

/** "handed_in": the member said it is done and nobody checked yet; "accepted": the orchestrator checked
 *  every item; "operator_approved": the operator accepted or merged it; "returned": checked and sent
 *  back for another round. Empty for a card nobody handed in. */
export type AcceptanceState = "" | "handed_in" | "accepted" | "operator_approved" | "returned";

/** The member's word for one item when it handed the work in: what it did and what came of it. */
export type Evidence = { how?: string; result?: string; by?: string; at?: string };
/** The orchestrator's verdict on one item. */
export type Mark = { ok: boolean; note?: string; by?: string };

/** One of the card's checks, named C1, C2 … by its place in the list. */
export type CheckItem = { text: string; done: boolean; evidence?: Evidence | null; mark?: Mark | null };

/** A requirement given to one member, and how far it got. `opened_at` is the host seeing a tool touch
 *  an input's file; a command-line member's tools are out of its sight, so there only its word counts. */
export type Delivery = {
  staff_id: string;
  staff_name: string;
  via: "brief" | "message";
  message_id?: string | null;
  delivered_at: string;
  acknowledged_at: string | null;
  opened_at: string | null;
  cli: boolean;
};

export type Requirement = {
  id: string;
  label: string;
  number: number;
  text: string;
  kind: "quality" | "scope" | "input" | "constraint";
  source: string;
  from_operator: boolean;
  state: "active" | "superseded" | "withdrawn";
  replaces: string | null;
  file_id: string | null;
  file_name: string;
  evidence: Evidence | null;
  mark: Mark | null;
  created_at: string;
  deliveries: Delivery[];
};

export type NeedsYou = {
  id: string;
  short_id: string;
  origin: "staff" | "orchestrator";
  kind: "question" | "permission" | "folder";
  text: string;
  suggestion: string;
  created_at: string;
  task_id: string | null;
  task_title: string | null;
  staff: { id: string; name: string; color: string; harness: Harness } | null;
  /** Where the request can be answered: the staff member's session or the orchestrator's. */
  session_id: string | null;
};

export type TeamMember = { id: string; name: string; color: string; harness: Harness; isolation: "shared" | "worktree" };

export type ProjectBoardData = {
  tasks: ProjectTask[];
  needs_you: NeedsYou[];
  counts: Record<TaskStatus | "needs_you", number>;
  staff: TeamMember[];
};

export type Arranged = { needs: NeedsYou[]; doing: ProjectTask[]; review: ProjectTask[]; queue: ProjectTask[]; done: ProjectTask[] };

/** The column a task stands in. A blocked task is still queued work: it waits for another one, not for a person. */
export function columnOf(status: TaskStatus): Exclude<Column, "needs"> {
  if (status === "doing") return "doing";
  if (status === "review") return "review";
  if (status === "done" || status === "dropped") return "done";
  return "queue";
}

const time = (iso: string) => Date.parse(iso) || 0;

/** The tasks by column, each column in the order it is read: the most urgent first, and in the queue
 *  what can start now before what waits for another task. */
export function arrange(data: Pick<ProjectBoardData, "tasks" | "needs_you">): Arranged {
  const out: Arranged = { needs: [...data.needs_you].sort((a, b) => time(a.created_at) - time(b.created_at)), doing: [], review: [], queue: [], done: [] };
  for (const task of data.tasks) out[columnOf(task.status)].push(task);
  const urgent = (a: ProjectTask, b: ProjectTask) => a.priority - b.priority || time(b.updated_at) - time(a.updated_at);
  out.doing.sort(urgent);
  out.review.sort(urgent);
  out.queue.sort((a, b) => Number(a.status === "blocked") - Number(b.status === "blocked") || a.priority - b.priority || time(a.created_at) - time(b.created_at));
  out.done.sort((a, b) => time(b.updated_at) - time(a.updated_at));
  return out;
}

/** How many a column holds. Finished tasks are counted by the host, since the board does not load them until asked. */
export function columnCount(column: Column, arranged: Arranged, counts?: ProjectBoardData["counts"]): number {
  if (column === "done") return counts ? (counts.done ?? 0) + (counts.dropped ?? 0) : arranged.done.length;
  return arranged[column].length;
}

/** The phone's chips: one per column that has something in it, and always "Needs you" when anything waits. */
export function chips(arranged: Arranged, counts?: ProjectBoardData["counts"]): { column: Column; count: number }[] {
  return COLUMNS.map((column) => ({ column, count: columnCount(column, arranged, counts) })).filter((c) => c.count > 0);
}

/** The sections a phone lists under its chips. No chip: everything open, with finished work folded
 *  away; a chip: that column alone, finished work included when that is the chip. */
export function sections(filter: Column | null, arranged: Arranged): Column[] {
  if (filter) return [filter];
  return COLUMNS.filter((c) => c !== "done" && arranged[c].length > 0);
}

/** The chip a tap leaves pressed: a second tap on the pressed one lets go of it. */
export function toggleFilter(current: Column | null, tapped: Column): Column | null {
  return current === tapped ? null : tapped;
}

/** The brief parts still empty. A staff member is not started on a task with any of them missing. */
export function missingBrief(brief: Brief): BriefField[] {
  return BRIEF_FIELDS.filter((field) => !brief[field]?.trim());
}

export type StatusLine =
  | { kind: "working"; status: StaffStatus; since: string | null; waiting: string }
  | { kind: "after"; title: string; more: number }
  | { kind: "waiting"; name: string }
  | { kind: "none" };

/** What a card says beside its assignee: their status while they work on this task, the task it
 *  waits for when it is blocked, who will take it. */
export function statusLine(task: ProjectTask, titles: Record<string, { title: string; status: TaskStatus }>): StatusLine {
  if (task.assignee?.on_task) return { kind: "working", status: task.assignee.status, since: task.assignee.status_at, waiting: task.assignee.waiting_for };
  const open = task.depends_on.filter((id) => titles[id] && titles[id].status !== "done" && titles[id].status !== "dropped");
  if (task.status === "blocked" && open.length > 0) return { kind: "after", title: titles[open[0]].title, more: open.length - 1 };
  if (task.assignee && (task.status === "todo" || task.status === "blocked")) return { kind: "waiting", name: task.assignee.name };
  return { kind: "none" };
}

/** Only the brief parts that changed, so an edit never writes back a part another window changed meanwhile. */
export function briefChanges(before: Brief, after: Brief): Partial<Brief> {
  const out: Partial<Brief> = {};
  for (const field of BRIEF_FIELDS) if ((before[field] ?? "") !== (after[field] ?? "")) out[field] = after[field];
  return out;
}

/** Where a task can be moved from where it stands. Done is not offered: a finished task is the
 *  operator's acceptance from review, which is its own button. */
export const NEXT: Record<TaskStatus, TaskStatus[]> = {
  todo: ["doing", "blocked", "dropped"],
  blocked: ["todo", "dropped"],
  doing: ["review", "todo", "blocked"],
  review: ["doing"],
  done: ["todo"],
  dropped: ["todo"],
};

export const emptyBrief = (): Brief => ({ objective: "", deliverable: "", boundaries: "", done_when: "" });

/** Why Merge cannot be pressed, as the host names it; the app words each code, and `text` is the host's own sentence. */
export type ReviewBlocker = { code: "status" | "branch" | "merged" | "dirty" | "moved" | "unknown" | "conflicts" | "checklist" | string; text: string };

/** What merging a staff branch would bring, from `GET /api/board/{task}/review`. */
export type Review = {
  task_id: string;
  title: string;
  status: TaskStatus;
  merge_state: ProjectTask["merge_state"];
  branch: string;
  base: string;
  current: string;
  head_sha?: string | null;
  base_sha?: string | null;
  current_sha?: string | null;
  merge_receipt?: { id: string; result_id: string; verdict_id: string; head_sha: string; base_sha: string; merge_sha: string; state: string; error: string | null } | null;
  folder: { id: string; path: string; label: string; env: string };
  exists: boolean;
  on_base: boolean;
  folder_clean: boolean;
  merged: boolean;
  commits: { sha: string; author: string; at: string; subject: string }[];
  more_commits: boolean;
  files: { path: string; added: number | null; removed: number | null }[];
  added: number;
  removed: number;
  patch: string;
  patch_complete: boolean;
  conflicts: string[] | null;
  receipts: { criterion: string; command: string; exit_code: number; passed: boolean; at: string }[];
  ci_status: "passed" | "blocked";
  ci_checks: { provider: string; repository_id: string; check_name: string; head_sha: string | null; state: string; delivery_id: string | null }[];
  can_merge: boolean;
  blockers: ReviewBlocker[];
};

/** The blocker codes the app has words for; any other is shown in the host's words. */
export const BLOCKER_CODES = ["status", "branch", "merged", "dirty", "moved", "unknown", "conflicts", "checklist"] as const;

/** The first reason Merge is disabled, the one the button's hint names; null when it may be pressed. */
export function mergeBlock(review: Pick<Review, "can_merge" | "blockers">): ReviewBlocker | null {
  if (review.can_merge) return null;
  // Conflicts first: they are the orchestrator's to sort out, and the rest the operator fixes in the folder.
  return review.blockers.find((b) => b.code === "conflicts") ?? review.blockers[0] ?? { code: "unknown", text: "" };
}

/** A task whose Accept is really Merge: its work is on a staff branch not merged yet. */
export function mergesOnAccept(task: Pick<ProjectTask, "branch" | "merge_state">): boolean {
  return Boolean(task.branch) && task.merge_state !== "merged";
}

/** The acceptance levels a card's chip names, and the tone each is drawn in. Only a card in review or
 *  done carries one: a card sent back is in progress again, and its own column already says so. */
export function acceptanceChip(task: Pick<ProjectTask, "status" | "acceptance_state">): { state: Exclude<AcceptanceState, "">; tone: "" | "ok" | "attn" } | null {
  const state = task.acceptance_state;
  if (!state || (task.status !== "done" && task.status !== "review")) return null;
  return { state, tone: acceptanceTone(state) };
}

/** Checked and approved are good news, returned wants attention, and handed in is only the member's
 *  word so far, which is drawn as neither. */
export function acceptanceTone(state: AcceptanceState): "" | "ok" | "attn" {
  if (state === "accepted" || state === "operator_approved") return "ok";
  if (state === "returned") return "attn";
  return "";
}

/** Whether the sheet has an acceptance section to draw: a level, or a check with a word or a mark on it. */
export function hasAcceptance(task: Pick<ProjectTask, "acceptance_state" | "checklist">): boolean {
  return Boolean(task.acceptance_state) || task.checklist.some((item) => item.evidence || item.mark);
}

/** Where a requirement came from, as a dictionary key and what fills it. A source of a kind this app
 *  does not know is the orchestrator's unless the host says it is the operator's. */
export function requirementSource(r: Pick<Requirement, "source" | "from_operator">): { key: "operator" | "answer" | "rule" | "orchestrator"; ref: string } {
  const [head, ...rest] = r.source.split(":");
  const ref = rest.join(":");
  if (head === "answer" && ref) return { key: "answer", ref };
  if (head === "rule" && ref) return { key: "rule", ref };
  return { key: r.from_operator || head === "operator" ? "operator" : "orchestrator", ref: "" };
}

export type DeliveryState = "opened" | "words" | "confirmed" | "sent";

/** How far a requirement got with one member. For an input, opening its file is the proof; a
 *  command-line member's confirmation of an input is only its word, since the host cannot see the file
 *  opened, and the sheet says so rather than pass it off as the same thing. */
export function deliveryState(d: Pick<Delivery, "opened_at" | "acknowledged_at" | "cli">, kind: Requirement["kind"]): DeliveryState {
  if (d.opened_at) return "opened";
  if (!d.acknowledged_at) return "sent";
  return kind === "input" && d.cli ? "words" : "confirmed";
}
