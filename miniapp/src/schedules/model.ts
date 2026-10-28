// What the Schedules screen and the schedules dialog know about a schedule beyond its row: which group
// it is listed under, which conversation it belongs to, how its last run went, and which rows a
// filter keeps. Pure functions, so the list, the dialog and their tests read one answer.

import type { Schedule } from "../api";

export type ProposalFile = { name: string; size: number; digest: string };

export type ScheduleProposal = {
  id: string;
  name: string;
  prompt: string;
  cron: string | null;
  run_at: string | null;
  kind: Schedule["kind"];
  run_in?: string;
  next_run_at: string;
  file_count: number;
  files: ProposalFile[];
  legacy_file_review_required: boolean;
  source_session_id: string | null;
  source_project_id: string | null;
  proposal_revision: number;
  request_digest: string;
  status: "pending" | "withdrawn" | "accepted";
  accepted_schedule_id?: string | null;
  created_at: string;
};

/** The proposals route also says whether the operator reviews the agent's schedules, and so how long an Accept lasts. */
export type ProposalList = {
  entries: ScheduleProposal[];
  collection_revisions: Record<string, number>;
  require_approval?: boolean;
  approval_days?: number;
};

/** A row of the Schedules list as the host sends it, with the facts the list route adds to the table's own columns. */
export type ScheduleRow = Schedule & {
  grant_expires_at?: string | null;
  last_session_id?: string | null;
  source_session_id?: string | null;
  files?: string | null;
};

export type Cycle = {
  id: string;
  due_at: string;
  kind: string;
  manual?: number;
  effect_state: string | null;
  action_state: string;
  effect_error: string | null;
  session_id?: string | null;
  created_at?: string;
};

export type CycleHistory = {
  schedule_revision: number;
  authority_state: string;
  collection_revision: number | null;
  scope: { kind: string; id: string };
  cycles: Cycle[];
};

export type Group = "approval" | "active" | "paused" | "done";
export const GROUPS: Group[] = ["approval", "active", "paused", "done"];

export type Item =
  | { key: string; source: "schedule"; group: Group; schedule: ScheduleRow }
  | { key: string; source: "proposal"; group: "approval"; proposal: ScheduleProposal };

/**
 * Where a schedule is listed. One that lost its approval (an edit, a resume, a grant that ran out)
 * cannot fire however it is set, so it is listed with the requests that wait for the operator rather
 * than among the active ones it only looks like.
 */
export function scheduleGroup(s: ScheduleRow): Group {
  const live = !!s.enabled && !!s.next_run_at;
  if (live && s.authority_state === "needs_approval") return "approval";
  if (live) return "active";
  if (!s.enabled && (s.cron || (s.run_at && !s.last_run_at))) return "paused";
  return "done";
}

/**
 * The conversation a schedule belongs to: the one it runs in, or else the one that asked for it. A
 * schedule that starts a fresh conversation each time still belongs to the chat where it was made.
 */
export function conversationOf(item: Item): string | null {
  if (item.source === "proposal") return item.proposal.source_session_id;
  const s = item.schedule;
  return (s.run_in === "self" ? s.target_session : null) ?? s.source_session_id ?? s.created_by_session ?? s.target_session ?? null;
}

/** Whether a row concerns a conversation: made there, run there, or running there now. */
export function belongsTo(item: Item, sessionId: string): boolean {
  if (item.source === "proposal") return item.proposal.source_session_id === sessionId;
  const s = item.schedule;
  return [s.target_session, s.created_by_session, s.source_session_id, s.active_session_id, s.last_session_id].includes(sessionId);
}

export function itemsFrom(schedules: ScheduleRow[] | undefined, proposals: ScheduleProposal[] | undefined): Item[] {
  const rows: Item[] = (proposals ?? []).filter((p) => p.status === "pending").map((proposal) => ({ key: proposal.id, source: "proposal", group: "approval", proposal }));
  for (const schedule of schedules ?? []) rows.push({ key: schedule.id, source: "schedule", group: scheduleGroup(schedule), schedule });
  return rows;
}

