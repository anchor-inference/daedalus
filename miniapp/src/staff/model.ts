// A command-line staff member as the staff view decides it: how a receipt moves, how the Feed takes
// a fresh page of the transcript, what "now" means for this CLI, which answers a permission offers,
// when the keyboard banner shows, and what the header says about the turn. Pure, so each rule is
// tested without a browser; the components only draw what these return.
//
// Every choice that differs between the CLIs is read from the capability table the host sends
// (`HarnessCapabilities`), never from the harness's name: a CLI added to the table is drawn right
// without a line here.

import type { Ask, ChannelHealth, HarnessCapabilities, MessageState, StaffMessage, StaffTurn } from "../api";
import { DICT, t } from "../i18n";

/** The order a message moves in. A receipt never goes back: an event that arrives late (a replay
 *  after a reconnect) must not turn "accepted" into "sent". */
const ORDER: Record<MessageState, number> = { queued: 0, written: 1, submitted: 2, acknowledged: 3, failed: 3 };

/** A `staff.message` event applied to the rows on screen. A message the list does not hold yet is
 *  left to the next read of the list, which brings it with everything else about it. */
export function applyMessageEvent(rows: StaffMessage[], event: { message_id?: unknown; state?: unknown; error?: unknown }): StaffMessage[] {
  const id = typeof event.message_id === "string" ? event.message_id : "";
  const state = typeof event.state === "string" && event.state in ORDER ? (event.state as MessageState) : null;
  if (!id || !state) return rows;
  let changed = false;
  const out = rows.map((row) => {
    if (row.id !== id || row.state === state) return row;
    // Nothing leaves "accepted", and "failed" is only ever reached from a state before it.
    if (row.state === "acknowledged" || row.state === "failed" || ORDER[state] < ORDER[row.state]) return row;
    changed = true;
    return { ...row, state, error: typeof event.error === "string" ? event.error : row.error };
  });
  return changed ? out : rows;
}

/** The last messages of the strip above the composer: newest last, the way a conversation reads. */
export function stripRows(rows: StaffMessage[], n = 3): StaffMessage[] {
  return [...rows].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, n).reverse();
}

const PENDING: ReadonlySet<MessageState> = new Set(["queued", "written", "submitted"]);

/** The messages the Session tab lists: newest first, the one that needs a look on top. */
export function listRows(rows: StaffMessage[]): StaffMessage[] {
  return [...rows].sort((a, b) => b.created_at.localeCompare(a.created_at));
}

/**
 * What of the messages needs the operator: those still on their way, and those that failed with
 * nothing accepted since. A failure older than an accepted message is history, not a task; counting
 * it would keep the header's marker lit for ever after one bad afternoon.
 */
export function attention(rows: StaffMessage[]): { failed: StaffMessage[]; pending: StaffMessage[] } {
  const newest = listRows(rows);
  const accepted = newest.find((m) => m.state === "acknowledged");
  const failed = newest.filter((m) => m.state === "failed" && (!accepted || m.created_at > accepted.created_at));
  const pending = newest.filter((m) => PENDING.has(m.state));
  return { failed, pending };
}

/**
 * The messages the Feed shows after its last turn: the ones the transcript cannot have yet (on their
 * way) or never will (failed, unanswered by an accepted one), oldest first, the way the Feed reads.
 * An accepted message is already in the transcript as a turn of its own.
 */
export function outboxRows(rows: StaffMessage[]): StaffMessage[] {
  const { failed, pending } = attention(rows);
  return [...failed, ...pending].sort((a, b) => a.created_at.localeCompare(b.created_at));
}

/**
 * A page of the transcript merged into what the Feed holds. The Feed asks again from its last turn
 * (`since`), because that turn is the one still being written: the fresh copy of a turn replaces the
 * old one by its index, and new turns are added after it, in order.
 */
export function mergeTurns(have: StaffTurn[], fresh: StaffTurn[]): StaffTurn[] {
  if (fresh.length === 0) return have;
  const byIndex = new Map(have.map((turn) => [turn.index, turn]));
  for (const turn of fresh) byIndex.set(turn.index, turn);
  return [...byIndex.values()].sort((a, b) => a.index - b.index);
}

/** Where the next read of the transcript starts: the last turn again, since it may still grow. */
export function nextSince(turns: StaffTurn[]): number {
  return turns.length ? turns[turns.length - 1].index : 0;
}

export type NowChoice = { enabled: boolean; hint: "" | "staff.now.degrades" | "staff.now.interrupts" | "staff.now.native" | "staff.now.tui" };

/**
 * What "now" (a steer) does for this CLI, and so whether it is offered. A CLI with no way to reach a
 * running turn would silently wait for its end, so the choice is shown disabled and says why; one
 * whose Enter cancels the turn is offered, and says that it interrupts.
 */
