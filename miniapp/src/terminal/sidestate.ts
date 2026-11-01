// The Terminals workspace's side panel as data: what it remembers (open, width, tab), the actions
// feed of the active pane in one shape for both of its sources, and the two workspace changes the
// panel makes that the grid itself does not (a terminal put into a chosen place, a saved layout
// restored). Pure, so the edge cases are tested without a browser.
//
// The feed has two sources and one shape. A shell's are the commands its integration marked (the
// command, its exit code, how long it ran); a staff member's are the tool calls of its CLI's own
// transcript, which is the same feed the staff view shows. Either way a row has a kind, a title,
// whether it failed, and when.

import type { StaffTurn, TerminalCommand, TerminalLayout } from "../api";
import { normalise, SLOTS, type Workspace } from "./workspacestate";

export type SideTab = "files" | "actions" | "panes";
export const SIDE_TABS: SideTab[] = ["files", "actions", "panes"];

export const SIDE_KEY = "daedalus.term.side";
export const SIDE_W_DEFAULT = 360;
export const SIDE_W_MIN = 280;
export const SIDE_W_MAX = 720;

export type SidePrefs = { open: boolean; width: number; tab: SideTab };
export const SIDE_DEFAULT: SidePrefs = { open: false, width: SIDE_W_DEFAULT, tab: "files" };

/** A width the panel may take in a window this wide: never under its minimum, never more than half
 *  the window, so the grid beside it keeps at least as much room as the panel has. */
export function clampSide(width: number, viewport: number): number {
  const most = Math.max(SIDE_W_MIN, Math.min(SIDE_W_MAX, Math.floor(viewport / 2)));
  if (!Number.isFinite(width)) return Math.min(SIDE_W_DEFAULT, most);
  return Math.round(Math.min(most, Math.max(SIDE_W_MIN, width)));
}

type Store = Pick<Storage, "getItem" | "setItem">;

function store(): Store | null {
  try {
    return localStorage;
  } catch {
    return null;
  }
}

export function readSide(storage: Store | null = store()): SidePrefs {
  try {
    const raw = JSON.parse(storage?.getItem(SIDE_KEY) ?? "null") as Partial<SidePrefs> | null;
    if (!raw || typeof raw !== "object") return SIDE_DEFAULT;
    return {
      open: raw.open === true,
      width: typeof raw.width === "number" && Number.isFinite(raw.width) ? raw.width : SIDE_W_DEFAULT,
      tab: SIDE_TABS.includes(raw.tab as SideTab) ? (raw.tab as SideTab) : "files",
    };
  } catch {
    return SIDE_DEFAULT;
  }
}

export function saveSide(prefs: SidePrefs, storage: Store | null = store()): void {
  try {
    storage?.setItem(SIDE_KEY, JSON.stringify(prefs));
  } catch {
    /* remembered for this page only */
  }
}

// ── the actions feed ──────────────────────────────────────────────────────────────────────

export type ActionKind = "shell" | "reads" | "search" | "edits" | "git" | "web" | "mcp" | "agents" | "tool";
/** The order the filter pills are drawn in, whatever order the rows came in. */
export const ACTION_KINDS: ActionKind[] = ["shell", "reads", "search", "edits", "git", "web", "mcp", "agents", "tool"];

export type ActionRow = {
  key: string;
  kind: ActionKind;
  title: string;
  /** The folder a command ran in, or the tool's name for a tool call whose summary is the title. */
  detail: string;
  at: string | null;
  durationMs: number | null;
  failed: boolean;
  running: boolean;
  exit: number | null;
  /** A shell command's number, by which its output is asked for. */
  n?: number;
};

const READERS = new Set(["cat", "less", "more", "head", "tail", "bat", "ls", "ll", "tree", "stat", "file", "wc", "du", "df", "pwd"]);
const SEARCHERS = new Set(["grep", "rg", "ag", "ack", "find", "fd", "fzf", "locate", "which", "whereis"]);
const EDITORS = new Set(["vim", "vi", "nvim", "nano", "emacs", "micro", "hx", "helix", "code", "touch", "mv", "cp", "rm", "mkdir", "patch"]);
const FETCHERS = new Set(["curl", "wget", "http", "xh", "ssh", "scp", "rsync", "ping"]);