const nextAt = (item: Item) => Date.parse((item.source === "proposal" ? item.proposal.next_run_at : item.schedule.next_run_at) ?? "") || Infinity;

/** The rows by group: what runs soonest first, and the finished ones by when they last ran. */
export function groupItems(items: Item[]): Record<Group, Item[]> {
  const by: Record<Group, Item[]> = { approval: [], active: [], paused: [], done: [] };
  for (const item of items) by[item.group].push(item);
  by.approval.sort((a, b) => nextAt(a) - nextAt(b));
  by.active.sort((a, b) => nextAt(a) - nextAt(b));
  by.paused.sort((a, b) => nameOf(a).localeCompare(nameOf(b)));
  const last = (item: Item) => (item.source === "schedule" ? Date.parse(item.schedule.last_run_at ?? "") || 0 : 0);
  by.done.sort((a, b) => last(b) - last(a));
  return by;
}

export const nameOf = (item: Item) => (item.source === "proposal" ? item.proposal.name : item.schedule.name);
export const promptOf = (item: Item) => (item.source === "proposal" ? item.proposal.prompt : item.schedule.prompt);

/** The rows a filter keeps: one conversation's, then those whose name, text or conversation title has every word typed. */
export function filterItems(items: Item[], opts: { query?: string; sessionId?: string | null; titles?: Record<string, string> }): Item[] {
  const words = (opts.query ?? "").toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return items.filter((item) => {
    if (opts.sessionId && !belongsTo(item, opts.sessionId)) return false;
    if (!words.length) return true;
    const chat = conversationOf(item);
    const hay = `${nameOf(item)}\n${promptOf(item)}\n${chat ? opts.titles?.[chat] ?? "" : ""}`.toLocaleLowerCase();
    return words.every((w) => hay.includes(w));
  });
}

export type LastRun = { status: "ok" | "failed" | "running"; at: string | null; sessionId: string | null };

/** How the last run went, and the conversation it can be read in, if the host knows one. */
export function lastRun(s: ScheduleRow): LastRun | null {
  if (s.active_session_id) return { status: "running", at: s.last_run_at, sessionId: s.active_session_id };
  if (!s.last_run_at) return null;
  const sessionId = s.last_session_id ?? (s.run_in === "self" ? s.target_session : null) ?? null;
  return { status: s.failure_count > 0 || s.last_error ? "failed" : "ok", at: s.last_run_at, sessionId };
}

/** The state of one occurrence in the history, in the words the screen has for it. */
export function cycleState(cycle: Cycle): "completed" | "failed" | "unknown" | "skipped" | "pending" {
  if (cycle.effect_state === "unknown") return "unknown";
  if (cycle.effect_state === "completed") return "completed";
  if (cycle.effect_state === "failed" || cycle.effect_state === "cancelled") return "failed";
  if (cycle.action_state === "skipped") return "skipped";
  return "pending";
}

/** The files a schedule hands its runs, read from the column the host keeps them in; a path list from before pinning is listed by name. */
export function scheduleFiles(s: ScheduleRow): { name: string; size?: number }[] {
  if (!s.files) return [];
  try {
    const raw = JSON.parse(s.files) as unknown;
    if (!Array.isArray(raw)) return [];
    return raw.flatMap((f) => (typeof f === "string" ? [{ name: f.split("/").pop() || f }] : f && typeof f === "object" && typeof (f as { name?: unknown }).name === "string" ? [{ name: (f as { name: string }).name, size: (f as { size?: number }).size }] : []));
  } catch {
    return [];
  }
}

/** The collection a proposal is decided in: its project's, or the global one. */
export const proposalScope = (p: ScheduleProposal) => (p.source_project_id ? `project:${p.source_project_id}` : "global:global");