export function nowChoice(caps: Pick<HarnessCapabilities, "steer"> | null | undefined): NowChoice {
  if (!caps) return { enabled: true, hint: "" };
  if (caps.steer === "degrade_to_queue") return { enabled: false, hint: "staff.now.degrades" };
  if (caps.steer === "cancel_and_send") return { enabled: true, hint: "staff.now.interrupts" };
  return { enabled: true, hint: caps.steer === "native" ? "staff.now.native" : "staff.now.tui" };
}

/**
 * The timing the staff composer sends: what the operator picked, else "now" as for the orchestrator's
 * Tell — except where "now" would stop the member's turn, which only a deliberate choice may do — and
 * "after the turn" wherever the CLI cannot take a message into a running turn at all.
 */
export function composerWhen(now: NowChoice, picked: "now" | "after_turn" | null): "now" | "after_turn" {
  if (!now.enabled) return "after_turn";
  return picked ?? (now.hint === "staff.now.interrupts" ? "after_turn" : "now");
}

/** Whether a permission can be answered "always": a CLI that asks for permissions has some form of
 *  "don't ask again" (the adapter falls back to a plain allow where it cannot deliver one). Never for
 *  a question, a folder, or a Daedalus member, whose gate has no such answer. */
export function canAlways(ask: Pick<Ask, "kind">, caps: Pick<HarnessCapabilities, "permissions"> | null | undefined): boolean {
  return ask.kind === "permission" && !!caps && caps.permissions !== "none";
}

/** The MCP server of a tool named `mcp__<server>__<tool>`, or "" for any other tool. Read the way
 *  Claude Code reads it (and the host's `mcp_server`): the server lies between the first two `__`. */
export function mcpServer(tool: string): string {
  const m = /^mcp__(.+?)__(.+)$/.exec(tool);
  return m ? m[1] : "";
}

/** The server whose every tool "Always" can allow at once for this request, or "": only for an MCP
 *  tool, and only where the member's CLI keeps the operator's grants as rules (`standing_rules`).
 *  A built-in tool never gets it — its only rule would be the whole tool, every command or file. */
export function alwaysServer(ask: Pick<Ask, "kind" | "detail">, caps: Pick<HarnessCapabilities, "permissions" | "standing_rules"> | null | undefined): string {
  if (!canAlways(ask, caps) || !caps?.standing_rules) return "";
  return mcpServer(typeof ask.detail?.tool === "string" ? ask.detail.tool : "");
}

/** A standing rule in the operator's words: the one tool, or every tool of a server. */
export function ruleWords(rule: string): string {
  const server = mcpServer(rule);
  if (server) return t("staff.rules.tool", { tool: rule.slice(`mcp__${server}__`.length), server });
  const whole = /^mcp__([^_].*)$/.exec(rule);
  return whole && !whole[1].includes("__") ? t("staff.rules.server", { server: whole[1] }) : rule;
}

/** A request's text with an MCP tool's name read the way a standing rule is: the host writes
 *  "<tool>: <what>", and `mcp__daedalus_browser__BrowserNavigate` in front of a URL asked the operator
 *  to parse an internal identifier before granting it. The raw text stays the card's tooltip. */
export function askWords(ask: Pick<Ask, "text" | "detail">): string {
  const tool = typeof ask.detail?.tool === "string" ? ask.detail.tool : "";
  if (!tool.startsWith("mcp__") || !ask.text.startsWith(tool)) return ask.text;
  return ruleWords(tool) + ask.text.slice(tool.length);
}

// The column beside the terminal: the operator drags it wider or narrower, and the width is kept per
// browser. Until they do, the stylesheet's own width stands (a quarter of the window, 280–360 px).

export const ASIDE_KEY = "daedalus.staff.asideWidth";
export const ASIDE_W_MIN = 260;
export const ASIDE_W_MAX = 760;
/** What the terminal keeps however wide the column is dragged: about eighty columns of a CLI. */
export const ASIDE_TERMINAL_MIN = 480;
/** One arrow key's worth of width. */
export const ASIDE_STEP = 16;

/** A width held to its bounds, and to what leaves the terminal its share of `body` (the row both sit in). */
export function clampAside(width: number, body: number): number {
  const max = Math.min(ASIDE_W_MAX, Math.max(ASIDE_W_MIN, body - ASIDE_TERMINAL_MIN));
  return Math.round(Math.min(max, Math.max(ASIDE_W_MIN, width)));
}

type WidthStore = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function widthStore(): WidthStore | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

