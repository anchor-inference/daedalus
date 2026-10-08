// The orchestrator's chat as the operator reads it at a glance, decided without a browser: the one
// line of counts over the composer and the list it opens (the work in hand, the results waiting for
// the orchestrator's decision, what it promised), and what became of each of the operator's messages.
// Everything here is read from the project's rows (`GET /api/projects/<id>/focus-state`), never from
// the model's retelling: the retelling is what lost the operator's corrections in the first place.

import type { MessageView, ReplyRef } from "../api";

export type FocusCounts = { in_work: number; decisions: number; waiting_for_you: number; unconfirmed: number; commitments: number };

export type FocusGoal = {
  task_id: string;
  title: string;
  status: string;
  acceptance: string;
  owner: { id: string; name: string } | null;
  /** Who worked the card last, when nobody holds it now: rework goes back to them. */
  previous_owner: string | null;
  /** What the card waits on, in the words of its notes, or the host's "waiting for the orchestrator's decision". */
  next: string;
  updated_at: string;
};

export type OpenResultCause = "report_done" | "report_stuck" | "report_needs_input" | "task_unowned";

export type OpenResult = { id: number | string; cause: OpenResultCause | string; task_id: string | null; title: string | null; staff_name: string | null; summary: string; opened_at: string; reminded: boolean };

export type AcceptedResult = { task_id: string; title: string; result_id: string; contract_revision: number; current_contract_revision: number; attempt_id: string | null; original_digest: string; author: string | null; created_at: string };

export type Commitment = { id: number | string; text: string; task_id: string | null; message_seq: number | null; at: string };

export type RequirementDelivery = { staff_name: string; acknowledged: boolean; opened: boolean; via: string; cli: boolean };

export type Receipt =
  | { kind: "requirement"; label: string; text: string; state: string; task_id: string; task_title: string; deliveries: RequirementDelivery[] }
  | { kind: "commitment"; id: number | string; text: string; task_id: string | null; kept: boolean };

export type FocusState = {
  project_id?: string;
  counts: FocusCounts;
  goals: FocusGoal[];
  open_results: OpenResult[];
  accepted_results?: AcceptedResult[];
  commitments: Commitment[];
  /** What each of the operator's messages turned into, by the message's seq (a string, as JSON keys are). */
  receipts: Record<string, Receipt[]>;
};

/** The counts the line names, in the order it names them. */
export const COUNT_KINDS = ["in_work", "decisions", "waiting_for_you", "unconfirmed", "commitments"] as const;
export type CountKind = (typeof COUNT_KINDS)[number];

/** Whether an answer is a focus state at all: an older host or a proxy's page is no answer, and the
 *  chat draws without the line rather than breaking on it. */
export function isFocusState(value: unknown): value is FocusState {
  const v = value as FocusState | null;
  return !!v && typeof v === "object" && !!v.counts && typeof v.counts === "object" && Array.isArray(v.goals) && Array.isArray(v.open_results) && Array.isArray(v.commitments);
}

/** The line's parts: only the counts that are not zero, so a quiet project has no line at all. */
export function goalLine(counts: Partial<FocusCounts> | null | undefined): { kind: CountKind; n: number }[] {
  if (!counts) return [];
  return COUNT_KINDS.map((kind) => ({ kind, n: Math.max(0, Math.floor(Number(counts[kind]) || 0)) })).filter((part) => part.n > 0);
}

/** Whether the line is worth an amber edge: something waits on the operator or on a decision. */
export function lineNeedsAttention(counts: Partial<FocusCounts> | null | undefined): boolean {
  return goalLine(counts).some((part) => part.kind === "decisions" || part.kind === "waiting_for_you");
}

/** The host writes this sentence when an open result holds a card; the app says it in the reader's language. */
export const DECISION_NEXT = "waiting for the orchestrator's decision";

export type GoalRow = {
  id: string;
  title: string;
  status: string;
  /** The member who holds the card; or, when nobody does, who held it last (and `unowned`). */
  owner: string;
  unowned: boolean;
  acceptance: string;
  /** What the card waits on: a key of the dictionary when the host's own words say it, else the notes' words. */
  next: { key: string } | { text: string } | null;
  /** The card waits on someone rather than moving: drawn in the warning tone. */
  blocked: boolean;
};

/** A goal as its row reads: card → owner → acceptance → next step or blocker. */
export function goalRow(goal: FocusGoal): GoalRow {
  const owner = goal.owner?.name ?? goal.previous_owner ?? "";
  const words = (goal.next ?? "").trim();
  const next = !words ? null : words === DECISION_NEXT ? { key: "goal.next.decision" } : { text: words };
  return {
    id: goal.task_id,
    title: goal.title,
    status: goal.status,
    owner,
    unowned: !goal.owner,
    acceptance: goal.acceptance ?? "",
    next,
    blocked: goal.status === "blocked" || !!next,
  };
}

/** The goals, those that wait on someone first: a blocked card is the one the operator came to find. */
export function goalRows(goals: FocusGoal[]): GoalRow[] {
  const rows = goals.map(goalRow);
  return [...rows.filter((r) => r.blocked), ...rows.filter((r) => !r.blocked)];
}