/** The program a command line runs: past `VAR=value` settings and the wrappers that run another. */
export function program(command: string): string {
  const words = command.trim().split(/\s+/);
  let i = 0;
  while (i < words.length && (/^[A-Za-z_][A-Za-z0-9_]*=/.test(words[i]) || ["sudo", "env", "time", "nice", "nohup", "exec", "command"].includes(words[i]))) i += 1;
  return (words[i] ?? "").replace(/^.*\//, "");
}

export function commandKind(command: string): ActionKind {
  const name = program(command);
  if (name === "git" || name === "gh") return "git";
  if (READERS.has(name)) return "reads";
  if (SEARCHERS.has(name)) return "search";
  if (EDITORS.has(name) || (name === "sed" && /\s-i\b/.test(command))) return "edits";
  if (FETCHERS.has(name)) return "web";
  return "shell";
}

/** A CLI's tool by its name: Claude's `Read`, OpenCode's `read`, Grok's `read_file`, an MCP server's
 *  `mcp__server__tool`. The words are the ones the CLIs use; anything else is a plain tool. */
export function toolKind(name: string): ActionKind {
  const lower = name.toLowerCase();
  if (lower.startsWith("mcp__") || lower.startsWith("mcp:")) return "mcp";
  // A to-do list is the agent's own notes, not an edit of the operator's files.
  if (lower.includes("todo")) return "tool";
  if (/(^|_)(bash|shell|exec|run_command|terminal|command)($|_)/.test(lower)) return "shell";
  if (/(^|_)(task|agent|subagent|delegate)($|_)/.test(lower)) return "agents";
  if (/web|fetch|browse|search_web|url/.test(lower)) return "web";
  if (/grep|glob|search|find/.test(lower)) return "search";
  if (/edit|write|patch|replace|create|notebook/.test(lower)) return "edits";
  if (/read|view|list|ls|cat/.test(lower)) return "reads";
  return "tool";
}

/** A shell's commands, newest first. A running one has no exit code yet. */
export function commandActions(commands: TerminalCommand[]): ActionRow[] {
  return commands
    .filter((c) => c.command.trim())
    .map((c) => ({
      key: `c${c.n}`,
      kind: commandKind(c.command),
      title: c.command.trim(),
      detail: c.cwd,
      at: c.started_at || null,
      durationMs: c.duration_ms,
      running: c.exit_code === null && !c.finished_at,
      failed: c.exit_code !== null && c.exit_code !== 0,
      exit: c.exit_code,
      n: c.n,
    }))
    .reverse();
}

/** A staff member's tool calls, newest first: every tool of every turn, at its turn's time. A tool
 *  whose result has not come back (`ok` null) on the turn still being written is running. */
export function turnActions(turns: StaffTurn[]): ActionRow[] {
  const rows: ActionRow[] = [];
  const last = turns.length ? turns[turns.length - 1].index : -1;
  for (const turn of turns) {
    turn.tools.forEach((tool, i) => {
      rows.push({
        key: `t${turn.index}.${i}`,
        kind: toolKind(tool.name),
        title: tool.summary || tool.name,
        detail: tool.name,
        at: turn.started_at || null,
        durationMs: null,
        running: tool.ok === null && turn.index === last && !turn.ended_at,
        failed: tool.ok === false,
        exit: null,
      });
    });
  }
  return rows.reverse();
}

export type KindFilter = ActionKind | "all";

/** The pills: every kind that has a row, with its count, in `ACTION_KINDS` order. */
export function kindCounts(rows: ActionRow[]): { kind: ActionKind; n: number }[] {
  return ACTION_KINDS.map((kind) => ({ kind, n: rows.filter((r) => r.kind === kind).length })).filter((k) => k.n > 0);
}

export function filterActions(rows: ActionRow[], kind: KindFilter, failedOnly: boolean): ActionRow[] {
  return rows.filter((r) => (kind === "all" || r.kind === kind) && (!failedOnly || r.failed));
}

// ── the workspace changes the panel makes ─────────────────────────────────────────────────

/** Puts a terminal into one place. Already in another place: the two places trade terminals, so the
 *  one it lands on is kept rather than dropped. The place gets the keyboard. */
export function assign(ws: Workspace, slot: number, id: string): Workspace {
  if (slot < 0 || slot >= SLOTS) return ws;
  const panes = [...ws.panes];
  const at = panes.indexOf(id);
  if (at >= 0) panes[at] = panes[slot];
  panes[slot] = id;
  return { ...ws, panes, active: slot < ws.layout ? slot : ws.active, zoom: null };
}

export type Missing = { slot: number; id: string; info: TerminalLayout["terminals"][string] | null };

/**
 * A saved layout as the workspace to show, given the terminals the host lists now. A place whose
 * terminal is gone or has ended is left empty and named in `missing`, with what reopening it takes
 * when the layout kept that: a pane that silently showed an old screen, or nothing, would read as
 * the layout having restored.
 */
export function restoreLayout(saved: Pick<TerminalLayout, "workspace" | "terminals">, running: ReadonlySet<string>): { ws: Workspace; missing: Missing[] } {
  const ws = normalise({ ...saved.workspace, zoom: null });
  const missing: Missing[] = [];
  const panes = ws.panes.map((id, slot) => {
    if (!id || running.has(id)) return id;
    missing.push({ slot, id, info: saved.terminals?.[id] ?? null });
    return null;
  });
  return { ws: { ...ws, panes }, missing };
}

/** Whether an ended terminal can be opened again from what the layout kept: a plain shell, not a
 *  staff member's CLI, which only its launch can start. */
export function reopenable(info: Missing["info"]): boolean {
  return !!info && info.profile === "shell" && info.owner_kind !== "staff" && !!info.cwd;
}