/** The width the operator left the column at, or null for the stylesheet's own. */
export function readAsideWidth(storage: WidthStore | null = widthStore()): number | null {
  try {
    const raw = storage?.getItem(ASIDE_KEY);
    const n = raw == null ? NaN : Number(raw);
    return Number.isFinite(n) && n >= ASIDE_W_MIN && n <= ASIDE_W_MAX ? Math.round(n) : null;
  } catch {
    return null;
  }
}

/** Keep a width, or forget it (null) so the stylesheet's own comes back. A browser without site data keeps it for the visit. */
export function rememberAsideWidth(width: number | null, storage: WidthStore | null = widthStore()): void {
  try {
    if (width == null) storage?.removeItem(ASIDE_KEY);
    else storage?.setItem(ASIDE_KEY, String(Math.round(width)));
  } catch {
    /* private mode */
  }
}

/** The requests of this member that wait for an answer, oldest first: the one waiting longest is the one on top. */
export function openRequests<T extends Pick<Ask, "resolved_at" | "created_at"> & { staff_id: string | null }>(asks: T[], staffId: string): T[] {
  return asks.filter((a) => a.staff_id === staffId && !a.resolved_at).sort((a, b) => a.created_at.localeCompare(b.created_at));
}

/** Who answered first, from the host's refusal ("request k7m2qd was already answered by the orchestrator"). */
export function answeredBy(detail: string): "operator" | "orchestrator" | "terminal" | "" {
  const m = /answered by the (operator|orchestrator|terminal)/.exec(detail);
  return m ? (m[1] as "operator" | "orchestrator" | "terminal") : "";
}

export type StaffViewMode = "feed" | "terminal";

/** The Feed below a desktop's width, the terminal from it: a phone reads, a desktop watches the TUI. */
export function defaultMode(width: number, saved: string | null, hasTerminal: boolean): StaffViewMode {
  if (!hasTerminal) return "feed";
  if (saved === "feed" || saved === "terminal") return saved;
  return width >= 1024 ? "terminal" : "feed";
}

/**
 * Whether the keyboard banner shows: a person holds the terminal's keyboard, and a message from the
 * orchestrator waits for them to let go. The operator's own messages never wait for the keyboard.
 */
export function keyboardBlocks(keyboard: { owner: string; until: number | null } | null | undefined, messages: Pick<StaffMessage, "state" | "origin">[], now = Date.now()): boolean {
  if (!keyboard || keyboard.owner !== "human") return false;
  if (keyboard.until !== null && keyboard.until <= now) return false;
  return messages.some((m) => m.state === "queued" && m.origin === "orchestrator");
}

/** "turn 4 · 18 min": how many turns were asked for, and how long the current state has lasted. */
export function turnFacts(turns: Pick<StaffTurn, "role" | "started_at">[], since: string | null | undefined, now = Date.now()): { turn: number; minutes: number | null } {
  const asked = turns.filter((t) => t.role === "user" || t.role === "orchestrator");
  const from = asked.length ? asked[asked.length - 1].started_at || since : since;
  const at = from ? Date.parse(from) : NaN;
  return { turn: asked.length, minutes: Number.isFinite(at) ? Math.max(0, Math.floor((now - at) / 60000)) : null };
}

/** The health line's parts, each with its dictionary key and whether it warns. Ordered as the
 *  operator reads them: can it reach us, when did it last speak, did our last message land. */
export function healthParts(health: ChannelHealth | null | undefined): { key: string; at?: string | null; warn: boolean; n?: number }[] {
  if (!health) return [];
  const parts: { key: string; at?: string | null; warn: boolean; n?: number }[] = [];
  parts.push({ key: `staff.health.tools.${health.team_tools}`, warn: health.team_tools === "missing" });
  // Silence is said only when it is past the threshold: "heard 0 min ago" beside "hook now" is noise.
  if (health.silent && health.silent_s !== null) parts.push({ key: "staff.health.silent", n: Math.floor(health.silent_s / 60), warn: true });
  if (health.last_hook_at) parts.push({ key: "staff.health.hook", at: health.last_hook_at, warn: false });
  if (health.last_team_call_at) parts.push({ key: "staff.health.team", at: health.last_team_call_at, warn: false });
  if (health.last_message?.state === "failed") parts.push({ key: "staff.health.failed", warn: true });
  else if (health.last_acknowledged_at) parts.push({ key: "staff.health.accepted", at: health.last_acknowledged_at, warn: false });
  return parts;
}

/** Where a CLI's status comes from, in the reader's words: the table's code where the app has a word
 *  for it, and the table's own English label for a code added since. */
export function channelWords(caps: Pick<HarnessCapabilities, "status_channel" | "status_channel_label"> | null | undefined): string {
  if (!caps) return "";
  const key = `harness.channel.${caps.status_channel}`;
  return key in DICT ? t(key) : caps.status_channel_label;
}