/** An open result, oldest first: the one that has waited longest is read first. */
export function resultRows(results: OpenResult[]): OpenResult[] {
  return [...results].sort((a, b) => a.opened_at.localeCompare(b.opened_at));
}

/** The key of the sentence an open result is told in; a cause this app does not know reads as a report. */
export function resultKey(cause: string): string {
  return ["report_done", "report_stuck", "report_needs_input", "task_unowned"].includes(cause) ? `goal.result.${cause}` : "goal.result.other";
}

/** The card a commitment or a result names, by its title when the goals know it. */
export function cardTitle(taskId: string | null | undefined, goals: FocusGoal[]): string {
  if (!taskId) return "";
  return goals.find((g) => g.task_id === taskId)?.title ?? "";
}

// ── what became of an operator's message ─────────────────────────────────────────────────────

/** How one member stands with one requirement, most advanced first: confirmed beats opened beats sent. */
export type DeliveryState = "confirmed" | "words" | "opened" | "sent";

export function deliveryState(d: RequirementDelivery): DeliveryState {
  if (d.acknowledged) return d.cli ? "words" : "confirmed";
  if (d.opened) return "opened";
  return "sent";
}

export type FateLine =
  | { kind: "requirement"; label: string; card: string; text: string; state: string; deliveries: { name: string; state: DeliveryState }[] }
  | { kind: "commitment"; text: string; kept: boolean };

export type MessageFate = {
  /** What the message turned into: requirements on cards and commitments to the operator. */
  lines: FateLine[];
  /** How it reached the model, when that was not the plain way: during a turn, or as one ended. */
  arrival: "steer" | "drained" | null;
  /** The orchestrator answered after it and nothing else says what became of it. */
  read: boolean;
};

/**
 * What the chat says under an operator's message. Its receipts first; a message with none that the
 * orchestrator has answered since says it was read, so a correction is never left looking ignored;
 * and the way it arrived is said whenever it was not a plain turn of its own. A message with nothing
 * to say about it (not read yet, arrived plainly) has an empty fate and draws nothing.
 */
export function messageFate(
  message: Pick<MessageView, "seq" | "delivery">,
  receipts: Record<string, Receipt[]> | null | undefined,
  lastAnswerSeq: number | null | undefined,
): MessageFate {
  const seq = message.seq ?? null;
  const own = seq != null ? receipts?.[String(seq)] ?? [] : [];
  const lines: FateLine[] = own.map((r) =>
    r.kind === "requirement"
      ? { kind: "requirement", label: r.label, card: r.task_title, text: r.text, state: r.state, deliveries: r.deliveries.map((d) => ({ name: d.staff_name, state: deliveryState(d) })) }
      : { kind: "commitment", text: r.text, kept: r.kept },
  );
  const arrival = message.delivery === "steer" || message.delivery === "drained" ? message.delivery : null;
  const read = lines.length === 0 && seq != null && lastAnswerSeq != null && lastAnswerSeq > seq;
  return { lines, arrival, read };
}

/** Whether a fate has anything to draw. */
export function hasFate(fate: MessageFate): boolean {
  return fate.lines.length > 0 || fate.arrival !== null || fate.read;
}

/** The seq of the latest answer of the model in a transcript page: what "read" is measured against. */
export function lastAnswerSeq(messages: readonly Pick<MessageView, "role" | "seq" | "internal">[] | null | undefined): number | null {
  let last: number | null = null;
  for (const m of messages ?? []) if (m.role === "assistant" && !m.internal && m.seq != null && (last === null || m.seq > last)) last = m.seq;
  return last;
}

// ── replying to something in the chat ────────────────────────────────────────────────────────

/** The host keeps up to this much of a quote; more is cut there anyway, so it is cut here, visibly. */
export const EXCERPT_MAX = 300;

/** A quote of a message, a line of an events card or a steps card: one line, markdown's marks off,
 *  cut with an ellipsis. The host takes the excerpt as written, so what the chip shows is what the
 *  orchestrator reads. */
export function excerptOf(text: string, max = EXCERPT_MAX): string {
  const flat = text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/[*_`#>|]+/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .split(/\s+/)
    .filter(Boolean)
    .join(" ");
  return flat.length > max ? `${flat.slice(0, max - 1).trimEnd()}…` : flat;
}

/** The body a message is posted with, with what it answers when the operator chose something. */
export function messageBody(text: string, opts: { followUp?: boolean; clientMessageId?: string; reply?: ReplyRef | null; secrets?: string[] }): Record<string, unknown> {
  return {
    text,
    ...(opts.followUp ? { follow_up: true } : {}),
    expected_running: !!opts.followUp,
    client_message_id: opts.clientMessageId,
    ...(opts.reply ? { reply_to: { seq: opts.reply.seq, excerpt: opts.reply.excerpt } } : {}),
    // The operator's secrets by name; their values went to the host on their own request.
    ...(opts.secrets?.length ? { secrets: opts.secrets } : {}),
  };
}

/** The text an upload carries when it answers something: the upload takes no reference, so the quote
 *  travels in the words, in the host's own form, rather than being dropped on the way. */
export function uploadText(text: string, reply: ReplyRef | null | undefined): string {
  return reply ? `[In reply to: «${reply.excerpt}»]\n${text}` : text;
}
