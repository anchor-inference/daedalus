// Messages between the agents of a project (SendToAgent), as the feeds draw them: a member's Feed
// lists what it sent and received among its own turns, and the orchestrator's chat reads them back out
// of the event lines it was woken with. Pure: the views are in `agentfeed.tsx`.

import type { AgentMessage, AgentParty, StaffTurn } from "../api";

const enc = encodeURIComponent;

/** How many of a member's messages its Feed reads: the newest, which is what a Feed shows. */
export const AGENT_MESSAGES_N = 50;

/** The log route, narrowed to one member when `staffId` is given. Every key starts with
 *  `agentMessagesBase(projectId)`, so one invalidation refreshes them all. */
export function agentMessagesKey(projectId: string, staffId?: string | null, limit = AGENT_MESSAGES_N): string {
  return `${agentMessagesBase(projectId)}?${staffId ? `staff=${enc(staffId)}&` : ""}limit=${limit}`;
}

export function agentMessagesBase(projectId: string): string {
  return `/api/projects/${enc(projectId)}/agent-messages`;
}

/** "to" when the member sent it, "from" when it received it. */
export function direction(message: Pick<AgentMessage, "sender">, staffId: string): "to" | "from" {
  return message.sender.staff_id === staffId ? "to" : "from";
}

/** The other side of a message, seen from the member. */
export function otherParty(message: Pick<AgentMessage, "sender" | "target">, staffId: string): AgentParty {
  return direction(message, staffId) === "to" ? message.target : message.sender;
}

/** Where a message that asked for an answer stands: still waiting, answered, or given up on. Null
 *  for one that asked for none, a reply, or one that never went. */
export function waitState(message: Pick<AgentMessage, "kind" | "wait_s" | "state">): "waiting" | "answered" | "unanswered" | null {
  if (message.kind === "reply" || !message.wait_s) return null;
  if (message.state === "sent") return "waiting";
  if (message.state === "answered" || message.state === "unanswered") return message.state;
  return null;
}

// The line the host puts over a message it types into a member's session (agent_mesh.py `_header`):
// "[message am-… from Ada, another agent of your project, hop 1 of 5: …]" or "[message am-… from your
// orchestrator]". The CLI's transcript keeps it as a turn of the person at the keyboard.
const RECEIVED_RE = /^\[message (am-[0-9a-z]+) from /;

/** The id of the agent message a transcript turn is, or null for any other turn. */
export function receivedId(turn: Pick<StaffTurn, "role" | "text">): string | null {
  if (turn.role !== "user" || !turn.text) return null;
  return RECEIVED_RE.exec(turn.text)?.[1] ?? null;
}

/** The words of a received message without the host's line over it. */
export function receivedBody(text: string): string {
  const cut = text.indexOf("\n");
  return cut < 0 ? "" : text.slice(cut + 1).trim();
}

export type FeedEntry = { kind: "turn"; turn: StaffTurn; message: AgentMessage | null } | { kind: "agent"; message: AgentMessage };

/**
 * The member's turns with its messages between them, oldest first. A message it received and took is
 * already a turn of its transcript: that turn is drawn as the message (`message` set) and the message
 * is not listed again. Every other message — sent, a reply, one not taken yet, a refused one — goes
 * before the first turn that started after it. A turn with no start time keeps its place.
 */
export function feedEntries(turns: readonly StaffTurn[], messages: readonly AgentMessage[]): FeedEntry[] {
  const byId = new Map(messages.map((m) => [m.id, m]));
  const taken = new Set<string>();
  const entries: FeedEntry[] = turns.map((turn) => {
    const id = receivedId(turn);
    if (id) taken.add(id);
    return { kind: "turn", turn, message: id ? byId.get(id) ?? null : null };
  });
  const rest = messages.filter((m) => !taken.has(m.id)).sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
  if (rest.length === 0) return entries;
  const out: FeedEntry[] = [];
  let next = 0;
  for (const entry of entries) {
    const started = entry.kind === "turn" && entry.turn.started_at ? Date.parse(entry.turn.started_at) : NaN;
    while (Number.isFinite(started) && next < rest.length && Date.parse(rest[next].created_at) < started) out.push({ kind: "agent", message: rest[next++] });
    out.push(entry);
  }
  while (next < rest.length) out.push({ kind: "agent", message: rest[next++] });
  return out;
}

/** A message between agents read back out of one line of a wake-up (orchestrator.py
 *  `_agent_message_line`). The host writes those sentences in one place and in English. */
export type AgentLine = {
  kind: "message" | "reply";
  state: "sent" | "refused" | "failed";
  from: string;
  to: string;
  /** The message's id; for a reply, the id of the message it answers. Absent on a refusal. */
  id: string;
  hop: number | null;
  wait: boolean;
  text: string;
  detail: string;
};

const SENT_RE = /^(.+?) → (.+?), agent message (am-[0-9a-z]+) \(hop (\d+)\)(; .+? waits for the answer)?: "([\s\S]*)"$/;
const REFUSED_RE = /^(.+?)'s message to (.+?) was refused \(([\s\S]*)\): "([\s\S]*)"$/;
const FAILED_RE = /^(.+?)'s message (am-[0-9a-z]+) to (.+?) was not delivered \(([\s\S]*)\): "([\s\S]*)"$/;
const ANSWERED_RE = /^(.+?) answered (.+?)'s message ((?:am-[0-9a-z]+)?)( \(the end of its turn\))?: "([\s\S]*)"$/;
const UNANSWERED_RE = /^(.+?) did not answer (.+?)'s message ((?:am-[0-9a-z]+)?): ([\s\S]*)$/;

export function parseAgentLine(text: string): AgentLine | null {
  let m = SENT_RE.exec(text);
  if (m) return { kind: "message", state: "sent", from: m[1], to: m[2], id: m[3], hop: Number(m[4]), wait: !!m[5], text: m[6], detail: "" };
  m = FAILED_RE.exec(text);
  if (m) return { kind: "message", state: "failed", from: m[1], to: m[3], id: m[2], hop: null, wait: false, text: m[5], detail: m[4] };
  m = REFUSED_RE.exec(text);
  if (m) return { kind: "message", state: "refused", from: m[1], to: m[2], id: "", hop: null, wait: false, text: m[4], detail: m[3] };
  m = ANSWERED_RE.exec(text);
  if (m) return { kind: "reply", state: "sent", from: m[1], to: m[2], id: m[3], hop: null, wait: false, text: m[5], detail: m[4] ? "turn" : "" };
  m = UNANSWERED_RE.exec(text);
  if (m) return { kind: "reply", state: "failed", from: m[1], to: m[2], id: m[3], hop: null, wait: false, text: "", detail: m[4] };
  return null;
}

/** The colour of an agent line in the orchestrator's card: a refusal is a warning, a failure an
 *  error, an answer good news, and a message on its way plain news. */
export function agentLineTone(line: Pick<AgentLine, "kind" | "state">): "ok" | "warn" | "bad" | "info" {
  if (line.state === "refused") return "warn";
  if (line.state === "failed") return line.kind === "reply" ? "warn" : "bad";
  return line.kind === "reply" ? "ok" : "info";
}
